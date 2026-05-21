"""
Disk-backed cache for Google Cloud Vision OCR responses.

Keyed by SHA-256 of the raw image bytes so:
  - Identical page images across runs never re-call the Vision API.
  - On network loss mid-run: resume picks up from the exact page where
    the outage started — any already-OCR'd page is free on the next run.
  - AWS Batch task restarts (spot interruption) are zero-cost for cached pages.

Storage layout:
  <output_root>/api_cache/<first2_hex>/<sha256>.json

Each JSON file stores a compact list of annotation tokens:
  [{"d": "<text>", "v": [{"x": N, "y": N}, ...]}, ...]

Thread-safety: reads are always safe. Writes use tmp-then-rename for atomicity.
Different worker processes write to disjoint shards (different image bytes →
different SHA → different file) so no locking is required.
"""

import hashlib
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Fake annotation objects that look like google.cloud.vision EntityAnnotation
# ---------------------------------------------------------------------------

class _FakeVertex:
    __slots__ = ("x", "y")
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


class _FakePoly:
    __slots__ = ("vertices",)
    def __init__(self, vertices):
        self.vertices = vertices


class _FakeAnnotation:
    __slots__ = ("description", "bounding_poly")
    def __init__(self, description: str, bounding_poly):
        self.description = description
        self.bounding_poly = bounding_poly


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialize(annotations: list) -> list[dict]:
    """Convert Vision API annotation objects to JSON-serialisable dicts."""
    out = []
    for ann in annotations:
        try:
            poly  = ann.bounding_poly
            verts = [{"x": v.x, "y": v.y} for v in (poly.vertices if poly else [])]
        except Exception:
            verts = []
        out.append({"d": ann.description or "", "v": verts})
    return out


def _deserialize(data: list[dict]) -> list:
    """Reconstruct fake annotation objects from cached JSON data."""
    result = []
    for item in data:
        verts = [_FakeVertex(v.get("x", 0), v.get("y", 0)) for v in item.get("v", [])]
        poly  = _FakePoly(verts)
        result.append(_FakeAnnotation(item.get("d", ""), poly))
    return result


# ---------------------------------------------------------------------------
# Cache class
# ---------------------------------------------------------------------------

class ApiResponseCache:
    """
    Persistent per-image OCR cache.  Works across process restarts, spot
    interruptions, and resume runs.  Cache misses are transparent: the caller
    falls through to the Vision API as normal.
    """

    def __init__(self, cache_dir: Path):
        self._root = Path(cache_dir)

    # -- key helpers ----------------------------------------------------------

    def _sha(self, image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()

    def _path(self, sha: str) -> Path:
        return self._root / sha[:2] / f"{sha}.json"

    # -- public API -----------------------------------------------------------

    def get(self, image_bytes: bytes) -> list | None:
        """Return cached annotation objects or None on cache miss."""
        p = self._path(self._sha(image_bytes))
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return _deserialize(data)
        except Exception:
            return None     # corrupted entry — treat as miss

    def put(self, image_bytes: bytes, annotations: list) -> None:
        """Persist annotations to disk atomically. Silently ignores errors."""
        sha = self._sha(image_bytes)
        p   = self._path(sha)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            serialised = _serialize(annotations)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(serialised, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(p)
        except Exception:
            pass    # failed write → cache miss next time, no harm done

    def stats(self) -> dict:
        """Return {entries, size_mb} for display in run summaries."""
        entries = 0
        total_bytes = 0
        for p in self._root.rglob("*.json"):
            try:
                entries += 1
                total_bytes += p.stat().st_size
            except OSError:
                pass
        return {"entries": entries, "size_mb": round(total_bytes / 1_000_000, 2)}

    def evict_older_than(self, days: int = 60) -> int:
        """Delete entries not accessed in `days` days. Returns count deleted."""
        import time
        cutoff = time.time() - days * 86_400
        n = 0
        for p in self._root.rglob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n


# ---------------------------------------------------------------------------
# Process-level singleton — initialised once from main.py startup
# ---------------------------------------------------------------------------

_cache: ApiResponseCache | None = None


def init_cache(output_root: Path) -> ApiResponseCache:
    """Initialise (or return existing) the process-level cache singleton."""
    global _cache
    if _cache is None:
        _cache = ApiResponseCache(Path(output_root) / "api_cache")
    return _cache


def get_cache() -> ApiResponseCache | None:
    """Return the current cache singleton, or None if not yet initialised."""
    return _cache

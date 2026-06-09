import json
import logging
import os
import random
import threading
import time
from pathlib import Path

import google.generativeai as genai

from config import (
    COUNTY_LIST_CLEAN,
    MODEL_FLASH_NAME,
    MODEL_PRO_NAME,
    VALID_COUNTY_LIST_ORIGINAL,
)

log = logging.getLogger(__name__)

# Compact county lists — used in prompts below.
_county_list_str  = ", ".join(COUNTY_LIST_CLEAN)          # base names, no quotes
_county_full_str  = ", ".join(VALID_COUNTY_LIST_ORIGINAL) # full names, no quotes

# ---------------------------------------------------------------------------
# Prompts — kept minimal to reduce input tokens.
# max_output_tokens=20 caps the response (county name ≤ 3 words + punctuation).
# ---------------------------------------------------------------------------

prompt_pass1 = f"""Oklahoma well record image. Find the county base name next to "County".
Valid: {_county_list_str}
Reply with ONLY the base name in lowercase (e.g. creek). If absent: Not detected."""

prompt_pass2 = f"""Oklahoma well record image. Identify the county name.
Valid: {_county_full_str}
Reply with ONLY the county name (e.g. Creek County). If absent: Not detected."""


# ---------------------------------------------------------------------------
# Global rate limiter — enforces min gap between Gemini API calls.
# GEMINI_MIN_CALL_GAP_S env var: default 3s (free tier ≈ 20 RPM effective).
#
# Multi-process note: _CALL_GAP and _rate_lock are per-process (multiprocessing
# workers do NOT share state).  With N workers each calling at 1/gap RPS, the
# combined rate is N/gap.  To stay within the free-tier 15 RPM limit:
#   gap_s ≥ N_workers × (60 / 15) = N_workers × 4.0 s
# Examples:  1 worker → 4.5 s    2 workers → 9.0 s
#
# Startup jitter: initialise _last_call to a random offset in [0, _CALL_GAP)
# seconds ago.  This staggers parallel worker processes so their first Gemini
# calls are spread across the gap window instead of bursting simultaneously.
# Without jitter, all workers start with _last_call=0 → first calls fire at
# the same instant → both hit the per-minute quota limit together.
# ---------------------------------------------------------------------------
_CALL_GAP  = float(os.environ.get("GEMINI_MIN_CALL_GAP_S", "3.0"))
_rate_lock = threading.Lock()
# Random startup offset staggers workers: each process independently picks a
# point in [0, _CALL_GAP) seconds ago as its "last call", so their first
# actual calls are naturally spread across the full gap window.
_last_call = time.monotonic() - random.uniform(0.0, _CALL_GAP)

# Per-model quota-exhaustion state: if a model hits 429, back off separately.
# Maps model_name -> (backoff_until_monotonic, consecutive_429_count)
_quota_state: dict[str, tuple[float, int]] = {}
_quota_lock  = threading.Lock()

# Back-off schedule for 429s: sleep these many seconds before retrying.
# Doubles each consecutive hit, capped at 300s (5 min).
_QUOTA_BACKOFF_BASE = 30.0
_QUOTA_BACKOFF_MAX  = 300.0
_QUOTA_MAX_RETRIES  = 6

# ---------------------------------------------------------------------------
# Multi-key rotation — set GOOGLE_API_KEY to a comma-separated list of keys.
# When the active key is fully exhausted (all _QUOTA_MAX_RETRIES 429s used),
# the next key is activated automatically and the call is retried from scratch.
# ---------------------------------------------------------------------------
_api_keys:       list[str]  = []   # populated by setup_gemini()
_current_key_idx: int       = 0
_exhausted_keys:  set[int]  = set()
_key_lock        = threading.Lock()
_key_rotation_count: int    = 0    # total rotations this process lifetime


def _rotate_key() -> bool:
    """
    Switch genai to the next non-exhausted key.
    Returns True if a fresh key was activated; False if all keys are spent.
    Resets per-model quota state so the new key gets a clean slate.
    """
    global _current_key_idx, _key_rotation_count
    if not _api_keys:
        return False
    with _key_lock:
        start = _current_key_idx
        for i in range(1, len(_api_keys) + 1):
            candidate = (start + i) % len(_api_keys)
            if candidate not in _exhausted_keys:
                _current_key_idx = candidate
                _key_rotation_count += 1
                genai.configure(api_key=_api_keys[_current_key_idx])
                with _quota_lock:
                    _quota_state.clear()
                log.warning(
                    "Gemini API key rotated: now using key %d/%d "
                    "(rotation #%d, exhausted keys: %s)",
                    _current_key_idx + 1, len(_api_keys),
                    _key_rotation_count, sorted(_exhausted_keys),
                )
                _write_quota_events()
                return True
        log.error(
            "All %d Gemini API key(s) exhausted after %d rotation(s) — "
            "county stage will fail for remaining records.",
            len(_api_keys), _key_rotation_count,
        )
        return False


def _write_quota_events():
    """Persist quota event state to OUTPUT_ROOT/quota_events.json for run_batch_job."""
    output_root = os.environ.get("OUTPUT_ROOT", "")
    if not output_root:
        return
    path = Path(output_root) / "quota_events.json"
    try:
        path.write_text(json.dumps({
            "key_rotation_count": _key_rotation_count,
            "total_keys":         len(_api_keys),
            "exhausted_keys":     sorted(_exhausted_keys),
            "current_key_idx":    _current_key_idx,
        }), encoding="utf-8")
    except Exception:
        pass  # non-critical — best effort


def _record_quota_hit(model_name: str) -> float:
    """Register a 429 on `model_name`, return how long to sleep."""
    with _quota_lock:
        _, count = _quota_state.get(model_name, (0.0, 0))
        count += 1
        delay = min(_QUOTA_BACKOFF_BASE * (2 ** (count - 1)), _QUOTA_BACKOFF_MAX)
        delay += random.uniform(0, delay * 0.2)   # ±20% jitter
        _quota_state[model_name] = (time.monotonic() + delay, count)
        return delay


def _clear_quota_state(model_name: str):
    with _quota_lock:
        _quota_state.pop(model_name, None)


def _rate_limited_generate(model, prompt, image, cfg):
    """
    Call model.generate_content with:
    - global inter-call rate limiting (GEMINI_MIN_CALL_GAP_S)
    - immediate API key rotation on the FIRST 429 when multiple keys are loaded
    - per-model exponential back-off on 429 only when all keys have been tried
    - up to _QUOTA_MAX_RETRIES retries with back-off when on the last key

    Key-rotation strategy (changed from v1):
      v1: wait through 6 exponential back-offs (~17 min) on key 1 before
          switching to key 2.  Very wasteful when 3 keys are available.
      v2: on first 429, immediately rotate to the next key.  Only fall back
          to exponential back-off once all rotation budget is spent (i.e. on
          the last remaining key).  This cuts P99 wait from 17 min to ~0s
          for the first two rotations.
    """
    global _last_call

    model_name     = getattr(model, "model_name", str(model))
    max_rotations  = max(0, len(_api_keys) - 1)
    key_rotations  = 0

    while True:                          # outer loop: retry after key rotation
        quota_exhausted  = False
        key_just_rotated = False

        for attempt in range(1, _QUOTA_MAX_RETRIES + 1):
            # Honour per-model quota backoff (only active when no keys left to rotate to).
            with _quota_lock:
                backoff_until, _ = _quota_state.get(model_name, (0.0, 0))
            wait_quota = backoff_until - time.monotonic()
            if wait_quota > 0:
                log.warning("Gemini %s quota backoff — sleeping %.0fs (key %d/%d)",
                            model_name, wait_quota,
                            _current_key_idx + 1, len(_api_keys) or 1)
                time.sleep(wait_quota)

            # Global inter-call gap.
            with _rate_lock:
                now  = time.monotonic()
                wait = _CALL_GAP - (now - _last_call)
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.monotonic()

            try:
                result = model.generate_content([prompt, image], generation_config=cfg)
                _clear_quota_state(model_name)
                return result

            except Exception as exc:
                exc_type = type(exc).__name__
                exc_str  = str(exc)
                is_quota = (
                    "ResourceExhausted" in exc_type
                    or "429" in exc_str
                    or "quota" in exc_str.lower()
                    or "rate" in exc_str.lower()
                )
                is_server = (
                    "ServiceUnavailable" in exc_type
                    or "503" in exc_str
                    or "500" in exc_str
                    or "InternalServerError" in exc_type
                )

                if is_quota:
                    # ── Multi-key fast path: rotate immediately ────────────────
                    # Don't burn time on back-off when another key is available.
                    if len(_api_keys) > 1 and key_rotations < max_rotations:
                        log.warning(
                            "Gemini %s quota hit (key %d/%d) — rotating key immediately",
                            model_name,
                            _current_key_idx + 1, len(_api_keys),
                        )
                        with _key_lock:
                            _exhausted_keys.add(_current_key_idx)
                        if _rotate_key():
                            key_rotations += 1
                            _clear_quota_state(model_name)
                            key_just_rotated = True
                            break       # break inner → outer will retry with new key
                        # _rotate_key() returned False — all keys somehow gone
                        raise RuntimeError(
                            f"All {len(_api_keys)} Gemini API key(s) exhausted "
                            f"after {key_rotations} rotation(s)."
                        )

                    # ── Single key or all rotation budget spent: back-off ──────
                    delay = _record_quota_hit(model_name)
                    log.warning(
                        "Gemini %s quota hit (attempt %d/%d, key %d/%d) — backoff %.0fs: %s",
                        model_name, attempt, _QUOTA_MAX_RETRIES,
                        _current_key_idx + 1, len(_api_keys) or 1,
                        delay, exc_str[:120],
                    )
                    if attempt == _QUOTA_MAX_RETRIES:
                        quota_exhausted = True
                        break          # break inner loop → try key rotation below
                    continue           # sleep handled at top of next iteration

                if is_server and attempt < _QUOTA_MAX_RETRIES:
                    sleep = min(5 * attempt + random.uniform(0, 3), 60)
                    log.warning(
                        "Gemini %s server error (attempt %d/%d) — retry in %.0fs: %s",
                        model_name, attempt, _QUOTA_MAX_RETRIES, sleep, exc_str[:120],
                    )
                    time.sleep(sleep)
                    continue

                raise   # non-retryable (bad request, auth, etc.)

        # ── After inner loop: decide how to continue ───────────────────────────
        if key_just_rotated:
            continue   # outer loop: retry from scratch with new key

        if quota_exhausted:
            # Mark this key as spent and try to rotate if budget remains.
            with _key_lock:
                _exhausted_keys.add(_current_key_idx)
            if key_rotations < max_rotations and _rotate_key():
                key_rotations += 1
                log.warning(
                    "Key rotation %d/%d — retrying call from scratch",
                    key_rotations, max_rotations,
                )
                continue   # retry with new key
            # All keys exhausted — give a clear error.
            raise RuntimeError(
                f"All {len(_api_keys) or 1} Gemini API key(s) exhausted "
                f"({_QUOTA_MAX_RETRIES} retries each, {key_rotations} rotation(s)). "
                "Add more keys to GOOGLE_API_KEY (comma-separated) or upgrade to a "
                "paid Gemini plan."
            )
        break   # normal exit (return inside try is the only real path here)


def setup_gemini():
    """
    Initialise Gemini Flash and Pro models with deterministic generation
    (temperature=0). Requires GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS.

    GOOGLE_API_KEY may be a comma-separated list of keys for automatic rotation:
      GOOGLE_API_KEY=key1,key2,key3

    When the active key is fully exhausted (all quota retries spent), the next
    key is activated automatically. Returns the (flash_model, pro_model,
    generation_config) triple.
    """
    global _api_keys, _current_key_idx, _exhausted_keys, _key_rotation_count

    raw_key = os.environ.get("GOOGLE_API_KEY", "")
    _api_keys = [k.strip() for k in raw_key.split(",") if k.strip()]
    _current_key_idx   = 0
    _exhausted_keys    = set()
    _key_rotation_count = 0

    if _api_keys:
        genai.configure(api_key=_api_keys[0])
        if len(_api_keys) > 1:
            log.info("Gemini: %d API keys loaded — key rotation enabled", len(_api_keys))
        else:
            log.info("Gemini: 1 API key loaded (add more as GOOGLE_API_KEY=k1,k2 for rotation)")
    else:
        genai.configure()   # falls back to ADC (service account)
        log.info("Gemini: using Application Default Credentials (no API key)")

    flash = genai.GenerativeModel(MODEL_FLASH_NAME)
    pro   = genai.GenerativeModel(MODEL_PRO_NAME)
    # max_output_tokens=20: county names are ≤ 3 words; caps billing on output side.
    gen_config = genai.types.GenerationConfig(
        candidate_count=1,
        temperature=0.0,
        max_output_tokens=20,
    )
    return flash, pro, gen_config

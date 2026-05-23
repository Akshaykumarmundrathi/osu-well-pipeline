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

_county_list_str  = ", ".join(f"'{c}'" for c in COUNTY_LIST_CLEAN)
_county_full_str  = ", ".join(f"'{n}'" for n in VALID_COUNTY_LIST_ORIGINAL)

prompt_pass1 = f"""Analyze this image snippet from an Oklahoma well record form.

Find the Oklahoma county base name next to the word 'County'.

Valid base names: {_county_list_str}

Respond ONLY with the matching base name in lowercase (e.g. "creek", "okmulgee").
If nothing matches, respond ONLY with: Not detected."""

prompt_pass2 = f"""Analyze this image snippet from an Oklahoma well record form.

Identify the most likely Oklahoma county name shown.

Valid county names: {_county_full_str}

Respond ONLY with the best candidate using standard capitalization
(e.g. "Creek County", "Okmulgee County").
If nothing matches, respond ONLY with: Not detected."""


# ---------------------------------------------------------------------------
# Global rate limiter — enforces min gap between Gemini API calls.
# GEMINI_MIN_CALL_GAP_S env var: default 2s (paid project); set higher
# for free-tier (6s = 10 RPM limit).
# ---------------------------------------------------------------------------
_CALL_GAP  = float(os.environ.get("GEMINI_MIN_CALL_GAP_S", "2.0"))
_rate_lock = threading.Lock()
_last_call = 0.0

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
    - per-model exponential back-off on 429 ResourceExhausted
    - automatic API key rotation when a key is fully exhausted
    - up to _QUOTA_MAX_RETRIES retries per key, cycling through all keys
    """
    global _last_call

    model_name     = getattr(model, "model_name", str(model))
    max_rotations  = max(0, len(_api_keys) - 1)
    key_rotations  = 0

    while True:                          # outer loop: retry after key rotation
        quota_exhausted = False

        for attempt in range(1, _QUOTA_MAX_RETRIES + 1):
            # Honour per-model quota backoff.
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
                    delay = _record_quota_hit(model_name)
                    log.warning(
                        "Gemini %s quota hit (attempt %d/%d, key %d/%d) — backoff %.0fs: %s",
                        model_name, attempt, _QUOTA_MAX_RETRIES,
                        _current_key_idx + 1, len(_api_keys) or 1,
                        delay, exc_str[:120],
                    )
                    if attempt == _QUOTA_MAX_RETRIES:
                        quota_exhausted = True
                        break          # break inner loop → try key rotation
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

        if quota_exhausted:
            # Mark this key as spent and try to rotate.
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
        break   # should not be reached — all paths return, raise, or continue


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
    gen_config = genai.types.GenerationConfig(candidate_count=1, temperature=0.0)
    return flash, pro, gen_config

import logging
import os
import random
import threading
import time

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
    - up to _QUOTA_MAX_RETRIES retries before re-raising
    """
    global _last_call

    model_name = getattr(model, "model_name", str(model))

    for attempt in range(1, _QUOTA_MAX_RETRIES + 1):
        # Honour per-model quota backoff.
        with _quota_lock:
            backoff_until, _ = _quota_state.get(model_name, (0.0, 0))
        wait_quota = backoff_until - time.monotonic()
        if wait_quota > 0:
            log.warning("Gemini %s quota backoff — sleeping %.0fs", model_name, wait_quota)
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
            _clear_quota_state(model_name)   # success — reset backoff
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
                    "Gemini %s quota hit (attempt %d/%d) — backoff %.0fs: %s",
                    model_name, attempt, _QUOTA_MAX_RETRIES, delay, exc_str[:120],
                )
                if attempt == _QUOTA_MAX_RETRIES:
                    raise
                # sleep already handled at top of next loop via _quota_state
                continue

            if is_server and attempt < _QUOTA_MAX_RETRIES:
                sleep = min(5 * attempt + random.uniform(0, 3), 60)
                log.warning(
                    "Gemini %s server error (attempt %d/%d) — retry in %.0fs: %s",
                    model_name, attempt, _QUOTA_MAX_RETRIES, sleep, exc_str[:120],
                )
                time.sleep(sleep)
                continue

            raise   # non-retryable (bad request, auth, etc.)

    # Should be unreachable — loop always raises or returns.
    raise RuntimeError(f"Gemini {model_name} exhausted {_QUOTA_MAX_RETRIES} retries")


def setup_gemini():
    """
    Initialise Gemini Flash and Pro models with deterministic generation
    (temperature=0). Requires GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS.
    Returns the (flash_model, pro_model, generation_config) triple.
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        genai.configure(api_key=api_key)
    else:
        genai.configure()   # falls back to ADC (service account)

    flash = genai.GenerativeModel(MODEL_FLASH_NAME)
    pro   = genai.GenerativeModel(MODEL_PRO_NAME)
    gen_config = genai.types.GenerationConfig(candidate_count=1, temperature=0.0)
    return flash, pro, gen_config

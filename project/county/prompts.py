import os
import threading
import time

import google.generativeai as genai

from config import (
    COUNTY_LIST_CLEAN,
    MODEL_FLASH_NAME,
    MODEL_PRO_NAME,
    VALID_COUNTY_LIST_ORIGINAL,
)

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
# Global rate limiter — enforces min gap between Gemini API calls so
# N parallel workers stay under the free-tier RPM limit (10 RPM = 6s/call).
# GEMINI_MIN_CALL_GAP_S env var overrides (default 6s for free tier;
# set to 0 on a paid project).
# ---------------------------------------------------------------------------
_CALL_GAP = float(os.environ.get("GEMINI_MIN_CALL_GAP_S", "6.0"))
_rate_lock  = threading.Lock()
_last_call  = 0.0


def _rate_limited_generate(model, prompt, image, cfg):
    """Call model.generate_content with a global rate-limiter."""
    global _last_call
    with _rate_lock:
        now  = time.monotonic()
        wait = _CALL_GAP - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    return model.generate_content([prompt, image], generation_config=cfg)


def setup_gemini():
    """
    Initialise Gemini Flash and Pro models with deterministic generation
    (temperature=0). Requires GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS.
    Returns the (flash_model, pro_model, generation_config, generate_fn) tuple.
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

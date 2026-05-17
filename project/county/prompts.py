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


def setup_gemini():
    """
    Initialises Gemini Flash and Pro models.
    Requires GOOGLE_API_KEY environment variable.
    Returns (model_flash, model_pro, generation_config).
    """
    genai.configure()
    flash = genai.GenerativeModel(MODEL_FLASH_NAME)
    pro   = genai.GenerativeModel(MODEL_PRO_NAME)
    gen_config = genai.types.GenerationConfig(candidate_count=1, temperature=0.0)
    return flash, pro, gen_config

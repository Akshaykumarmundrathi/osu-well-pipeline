import google.generativeai as genai

from config import (
    MODEL_FLASH_NAME,
    MODEL_PRO_NAME,
    COUNTY_LIST_CLEAN,
    VALID_COUNTY_LIST_ORIGINAL,
)


def setup_gemini():
    """
    Initializes Gemini Flash & Pro models.

    Returns
    -------
    model_flash : GenerativeModel
    model_pro   : GenerativeModel
    gen_config  : GenerationConfig
    """
    genai.configure()

    model_flash = genai.GenerativeModel(MODEL_FLASH_NAME)
    model_pro   = genai.GenerativeModel(MODEL_PRO_NAME)

    generation_config_pro = genai.types.GenerationConfig(
        candidate_count=1,
        temperature=0.0
    )

    return model_flash, model_pro, generation_config_pro


# =====================================================
# PROMPT CONTEXT STRINGS
# =====================================================

county_list_context_str = ", ".join(
    f"'{name}'" for name in VALID_COUNTY_LIST_ORIGINAL
)

prompt_pass1 = f"""
Analyze the provided image snippet from a well record form.

Identify if any of the following specific Oklahoma county base names
are present anywhere in the image text.

It would be right next to the word 'County'.

Valid Oklahoma county base names include:
{", ".join(f"'{c}'" for c in COUNTY_LIST_CLEAN)}

Read the text carefully.

If you find text matching one of the base names,
respond ONLY with that matching base name in lowercase
(e.g., "creek", "okmulgee").

If multiple match, return the most likely one based on context.

If NO base name is identifiable, respond ONLY with:
Not detected.
"""

prompt_pass2 = f"""
Analyze the provided image snippet from a well record form,
likely containing a county name.

Identify the most likely Oklahoma county name shown in the image.

Valid Oklahoma county names include:
{county_list_context_str}

Read the text carefully.

Determine the most probable county name based on the visual text
and the list.

Respond ONLY with the best candidate county name using standard
capitalization (e.g., "Creek County", "Okmulgee", "Washington").

If NO county name seems present or identifiable from the list,
respond ONLY with:
Not detected.
"""

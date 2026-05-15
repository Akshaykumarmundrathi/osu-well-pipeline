import os
import pandas as pd
from pathlib import Path
from google.cloud import vision

from config import (
    PDF_FOLDER,
    COUNTY_IMAGE_OUTPUT_FOLDER,
    COUNTY_SUMMARY_CSV,
    COUNTY_KEYWORDS,
    PAGE_TO_PROCESS,
    EXTEND_LEFT_PIXELS,
    EXTEND_RIGHT_PIXELS,
    VERTICAL_PADDING_PIXELS,
)
from ocr.vision_api import get_page_annotations, find_keyword_box


def stage_county_image_extraction():
    print("▶ Stage 2: County Image Extraction")

    os.makedirs(COUNTY_IMAGE_OUTPUT_FOLDER, exist_ok=True)

    # Validate Vision API once
    try:
        vision.ImageAnnotatorClient()
        print("✓ Google Vision API ready")
    except Exception as e:
        raise RuntimeError(f"Vision API init failed: {e}")

    pdf_files = [
        f for f in os.listdir(PDF_FOLDER)
        if f.lower().endswith(".pdf")
    ]
    if not pdf_files:
        raise FileNotFoundError("No PDFs found for county extraction")

    summary = []

    for pdf_file in pdf_files:
        pdf_path = os.path.join(PDF_FOLDER, pdf_file)
        status = "Error"

        try:
            annotations, pil_image = get_page_annotations(
                pdf_path, page_num=PAGE_TO_PROCESS
            )

            if not annotations or not pil_image:
                status = "Vision Failure"
                raise ValueError(status)

            keyword_box = find_keyword_box(
                annotations,
                [k.lower() for k in COUNTY_KEYWORDS]
            )

            if not keyword_box:
                status = "Keyword Not Found"
                raise ValueError(status)

            x_min, y_min, x_max, y_max = keyword_box

            crop_box = (
                max(0, int(x_min - EXTEND_LEFT_PIXELS)),
                max(0, int(y_min - VERTICAL_PADDING_PIXELS)),
                min(pil_image.width,  int(x_max + EXTEND_RIGHT_PIXELS)),
                min(pil_image.height, int(y_max + VERTICAL_PADDING_PIXELS)),
            )

            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                status = "Invalid Crop Box"
                raise ValueError(status)

            cropped  = pil_image.crop(crop_box)
            out_name = f"{Path(pdf_file).stem}_page{PAGE_TO_PROCESS+1}_county.png"
            cropped.save(os.path.join(COUNTY_IMAGE_OUTPUT_FOLDER, out_name))

            status = "Cropped"

        except Exception as e:
            if status == "Error":
                status = f"Processing Error: {e}"

        summary.append({
            "PDF File": Path(pdf_file).stem,
            "Status":   status,
        })

    pd.DataFrame(summary).to_csv(COUNTY_SUMMARY_CSV, index=False)
    print("✓ County image extraction complete")

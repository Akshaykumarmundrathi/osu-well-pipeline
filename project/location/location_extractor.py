import os
import io

from google.cloud import vision

from config import PDF_FOLDER, LOCATION_OUTPUT_FOLDER, LOCATION_KEYWORDS
from pdf.pdf_manager import PDFDocumentManager
from ocr.vision_api import detect_text_with_vision
from location.grouping import (
    find_keywords_lists,
    choose_group,
    get_unified_bounding_box,
)


def find_relevant_page_with_vision(
    pdf_path,
    keywords,
    extend_right=400,
    padding_height=50
):
    """
    Processes every page of the PDF using centralized PDF manager.

    For each page:
      - Converts page to PIL image
      - Runs Vision OCR
      - Finds grouped Section/Township/Range keywords

    Returns:
        (page_num, group, text_annotations)
    """
    manager = PDFDocumentManager(pdf_path, resolution_multiplier=2)

    try:
        for page_num, image in manager.iter_pil_pages():

            text_annotations = detect_text_with_vision(image)

            print(f"\nDetected text annotations on page {page_num}:")

            if not text_annotations:
                continue

            sections, townships, ranges = find_keywords_lists(
                text_annotations,
                keywords,
                extend_right,
                padding_height
            )

            group = choose_group(sections, townships, ranges, min_overlap=0.5)

            if group is not None:
                print(f"Found grouped keywords on page {page_num}.")
                return (
                    page_num - 1,  # return 0-indexed for downstream use
                    group,
                    text_annotations
                )

    except Exception as e:
        print(f"Error processing PDF for vision grouping: {e}")

    print("No relevant grouping found with all keywords in the same area.")
    return None, None, None


def process_relevant_page_and_extract_unified_value(
    pdf_path,
    page_num,
    group,
    output_folder,
    section_right_extension=200
):
    """
    For the selected page:
    - Computes unified bounding box
    - Crops target region
    - Saves cropped image
    - Runs OCR on cropped region

    Uses centralized PDFDocumentManager instead of reopening PDFs manually.
    """
    unified_box = get_unified_bounding_box(group, section_right_extension)

    if unified_box is None:
        print("No unified bounding box found.")
        return

    manager = PDFDocumentManager(pdf_path, resolution_multiplier=2)
    cropped_image = None

    # Centralized PDF access — iter_pil_pages() returns 1-indexed pages
    for current_page_num, image in manager.iter_pil_pages():
        if current_page_num == (page_num + 1):
            cropped_image = image.crop(tuple(unified_box))
            break

    if cropped_image is None:
        print(f"Could not retrieve page {page_num + 1}")
        return

    pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
    cropped_filename = os.path.join(
        output_folder,
        f"{pdf_basename}_page_{page_num + 1}_unified_value.png"
    )
    cropped_image.save(cropped_filename)
    print(f"Unified value area saved as {cropped_filename}")

    # OCR on cropped image
    image_byte_array = io.BytesIO()
    cropped_image.save(image_byte_array, format="PNG")
    image_bytes = image_byte_array.getvalue()

    client = vision.ImageAnnotatorClient()
    vision_image = vision.Image(content=image_bytes)
    response = client.text_detection(image=vision_image)
    texts = response.text_annotations

    if texts:
        value = texts[0].description.strip()
        print(f"Extracted unified value: {value}")
    else:
        print("No unified value detected.")


def process_pdf_with_keywords(
    pdf_path,
    keywords,
    output_folder,
    extend_right=400,
    padding_height=50,
    section_right_extension=200
):
    """
    Processes a single PDF by finding a page where the grouped keywords appear together.
    Then it crops that unified area and extracts its text.
    """
    page_num, group, text_annotations = find_relevant_page_with_vision(
        pdf_path, keywords, extend_right, padding_height
    )
    if page_num is not None:
        process_relevant_page_and_extract_unified_value(
            pdf_path, page_num, group, output_folder, section_right_extension
        )
    else:
        print(f"No relevant page detected in {pdf_path} based on grouping criteria.")


def process_all_pdfs_in_folder(
    folder_path,
    keywords,
    output_folder,
    extend_right=400,
    padding_height=50,
    section_right_extension=200
):
    """
    Loops through all files in the specified folder.
    For each PDF file, processes it using the above functions.
    Cropped (unified) images will be saved with filenames including the original PDF file's name.
    """
    os.makedirs(output_folder, exist_ok=True)
    for file_name in os.listdir(folder_path):
        if file_name.lower().endswith(".pdf"):
            pdf_path = os.path.join(folder_path, file_name)
            print(f"\nProcessing file: {pdf_path}")
            process_pdf_with_keywords(
                pdf_path, keywords, output_folder,
                extend_right, padding_height, section_right_extension
            )
        else:
            print(f"Skipping non-PDF file: {file_name}")


def stage_location_extraction():
    print("▶ Stage 1: Location (Section/Township/Range) Extraction")

    os.makedirs(LOCATION_OUTPUT_FOLDER, exist_ok=True)

    process_all_pdfs_in_folder(
        folder_path=PDF_FOLDER,
        keywords=LOCATION_KEYWORDS,
        output_folder=LOCATION_OUTPUT_FOLDER,
        extend_right=500,
        padding_height=50,
        section_right_extension=200
    )

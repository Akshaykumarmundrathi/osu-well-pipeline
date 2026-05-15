import io

from google.cloud import vision

from ocr.preprocessing import preprocess_image
from pdf.pdf_manager import PDFDocumentManager


def detect_text_with_vision(image):
    """
    Saves the given PIL image to disk, preprocesses it,
    and sends it to Google Vision using document_text_detection.
    """
    temp_image_path = "temp_image.png"
    image.save(temp_image_path)

    preprocessed_image = preprocess_image(temp_image_path)

    img_byte_array = io.BytesIO()
    preprocessed_image.save(img_byte_array, format="PNG")
    image_bytes = img_byte_array.getvalue()

    client = vision.ImageAnnotatorClient()
    vision_image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=vision_image)

    return response.text_annotations


def get_page_annotations(pdf_path, page_num=0, resolution_multiplier=2.5):
    """
    Uses centralized PDF manager to:
      - render page
      - convert to PIL
      - run Vision OCR

    Returns:
        (text_annotations, pil_image)
    """
    try:
        manager = PDFDocumentManager(
            pdf_path,
            resolution_multiplier=resolution_multiplier
        )

        pil_image = manager.get_page_pil(page_num)

        if pil_image is None:
            return None, None

        img_byte_array = io.BytesIO()
        pil_image.save(img_byte_array, format="PNG")
        image_bytes = img_byte_array.getvalue()

        client = vision.ImageAnnotatorClient()
        vision_image = vision.Image(content=image_bytes)
        response = client.document_text_detection(image=vision_image)

        if response.error.message:
            print(f"Vision API Error: {response.error.message}")
            return None, pil_image

        if not response.text_annotations:
            return None, pil_image

        return response.text_annotations, pil_image

    except Exception as e:
        print(f"Error during PDF conversion or Vision API call: {e}")
        return None, None


def find_keyword_box(text_annotations, keywords_to_find):
    """
    Finds the first annotation containing any keyword.
    Returns bounding box [x_min, y_min, x_max, y_max].
    """
    if not text_annotations:
        return None

    # Skip full text annotation
    for annotation in text_annotations[1:]:
        raw_text = annotation.description
        cleaned = raw_text.lower().strip('.,:')

        for keyword in keywords_to_find:
            if keyword.lower() in cleaned:
                poly = annotation.bounding_poly
                if not poly or not poly.vertices:
                    continue

                try:
                    x_coords = [v.x for v in poly.vertices]
                    y_coords = [v.y for v in poly.vertices]

                    if not all(isinstance(c, (int, float)) for c in x_coords + y_coords):
                        continue

                    return [
                        min(x_coords),
                        min(y_coords),
                        max(x_coords),
                        max(y_coords),
                    ]
                except Exception:
                    continue
    return None

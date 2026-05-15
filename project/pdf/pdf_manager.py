import os
import fitz

from pdf.rendering import pixmap_to_pil, pixmap_to_cv2


class PDFDocumentManager:
    """
    Centralized PDF manager for:
    - Opening PDFs
    - Rendering pages
    - Returning PIL / CV2 images
    - Preventing repeated PDF access
    """

    def __init__(self, pdf_path, resolution_multiplier=2):
        self.pdf_path = pdf_path
        self.resolution_multiplier = resolution_multiplier

    def iter_pil_pages(self):
        """
        Yield:
            (page_number, PIL_image)
        page_number is 1-indexed.
        """
        document = fitz.open(self.pdf_path)
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                matrix = fitz.Matrix(
                    self.resolution_multiplier,
                    self.resolution_multiplier
                )
                pix = page.get_pixmap(matrix=matrix, alpha=False)

                pil_image = pixmap_to_pil(pix)
                if pil_image is None:
                    del pix
                    continue

                yield page_index + 1, pil_image

                del pix
                del pil_image
        finally:
            document.close()

    def iter_cv2_pages(self):
        """
        Yield:
            (page_number, cv2_bgr_image)
        page_number is 1-indexed.
        """
        document = fitz.open(self.pdf_path)
        try:
            print(
                f"Processing PDF '{os.path.basename(self.pdf_path)}': "
                f"{len(document)} pages..."
            )
            for page_index in range(len(document)):
                try:
                    page = document[page_index]
                    matrix = fitz.Matrix(
                        self.resolution_multiplier,
                        self.resolution_multiplier
                    )
                    pix = page.get_pixmap(matrix=matrix, alpha=False)

                    cv_image = pixmap_to_cv2(pix)
                    if cv_image is None:
                        del pix
                        continue

                    yield page_index + 1, cv_image

                    del pix
                    del cv_image

                except Exception as e:
                    print(f"Fatal Error processing {self.pdf_path}: {e}")
        finally:
            document.close()
            print("  Conversion complete.")

    def get_page_pil(self, page_num):
        """
        Returns a single PIL page image.
        page_num is 0-indexed.
        """
        document = fitz.open(self.pdf_path)
        try:
            if page_num >= len(document):
                return None

            page = document[page_num]
            matrix = fitz.Matrix(
                self.resolution_multiplier,
                self.resolution_multiplier
            )
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            return pixmap_to_pil(pix)
        finally:
            document.close()

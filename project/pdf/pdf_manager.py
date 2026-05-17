"""
Centralized PDF manager.

Accepts either a file path OR raw bytes (for ZIPs).
All page iteration streams one page at a time — no full-PDF RAM load.
"""

import fitz  # PyMuPDF

from pdf.rendering import pixmap_to_cv2, pixmap_to_pil


class PDFDocumentManager:

    def __init__(
        self,
        pdf_path=None,
        *,
        pdf_bytes: bytes = None,
        resolution_multiplier: float = 2.0,
    ):
        if pdf_path is None and pdf_bytes is None:
            raise ValueError("Provide pdf_path or pdf_bytes")
        self.pdf_path             = str(pdf_path) if pdf_path else None
        self._bytes               = pdf_bytes
        self.resolution_multiplier = resolution_multiplier

    # ── internal ─────────────────────────────────────────────────────────────

    def _open(self) -> fitz.Document:
        if self._bytes:
            return fitz.open(stream=self._bytes, filetype="pdf")
        return fitz.open(self.pdf_path)

    def _matrix(self) -> fitz.Matrix:
        m = self.resolution_multiplier
        return fitz.Matrix(m, m)

    # ── public iterators ─────────────────────────────────────────────────────

    def iter_pil_pages(self):
        """Yield (1-indexed page_num, PIL image) streaming one page at a time."""
        doc = self._open()
        try:
            for i in range(len(doc)):
                pix = doc[i].get_pixmap(matrix=self._matrix(), alpha=False)
                img = pixmap_to_pil(pix)
                del pix
                if img is not None:
                    yield i + 1, img
                del img
        finally:
            doc.close()

    def iter_cv2_pages(self):
        """Yield (1-indexed page_num, BGR numpy array) streaming one page at a time."""
        doc = self._open()
        try:
            for i in range(len(doc)):
                pix = doc[i].get_pixmap(matrix=self._matrix(), alpha=False)
                img = pixmap_to_cv2(pix)
                del pix
                if img is not None:
                    yield i + 1, img
                del img
        finally:
            doc.close()

    def get_page_pil(self, page_num: int):
        """Return a single PIL image. page_num is 0-indexed. Returns None if out of range."""
        doc = self._open()
        try:
            if page_num >= len(doc):
                return None
            pix = doc[page_num].get_pixmap(matrix=self._matrix(), alpha=False)
            return pixmap_to_pil(pix)
        finally:
            doc.close()

    def page_count(self) -> int:
        doc = self._open()
        try:
            return len(doc)
        finally:
            doc.close()

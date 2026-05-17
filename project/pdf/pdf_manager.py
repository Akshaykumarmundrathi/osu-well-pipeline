"""
Centralized PDF rendering manager.

Accepts a file path OR raw bytes (ZIP-extracted PDFs). All iteration is
streaming — one page is rendered, yielded, and freed before the next is
loaded. No full-PDF RAM load.
"""

import fitz  # PyMuPDF

from pdf.rendering import pixmap_to_cv2, pixmap_to_pil


class PDFDocumentManager:
    """Lazy PDF accessor; opens the document fresh for each iteration call."""

    def __init__(
        self,
        pdf_path=None,
        *,
        pdf_bytes: bytes = None,
        resolution_multiplier: float = 2.0,
    ):
        if pdf_path is None and pdf_bytes is None:
            raise ValueError("Provide pdf_path or pdf_bytes")
        self.pdf_path              = str(pdf_path) if pdf_path else None
        self._bytes                = pdf_bytes
        self.resolution_multiplier = resolution_multiplier

    # -- internal -------------------------------------------------------------

    def _open(self) -> fitz.Document:
        """Open the document from bytes (preferred) or file path."""
        if self._bytes:
            return fitz.open(stream=self._bytes, filetype="pdf")
        return fitz.open(self.pdf_path)

    def _matrix(self) -> fitz.Matrix:
        """Render transform: uniform scale by `resolution_multiplier`."""
        m = self.resolution_multiplier
        return fitz.Matrix(m, m)

    def _iter(self, converter):
        """
        Internal page iterator. `converter` is a callable that turns a
        fitz Pixmap into the desired image type (PIL or cv2). Yields
        (1-indexed page_num, image). Releases each pixmap immediately.
        """
        doc = self._open()
        try:
            mat = self._matrix()
            for i in range(len(doc)):
                pix = doc[i].get_pixmap(matrix=mat, alpha=False)
                img = converter(pix)
                del pix
                if img is not None:
                    yield i + 1, img
                del img
        finally:
            doc.close()

    # -- public iterators -----------------------------------------------------

    def iter_pil_pages(self):
        """Yield (1-indexed page_num, PIL.Image) one page at a time."""
        return self._iter(pixmap_to_pil)

    def iter_cv2_pages(self):
        """Yield (1-indexed page_num, BGR numpy array) one page at a time."""
        return self._iter(pixmap_to_cv2)

    def get_page_pil(self, page_num: int):
        """
        Render a single page as PIL. `page_num` is 0-indexed.
        Returns None if the page is out of range.
        """
        doc = self._open()
        try:
            if page_num >= len(doc):
                return None
            pix = doc[page_num].get_pixmap(matrix=self._matrix(), alpha=False)
            return pixmap_to_pil(pix)
        finally:
            doc.close()

    def page_count(self) -> int:
        """Return the number of pages in the document."""
        doc = self._open()
        try:
            return len(doc)
        finally:
            doc.close()

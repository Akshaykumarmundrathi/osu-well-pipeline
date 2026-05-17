import numpy as np
from PIL import Image as PILImage


def pixmap_to_pil(pix):
    """Convert a fitz Pixmap (RGB, no alpha) to a PIL RGB image."""
    if pix.samples is None:
        return None
    return PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)


def pixmap_to_cv2(pix):
    """
    Convert a fitz Pixmap (RGB, no alpha) directly to a BGR numpy array
    without going through cv2.cvtColor, avoiding an extra memory copy.
    """
    if pix.samples is None:
        return None
    np_img = np.frombuffer(
        pix.samples, dtype=np.uint8
    ).reshape(pix.height, pix.width, 3)
    return np_img[:, :, ::-1]  # RGB -> BGR

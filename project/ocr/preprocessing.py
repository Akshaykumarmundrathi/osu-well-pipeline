from PIL import Image as PILImage, ImageEnhance


def preprocess_image(pil_image: PILImage.Image) -> PILImage.Image:
    """
    Grayscale -> contrast boost -> binarize.
    Accepts a PIL image; returns a processed PIL image.
    No disk I/O — safe for parallel workers.
    """
    image = pil_image.convert("L")
    image = ImageEnhance.Contrast(image).enhance(2.0)
    image = image.point(lambda x: 0 if x < 128 else 255, "1")
    return image

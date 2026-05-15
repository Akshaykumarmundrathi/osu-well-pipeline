from PIL import Image as PILImage, ImageEnhance


def preprocess_image(image_path):
    """
    Convert the image (from file) to grayscale, increase contrast, and binarize it.
    """
    image = PILImage.open(image_path).convert("L")

    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2)  # Increase contrast

    image = image.point(lambda x: 0 if x < 128 else 255, '1')  # Binarize

    image.save("preprocessed_image.png")  # (Optional) for debugging

    return image

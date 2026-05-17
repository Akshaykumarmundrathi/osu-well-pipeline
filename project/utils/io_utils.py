"""
Image annotation helper. Other small I/O wrappers were removed because
nothing in the project imported them.
"""

from PIL import Image as PILImage, ImageDraw


def annotate_page(
    pil_image: PILImage.Image,
    bbox: tuple,
    color: str = "red",
    label: str = "",
    width: int = 4,
) -> PILImage.Image:
    """
    Return a copy of `pil_image` with `bbox` drawn as a coloured rectangle
    and an optional text `label` above its top-left corner. Used for
    saving annotated debug pages alongside extraction crops.
    """
    annotated = pil_image.copy().convert("RGB")
    draw = ImageDraw.Draw(annotated)
    x0, y0, x1, y1 = (int(c) for c in bbox)
    draw.rectangle([x0, y0, x1, y1], outline=color, width=width)
    if label:
        draw.text((x0 + 4, max(0, y0 - 18)), label, fill=color)
    return annotated

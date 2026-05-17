import io
from pathlib import Path
from PIL import Image as PILImage, ImageDraw


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_pdfs(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.pdf"))


def stem(path: Path) -> str:
    return Path(path).stem


def pil_to_bytes(image: PILImage.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format=fmt)
    return buf.getvalue()


def annotate_page(
    pil_image: PILImage.Image,
    bbox: tuple,
    color: str = "red",
    label: str = "",
    width: int = 4,
) -> PILImage.Image:
    """Draw a bounding box on a copy of the page for visual debugging."""
    annotated = pil_image.copy().convert("RGB")
    draw = ImageDraw.Draw(annotated)
    x0, y0, x1, y1 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    draw.rectangle([x0, y0, x1, y1], outline=color, width=width)
    if label:
        draw.text((x0 + 4, max(0, y0 - 18)), label, fill=color)
    return annotated

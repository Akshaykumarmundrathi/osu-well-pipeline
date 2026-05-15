import os


def ensure_dir(path: str) -> str:
    """Create directory (and parents) if it does not exist. Returns path."""
    os.makedirs(path, exist_ok=True)
    return path


def list_pdfs(folder: str) -> list:
    """Return sorted list of absolute paths for all .pdf files in folder."""
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".pdf")
    )


def stem(path: str) -> str:
    """Return filename without extension. Thin wrapper around os.path."""
    return os.path.splitext(os.path.basename(path))[0]

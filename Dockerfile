# OSU Well Records Pipeline — AWS Batch / Fargate Spot
# CPU-only: no GPU needed (U-Net dot detection uses PyTorch CPU inference)
#
# Build: python aws/build_image.py
# Base:  python:3.11-slim (Debian bookworm) — smallest image with system libs

FROM python:3.11-slim

# ── System dependencies ──────────────────────────────────────────────────────
# tesseract-ocr: OCR engine (primary, replaces Vision API)
# libGL / libSM: OpenCV runtime
# poppler-utils: PDF utilities (PyMuPDF uses its own renderer so this is optional)
# gcc / g++: compile psycopg2, rapidfuzz
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libgomp1 \
        gcc \
        g++ \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ──────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .

# PyTorch CPU-only wheel (avoids pulling 2 GB CUDA packages)
RUN pip install --no-cache-dir \
        torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# ── Pipeline code ────────────────────────────────────────────────────────────
COPY project/   ./project/
COPY aws/       ./aws/

# U-Net model weights (5.7 MB — trained dot detector)
COPY unet_best.pth .

# ── Runtime config ───────────────────────────────────────────────────────────
ENV PYTHONPATH=/app/project \
    OUTPUT_ROOT=/tmp/output \
    USE_VISION_API=0 \
    GEMINI_MIN_CALL_GAP_S=3.0 \
    PYTHONUNBUFFERED=1

# ── Entrypoint ───────────────────────────────────────────────────────────────
# Default: pipeline job. Override CMD for enrich/mapbuild jobs.
ENTRYPOINT ["python"]
CMD ["aws/run_batch_job.py"]

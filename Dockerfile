## ============================================================
## App image  (code-only layer — rebuilds in ~10 seconds)
##
## FROM references the base image (apt + pip + torch).
## Base is rebuilt ONLY when requirements.txt or torch version changes.
## Use aws/build_push.sh which handles everything automatically.
## ============================================================
FROM 225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline-base:latest

LABEL org.opencontainers.image.title="osu-well-pipeline" \
      org.opencontainers.image.description="Oklahoma well-record PDF processing pipeline"

WORKDIR /app

# Project source — last so code edits only invalidate this one layer
COPY project              /app
COPY aws/run_batch_job.py /app/run_batch_job.py

# U-Net detector + checkpoint — baked in; Batch workers need no S3 access for them
COPY unet_dot_detector.py /app/unet_dot_detector.py
COPY unet_best.pth        /app/unet_best.pth

# Runtime env — actual secrets injected at container start by run_batch_job.py
ENV UNET_CHECKPOINT=/app/unet_best.pth \
    OUTPUT_ROOT=/tmp/output

# Required at runtime (injected by Batch / docker run — never baked in):
#   INPUT_BUCKET, OUTPUT_BUCKET, INDEX_KEY
#   GOOGLE_CREDS_SECRET_ID, RDS_CREDS_SECRET_ID

ENTRYPOINT ["python", "/app/run_batch_job.py"]

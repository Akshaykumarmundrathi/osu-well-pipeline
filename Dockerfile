FROM python:3.11-slim

# OpenCV + PyMuPDF need a couple of native libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY project /app
COPY aws/run_batch_job.py /app/run_batch_job.py

# AWS Batch will set these at job time; sensible defaults for local debug.
ENV AWS_DEFAULT_REGION=us-east-1
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/run_batch_job.py"]

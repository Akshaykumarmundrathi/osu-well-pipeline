#!/usr/bin/env python3
"""
Local Pipeline Processor - FREE Alternative to AWS Batch

Process PDFs locally, upload results to S3.
Cost: $0 AWS compute + minimal S3 storage costs
"""

import boto3
import json
import os
import sys
from pathlib import Path
from datetime import datetime
import subprocess

S3_INPUT = 'osu-well-records-225989338968'
S3_OUTPUT = 'osu-pipeline-results'
LOCAL_WORK = Path('/d/project_modular/.work')
BATCH_SIZE = 50  # Process 50 PDFs at a time locally

s3 = boto3.client('s3', region_name='us-east-1')

print("="*70)
print("LOCAL PIPELINE PROCESSOR - FREE TIER")
print("="*70)
print()
print("COST: $0 AWS compute (uses your local CPU/GPU)")
print("      Uses S3 free tier for upload (5GB free/month)")
print()

# Create work directory
LOCAL_WORK.mkdir(parents=True, exist_ok=True)

def get_collection_index():
    """Download and parse collection index"""
    print("[1] Downloading collection index from S3...")

    index_key = 'collections_index.json'
    index_path = LOCAL_WORK / index_key

    try:
        s3.download_file(S3_INPUT, index_key, str(index_path))
        with open(index_path) as f:
            index = json.load(f)
        print(f"    OK: {len(index)} collections indexed")
        return index
    except Exception as e:
        print(f"    ERROR: {e}")
        return None

def process_batch_locally(slice_num, batch_size=50):
    """Process a batch of PDFs locally"""
    print(f"\n[PROCESSING] Slice {slice_num} ({batch_size} PDFs)")
    print("-"*70)

    output_dir = LOCAL_WORK / f'slice-{slice_num:05d}'
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run the pipeline's main processing function
    try:
        # Import and call main processing logic
        from project import main as pipeline_main

        # Process this slice
        # (Pseudocode - adapt based on actual main.py signature)
        result = pipeline_main.process_slice(
            slice_num=slice_num,
            slice_size=batch_size,
            input_bucket=S3_INPUT,
            output_bucket=S3_OUTPUT,
            output_dir=str(output_dir),
            local_mode=True
        )

        print(f"    Wells extracted: {result.get('well_count', 'N/A')}")
        print(f"    Processing time: {result.get('duration_sec', 'N/A')}s")

        return output_dir, result

    except Exception as e:
        print(f"    ERROR: {e}")
        return output_dir, None

def upload_results_to_s3(slice_num, output_dir):
    """Upload processed results to S3"""
    print(f"    Uploading results to S3...")

    try:
        for file in output_dir.glob('*'):
            s3_key = f'results/slice-{slice_num:05d}/{file.name}'
            s3.upload_file(str(file), S3_OUTPUT, s3_key)

        print(f"    ✓ Uploaded to s3://{S3_OUTPUT}/results/slice-{slice_num:05d}/")

    except Exception as e:
        print(f"    ERROR uploading: {e}")

def main():
    """Main processing loop"""

    # Get collection index
    index = get_collection_index()
    if not index:
        print("ERROR: Could not load collection index")
        sys.exit(1)

    print()
    print("[2] STARTING LOCAL PROCESSING")
    print("="*70)
    print(f"Total slices to process: 391")
    print(f"Batch size: {BATCH_SIZE} PDFs")
    print(f"Estimated slices: {391}")
    print(f"Estimated runtime: depends on your hardware")
    print()

    # Track progress
    processed = 0
    start_time = datetime.now()

    for slice_num in range(1, 392):

        # Process this slice locally
        output_dir, result = process_batch_locally(slice_num, BATCH_SIZE)

        if result:
            # Upload to S3
            upload_results_to_s3(slice_num, output_dir)
            processed += 1

        # Progress update
        if slice_num % 50 == 0:
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = processed / (elapsed / 3600) if elapsed > 0 else 0
            eta_hours = (391 - processed) / (rate if rate > 0 else 1)

            print()
            print(f"Progress: {slice_num}/391 processed ({processed} successful)")
            print(f"  Elapsed: {elapsed/3600:.1f}h | Rate: {rate:.1f} slices/h | ETA: {eta_hours:.1f}h")
            print()

    print()
    print("="*70)
    print(f"COMPLETE: {processed} / 391 slices processed")
    print(f"Total time: {(datetime.now() - start_time).total_seconds()/3600:.1f} hours")
    print()
    print("Results uploaded to: s3://osu-pipeline-results/results/")
    print()
    print("[NEXT] Run: python3 visualizer/auto_refresh_map.py")
    print("       to aggregate results and update CSV")

if __name__ == '__main__':
    # Check if main.py exists
    if not Path('/d/project_modular/project/main.py').exists():
        print("ERROR: project/main.py not found")
        print("Copy the actual pipeline code to use local processor")
        sys.exit(1)

    main()

"""
deploy_viewer.py
----------------
Deploys the well map viewer to S3 as a public static website.

Uploads:
  well_map.html     → s3://osu-well-records-225989338968/viewer/well_map.html
  well_locations.json (if present locally)

Sets public-read ACL on all viewer files.
Prints the public URL when done.

Usage:
  python deploy_viewer.py                    # deploy HTML only
  python deploy_viewer.py --with-data        # deploy HTML + run data merge first
"""

import boto3, os, sys, subprocess

BUCKET   = "osu-well-records-225989338968"
REGION   = "us-east-1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = [
    ("well_map.html",        "text/html",             "viewer/well_map.html"),
    ("well_locations.json",  "application/json",      "viewer/well_locations.json"),
    ("well_locations.csv",   "text/csv",              "viewer/well_locations.csv"),
]

BUCKET_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid":       "PublicReadViewer",
        "Effect":    "Allow",
        "Principal": "*",
        "Action":    "s3:GetObject",
        "Resource":  f"arn:aws:s3:::{BUCKET}/viewer/*"
    }]
}


def ensure_public_policy(s3):
    """Add/update bucket policy to allow public read on /viewer/ prefix."""
    import json
    try:
        existing = json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"])
        stmts = existing.get("Statement", [])
    except Exception:
        stmts = []

    # Check if our statement already exists
    if any(s.get("Sid") == "PublicReadViewer" for s in stmts):
        print("  Bucket policy already includes PublicReadViewer statement.")
        return

    stmts.append(BUCKET_POLICY["Statement"][0])
    new_policy = {"Version": "2012-10-17", "Statement": stmts}

    # Disable block public access for the bucket if needed
    try:
        s3.put_public_access_block(
            Bucket=BUCKET,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False
            }
        )
        print("  Updated public access block settings.")
    except Exception as e:
        print(f"  Warning: could not update public access block: {e}")

    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(new_policy))
    print("  Bucket policy updated with PublicReadViewer.")


def upload_file(s3, local_path, s3_key, content_type):
    if not os.path.exists(local_path):
        print(f"  Skipping {local_path} (not found)")
        return False
    size = os.path.getsize(local_path)
    with open(local_path, "rb") as f:
        s3.put_object(
            Bucket=BUCKET, Key=s3_key, Body=f,
            ContentType=content_type
        )
    print(f"  ✅ {s3_key}  ({size//1024} KB)")
    return True


def main():
    with_data = "--with-data" in sys.argv

    if with_data:
        print("=== Step 1: Merging well location data from S3 ===")
        merge_script = os.path.join(BASE_DIR, "merge_well_locations.py")
        result = subprocess.run([sys.executable, merge_script], cwd=BASE_DIR)
        if result.returncode != 0:
            print("ERROR: merge_well_locations.py failed. Aborting.")
            sys.exit(1)

    print("\n=== Deploying viewer to S3 ===")
    s3 = boto3.client("s3", region_name=REGION)

    ensure_public_policy(s3)

    for fname, ctype, s3_key in FILES:
        local = os.path.join(BASE_DIR, fname)
        upload_file(s3, local, s3_key, ctype)

    url = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/viewer/well_map.html"
    print(f"\n🗺️  Map viewer live at:\n   {url}\n")
    return url


if __name__ == "__main__":
    main()

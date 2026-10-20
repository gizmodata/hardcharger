"""Write a manifest of presigned GET URLs for one staged TPC-H scale factor.

    HARDCHARGER_BENCH_DATA_BUCKET=<bucket> python presign.py --scale-factor 1000 --output urls.tsv \
        [--expires-seconds 21600] [--bucket <bucket>] [--region <region>]

The data is staged by gizmodata/tpch-datagen in s3://<bucket>/tpch/sf=<N>/<table>/*.parquet (a
private bucket). The bucket comes from --bucket or HARDCHARGER_BENCH_DATA_BUCKET (one is required);
the region from --region or HARDCHARGER_BENCH_DATA_REGION (default us-east-2). The VM gets these
read-only, per-object, expiring URLs and never any AWS credentials. Each line:
<table>/<file>\t<size in bytes>\t<url>. The URLs are never printed; the file is written with mode
0600.
"""

import argparse
import os

import boto3

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale-factor", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expires-seconds", type=int, default=6 * 3600)
    parser.add_argument("--bucket", default=os.environ.get("HARDCHARGER_BENCH_DATA_BUCKET"))
    parser.add_argument("--region", default=os.environ.get("HARDCHARGER_BENCH_DATA_REGION", "us-east-2"))
    args = parser.parse_args()
    if not args.bucket:
        parser.error("set HARDCHARGER_BENCH_DATA_BUCKET (or pass --bucket): the S3 bucket the data is staged in")
    bucket = args.bucket

    s3 = boto3.client("s3", region_name=args.region)
    prefix = f"tpch/sf={args.scale_factor}/"
    lines = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            relative = item["Key"][len(prefix):]
            if not relative.endswith(".parquet"):
                continue
            url = s3.generate_presigned_url(ClientMethod="get_object",
                                            Params={"Bucket": bucket, "Key": item["Key"]},
                                            ExpiresIn=args.expires_seconds)
            lines.append(f"{relative}\t{item['Size']}\t{url}\n")
    if not lines:
        raise SystemExit(f"no parquet files under s3://{bucket}/{prefix}")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.writelines(sorted(lines))
    total = sum(int(line.split("\t")[1]) for line in lines)
    print(f"{len(lines)} files, {total / 1e9:.1f} GB, URLs valid {args.expires_seconds // 3600} h")


if __name__ == "__main__":
    main()

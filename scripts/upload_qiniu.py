#!/usr/bin/env python3
import argparse
import glob
import os
import time


def fail(message: str, code: int = 1) -> None:
    print(f"Error: {message}")
    raise SystemExit(code)


def clean_env(name: str) -> str:
    return os.environ.get(name, "").strip().strip("'\"").strip()


def resolve_file_path(file_path: str, glob_pattern: str) -> str:
    if file_path:
        if not os.path.exists(file_path):
            fail(f"File not found: {file_path}")
        return file_path

    matches = sorted(glob.glob(glob_pattern or ""))
    if not matches:
        fail(f"No file matched glob: {glob_pattern}")
    if len(matches) > 1:
        fail(f"Multiple files matched glob {glob_pattern}: {', '.join(matches)}")
    return matches[0]


def build_key(file_path: str, explicit_key: str, prefix: str) -> str:
    if explicit_key:
        return explicit_key
    basename = os.path.basename(file_path)
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/{basename}" if prefix else basename


def compute_part_size(file_path: str) -> int:
    file_size = os.path.getsize(file_path)
    if file_size < 512 * 1024 * 1024:
        return 8 * 1024 * 1024
    return 16 * 1024 * 1024


def upload_once(ak: str, sk: str, bucket: str, region_id: str, file_path: str, key: str, expires: int, connection_timeout: int, connection_retries: int):
    try:
        from qiniu import Auth, Zone, put_file_v2, set_default
        from qiniu.region import LegacyRegion
    except Exception as exc:
        fail(f"Failed to import qiniu SDK: {exc}")

    q = Auth(ak, sk)
    region = Zone.from_region_id(region_id)
    set_default(
        default_zone=LegacyRegion(scheme="https"),
        connection_timeout=connection_timeout,
        connection_retries=connection_retries,
    )
    token = q.upload_token(bucket, key, expires)
    part_size = compute_part_size(file_path)
    return put_file_v2(
        token,
        key,
        file_path,
        version="v2",
        bucket_name=bucket,
        part_size=part_size,
        regions=[region],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a file to Qiniu Kodo with retries.")
    parser.add_argument("--file", default="", help="Explicit file path to upload.")
    parser.add_argument("--glob", default="", help="Glob pattern used when --file is not provided.")
    parser.add_argument("--key", default="", help="Target object key in Qiniu.")
    parser.add_argument("--prefix", default="releases", help="Key prefix used when --key is omitted.")
    parser.add_argument("--bucket", default="", help="Bucket name. Defaults to QINIU_BUCKET env.")
    parser.add_argument("--region", default="z2", help="Qiniu region id. Defaults to z2.")
    parser.add_argument("--expires", type=int, default=36000, help="Upload token expiry seconds.")
    parser.add_argument("--max-retries", type=int, default=5, help="Maximum upload attempts.")
    parser.add_argument("--retry-base-seconds", type=int, default=15, help="Base seconds between retries.")
    parser.add_argument("--retry-max-seconds", type=int, default=45, help="Maximum seconds between retries.")
    parser.add_argument("--connection-timeout", type=int, default=120, help="Qiniu SDK connection timeout seconds.")
    parser.add_argument("--connection-retries", type=int, default=5, help="Qiniu SDK connection retries.")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs without uploading.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ak = clean_env("QINIU_ACCESS_KEY")
    sk = clean_env("QINIU_SECRET_KEY")
    bucket = (args.bucket or clean_env("QINIU_BUCKET")).strip()

    print(f"Debug: AK length={len(ak)}, SK length={len(sk)}")

    if not ak or not sk or not bucket:
        fail("Missing Qiniu credentials or bucket. Check QINIU_ACCESS_KEY / QINIU_SECRET_KEY / QINIU_BUCKET.")

    if not args.file and not args.glob:
        fail("Either --file or --glob must be provided.")

    file_path = resolve_file_path(args.file, args.glob)
    key = build_key(file_path, args.key, args.prefix)

    print(f"Resolved file: {file_path}")
    print(f"Resolved key: {key}")
    print(f"Resolved region: {args.region}")

    if args.dry_run:
        print("Dry run passed.")
        return

    print(f"Uploading {os.path.basename(file_path)} to Qiniu bucket: {bucket} ...")

    for attempt in range(1, args.max_retries + 1):
        print(f"Attempt {attempt}/{args.max_retries}...")
        try:
            ret, info = upload_once(
                ak=ak,
                sk=sk,
                bucket=bucket,
                region_id=args.region,
                file_path=file_path,
                key=key,
                expires=args.expires,
                connection_timeout=args.connection_timeout,
                connection_retries=args.connection_retries,
            )
        except Exception as exc:
            ret, info = None, None
            print(f"Upload exception: {exc}")

        status_code = getattr(info, "status_code", None)
        if status_code == 200 and ret is not None:
            print("Upload successful!")
            return

        print(f"Upload failed: {info or 'no response info'}")
        if attempt < args.max_retries:
            sleep_seconds = min(args.retry_base_seconds * attempt, args.retry_max_seconds)
            print(f"Retrying in {sleep_seconds} seconds...")
            time.sleep(sleep_seconds)

    fail("Max retries reached. Upload failed.")


if __name__ == "__main__":
    main()

import argparse
import calendar
import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen, urlretrieve

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from auth import init_client


def bucket_exists(bucket_name: str) -> bool:
    """Return whether an S3 bucket exists using credentials from auth.py."""
    s3_client = init_client()

    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error_code in {"404", "NoSuchBucket"} or status_code == 404:
            return False
        raise

    return True


def set_bucket_versioning(s3_client, bucket_name: str, enabled: bool) -> None:
    """Enable or suspend versioning for an S3 bucket."""
    status = "Enabled" if enabled else "Suspended"
    s3_client.put_bucket_versioning(
        Bucket=bucket_name,
        VersioningConfiguration={"Status": status},
    )


def get_bucket_versioning_status(s3_client, bucket_name: str) -> str:
    """Return the bucket's versioning status, including when it was never enabled."""
    return s3_client.get_bucket_versioning(Bucket=bucket_name).get(
        "Status", "Never enabled"
    )


def grant_public_read(s3_client, bucket_name: str, object_key: str | None = None) -> None:
    """Grant public download access to all bucket objects or one object."""
    resource = (
        f"arn:aws:s3:::{bucket_name}/{object_key}"
        if object_key is not None
        else f"arn:aws:s3:::{bucket_name}/*"
    )
    statement_id = "PublicRead" + hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]
    statement = {
        "Sid": statement_id,
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": resource,
    }

    try:
        policy = json.loads(s3_client.get_bucket_policy(Bucket=bucket_name)["Policy"])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
            raise
        policy = {"Version": "2012-10-17", "Statement": []}

    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    statements = [
        existing for existing in statements if existing.get("Sid") != statement_id
    ]
    statements.append(statement)
    policy["Statement"] = statements

    s3_client.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))


def read_bucket_policy(s3_client, bucket_name: str) -> dict:
    """Return the bucket policy as a JSON object."""
    return json.loads(s3_client.get_bucket_policy(Bucket=bucket_name)["Policy"])


def disable_bucket_public_access_block(s3_client, bucket_name: str) -> None:
    """Disable all bucket-level S3 Block Public Access settings."""
    s3_client.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )


def list_objects(s3_client, bucket_name: str) -> list[str]:
    """Return all object keys in an S3 bucket."""
    paginator = s3_client.get_paginator("list_objects_v2")
    return [
        item["Key"]
        for page in paginator.paginate(Bucket=bucket_name)
        for item in page.get("Contents", [])
    ]


def group_objects_by_extension(s3_client, bucket_name: str) -> int:
    """Move bucket objects under key prefixes named for their file extensions."""
    object_keys = list_objects(s3_client, bucket_name)
    existing_keys = set(object_keys)
    moves = []

    for source_key in object_keys:
        if source_key.endswith("/"):
            continue

        extension = PurePosixPath(source_key).suffix.lower()
        folder = extension[1:] if extension else "no-extension"
        if source_key.split("/", 1)[0] == folder:
            continue

        destination_key = f"{folder}/{source_key}"
        if destination_key in existing_keys:
            raise ValueError(
                f"Cannot move '{source_key}': destination "
                f"'{destination_key}' already exists."
            )
        moves.append((source_key, destination_key))

    for source_key, destination_key in moves:
        s3_client.copy_object(
            Bucket=bucket_name,
            CopySource={"Bucket": bucket_name, "Key": source_key},
            Key=destination_key,
        )
        s3_client.delete_object(Bucket=bucket_name, Key=source_key)

    return len(moves)


def cleanup_old_object_versions(s3_client, bucket_name: str, object_key: str) -> int:
    """Delete noncurrent object versions and delete markers older than six months."""
    now = datetime.now(timezone.utc)
    target_month = now.month - 6
    target_year = now.year
    while target_month <= 0:
        target_month += 12
        target_year -= 1
    cutoff_day = min(now.day, calendar.monthrange(target_year, target_month)[1])
    cutoff = now.replace(year=target_year, month=target_month, day=cutoff_day)

    paginator = s3_client.get_paginator("list_object_versions")
    old_versions = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=object_key):
        for field in ("Versions", "DeleteMarkers"):
            for version in page.get(field, []):
                if (
                    version["Key"] == object_key
                    and not version["IsLatest"]
                    and version["LastModified"] < cutoff
                ):
                    old_versions.append(version["VersionId"])

    for version_id in old_versions:
        s3_client.delete_object(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
        )

    return len(old_versions)


def get_object_versions(s3_client, bucket_name: str, object_key: str) -> list[dict]:
    """Return all versions and delete markers for an exact object key."""
    paginator = s3_client.get_paginator("list_object_versions")
    versions = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=object_key):
        for field, is_delete_marker in (("Versions", False), ("DeleteMarkers", True)):
            for entry in page.get(field, []):
                if entry["Key"] != object_key:
                    continue
                version = {
                    "VersionId": entry["VersionId"],
                    "IsLatest": entry["IsLatest"],
                    "LastModified": entry["LastModified"].isoformat(),
                    "IsDeleteMarker": is_delete_marker,
                }
                if not is_delete_marker:
                    version["Size"] = entry["Size"]
                versions.append(version)
    return versions


def recover_previous_object_version(s3_client, bucket_name: str, object_key: str) -> str:
    """Copy the newest noncurrent data version to create a new latest version."""
    status = get_bucket_versioning_status(s3_client, bucket_name)
    if status != "Enabled":
        raise ValueError(
            f"Cannot recover a previous version while bucket versioning is {status.lower()}."
        )

    paginator = s3_client.get_paginator("list_object_versions")
    previous_versions = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=object_key):
        previous_versions.extend(
            version
            for version in page.get("Versions", [])
            if version["Key"] == object_key and not version["IsLatest"]
        )

    if not previous_versions:
        raise ValueError(f"No previous data version found for object '{object_key}'.")

    previous_version = max(previous_versions, key=lambda version: version["LastModified"])
    response = s3_client.copy_object(
        Bucket=bucket_name,
        CopySource={
            "Bucket": bucket_name,
            "Key": object_key,
            "VersionId": previous_version["VersionId"],
        },
        Key=object_key,
    )
    return response.get("VersionId", "")


def list_buckets(s3_client) -> list[str]:
    """Return the names of all S3 buckets accessible to the account."""
    return [bucket["Name"] for bucket in s3_client.list_buckets().get("Buckets", [])]


def create_bucket(s3_client, bucket_name: str) -> None:
    """Create an S3 bucket in the client's configured region."""
    region = s3_client.meta.region_name
    if region and region != "us-east-1":
        s3_client.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    else:
        s3_client.create_bucket(Bucket=bucket_name)


def create_static_website(s3_client, bucket_name: str, index_file: str) -> None:
    """Create a public S3 website bucket with a local file as its index page."""
    file_path = Path(index_file)
    if not file_path.is_file():
        raise FileNotFoundError(f"Index file does not exist or is not a file: {file_path}")

    create_bucket(s3_client, bucket_name)
    disable_bucket_public_access_block(s3_client, bucket_name)
    s3_client.upload_file(
        str(file_path),
        bucket_name,
        "index.html",
        ExtraArgs={"ContentType": "text/html"},
    )
    s3_client.put_bucket_website(
        Bucket=bucket_name,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
    )
    grant_public_read(s3_client, bucket_name, "index.html")


def create_static_website_from_source(s3_client, source_url: str) -> tuple[str, str]:
    """Create a public S3 website from a public GitHub repository archive."""
    parsed_url = urlsplit(source_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.port is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("Source must be a public GitHub repository URL over HTTPS.")

    repository_parts = [part for part in parsed_url.path.strip("/").split("/") if part]
    if len(repository_parts) != 2:
        raise ValueError("Source URL must have the form https://github.com/OWNER/REPOSITORY.")
    owner, repository = repository_parts
    repository = repository.removesuffix(".git")
    if not owner or not repository:
        raise ValueError("Source URL must include a GitHub owner and repository.")

    headers = {"User-Agent": "boto3-tool-static-site"}
    api_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repository)}"
    with urlopen(Request(api_url, headers=headers), timeout=30) as response:
        repository_info = json.load(response)
    default_branch = repository_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("Could not determine the repository's default branch.")

    archive_url = (
        f"https://api.github.com/repos/{quote(owner)}/{quote(repository)}"
        f"/zipball/{quote(default_branch, safe='')}"
    )
    with tempfile.TemporaryDirectory(prefix="boto3-tool-site-") as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / "source.zip"
        with urlopen(Request(archive_url, headers=headers), timeout=60) as response:
            with archive_path.open("wb") as archive_file:
                shutil.copyfileobj(response, archive_file)

        website_root = temp_path / "website"
        website_root.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            roots = {
                PurePosixPath(entry.filename).parts[0]
                for entry in entries
                if PurePosixPath(entry.filename).parts
            }
            if len(roots) != 1:
                raise ValueError("GitHub source archive has an unexpected directory layout.")
            archive_root = next(iter(roots))

            for entry in entries:
                archive_path_part = PurePosixPath(entry.filename)
                if (
                    archive_path_part.is_absolute()
                    or ".." in archive_path_part.parts
                    or not archive_path_part.parts
                    or archive_path_part.parts[0] != archive_root
                ):
                    raise ValueError("GitHub source archive contains an unsafe file path.")
                if entry.is_dir():
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("GitHub source archive contains a symbolic link.")

                relative_parts = archive_path_part.parts[1:]
                if not relative_parts:
                    continue
                destination = website_root.joinpath(*relative_parts)
                if not destination.resolve().is_relative_to(website_root.resolve()):
                    raise ValueError("GitHub source archive contains an unsafe file path.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source_file, destination.open("wb") as output_file:
                    shutil.copyfileobj(source_file, output_file)

        if not (website_root / "index.html").is_file():
            raise ValueError("GitHub repository must contain an index.html at its root.")

        region = s3_client.meta.region_name
        if not region:
            raise ValueError("AWS region is not configured; cannot create a website endpoint.")
        safe_repository = re.sub(r"[^a-z0-9-]", "-", repository.lower()).strip("-")
        safe_repository = safe_repository or "site"
        bucket_name = f"static-{safe_repository[:25]}-{secrets.token_hex(8)}"

        create_bucket(s3_client, bucket_name)
        disable_bucket_public_access_block(s3_client, bucket_name)
        for file_path in website_root.rglob("*"):
            if not file_path.is_file():
                continue
            object_key = file_path.relative_to(website_root).as_posix()
            content_type, _ = mimetypes.guess_type(object_key)
            extra_args = {"ContentType": content_type or "application/octet-stream"}
            s3_client.upload_file(
                str(file_path),
                bucket_name,
                object_key,
                ExtraArgs=extra_args,
            )

        s3_client.put_bucket_website(
            Bucket=bucket_name,
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        )
        grant_public_read(s3_client, bucket_name)

    if region.startswith("cn-"):
        website_url = f"http://{bucket_name}.s3-website.{region}.amazonaws.com.cn"
    else:
        website_url = f"http://{bucket_name}.s3-website-{region}.amazonaws.com"
    return bucket_name, website_url


def total_size_by_extension(s3_client, bucket_name: str) -> dict[str, int]:
    """Return the total object size in bytes grouped by file extension."""
    sizes: dict[str, int] = defaultdict(int)
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket_name):
        for item in page.get("Contents", []):
            extension = PurePosixPath(item["Key"]).suffix.lower() or "(no extension)"
            sizes[extension] += item["Size"]

    return dict(sizes)


def upload_local_file(
    s3_client, bucket_name: str, local_file: str, magic_group: bool = False
) -> str:
    """Upload a local file, optionally grouping it by detected MIME type."""
    file_path = Path(local_file)
    if not file_path.is_file():
        raise FileNotFoundError(f"Local file does not exist or is not a file: {file_path}")

    object_key = file_path.name
    if magic_group:
        import magic

        mime_type = magic.from_file(str(file_path), mime=True)
        if not isinstance(mime_type, str) or "/" not in mime_type:
            raise ValueError(f"Could not determine a valid MIME type for '{file_path}'.")
        object_key = f"{mime_type}/{object_key}"

    s3_client.upload_file(str(file_path), bucket_name, object_key)
    return object_key


def download_file(url: str) -> Path:
    """Download a URL to the current directory using its filename."""
    filename = PurePosixPath(unquote(urlsplit(url).path)).name
    if filename in {"", ".", ".."}:
        raise ValueError("The URL must include a filename in its path.")

    destination = Path(filename)
    urlretrieve(url, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authenticate with S3 or manage buckets and objects."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--list-buckets",
        action="store_true",
        help="List all S3 buckets accessible to the account",
    )
    action.add_argument(
        "--create-bucket",
        metavar="BUCKET_NAME",
        help="Create an S3 bucket in the configured region",
    )
    action.add_argument(
        "--check-bucket",
        metavar="BUCKET_NAME",
        help="Check whether the specified S3 bucket exists",
    )
    action.add_argument(
        "--read-bucket-policy",
        metavar="BUCKET_NAME",
        help="Read the policy for the specified S3 bucket",
    )
    action.add_argument(
        "--disable-block-public",
        metavar="BUCKET_NAME",
        help="Disable all bucket-level S3 Block Public Access settings",
    )
    action.add_argument(
        "--delete-bucket",
        metavar="BUCKET_NAME",
        help="Delete the specified empty S3 bucket",
    )
    action.add_argument(
        "--delete-object",
        nargs=2,
        metavar=("BUCKET_NAME", "OBJECT_KEY"),
        help="Delete the specified object from an S3 bucket",
    )
    action.add_argument(
        "--enable-versioning",
        metavar="BUCKET_NAME",
        help="Enable versioning for the specified S3 bucket",
    )
    action.add_argument(
        "--disable-versioning",
        metavar="BUCKET_NAME",
        help="Suspend versioning for the specified S3 bucket",
    )
    action.add_argument(
        "--public-read-bucket",
        metavar="BUCKET_NAME",
        help="Allow anyone to download objects in the specified bucket",
    )
    action.add_argument(
        "--public-read-object",
        nargs=2,
        metavar=("BUCKET_NAME", "OBJECT_KEY"),
        help="Allow anyone to download the specified object",
    )
    action.add_argument(
        "--list-object",
        metavar="BUCKET_NAME",
        help="List all object keys in the specified bucket",
    )
    action.add_argument(
        "--size-by-extension",
        metavar="BUCKET_NAME",
        help="Show total object size grouped by file extension",
    )
    parser.add_argument(
        "--bucket",
        metavar="BUCKET_NAME",
        help="Bucket to organize or clean up versions in",
    )
    parser.add_argument(
        "--group-by-extension",
        type=str.lower,
        choices=("true", "false"),
        metavar="TRUE|FALSE",
        help="Move bucket objects into extension-based prefixes",
    )
    parser.add_argument(
        "--cleanup-versions",
        action="store_true",
        help="Delete noncurrent object versions and delete markers older than six months",
    )
    parser.add_argument(
        "--bucket-versioning-info",
        action="store_true",
        help="Check whether versioning is enabled for --bucket",
    )
    parser.add_argument(
        "--object-versioning-info",
        action="store_true",
        help="List all versions and delete markers for --object in --bucket",
    )
    parser.add_argument(
        "--recover-previous",
        action="store_true",
        help="Restore the newest noncurrent object version as a new latest version",
    )
    parser.add_argument(
        "--object",
        dest="object_key",
        metavar="OBJECT_KEY",
        help="Object key to use with version information or cleanup actions",
    )
    parser.add_argument(
        "--static-web",
        action="store_true",
        help="Create a static website bucket using the file supplied with --index",
    )
    parser.add_argument(
        "--index",
        dest="index_file",
        metavar="INDEX_FILE",
        help="Local HTML file to upload as the website's index.html",
    )
    parser.add_argument(
        "--static-from-source",
        metavar="GITHUB_URL",
        help="Create and host a static website from a public GitHub repository",
    )
    parser.add_argument(
        "--download-file",
        metavar="URL",
        help="Download a file from a URL into the current directory",
    )
    parser.add_argument(
        "--upload-file",
        nargs="+",
        metavar="ARGUMENT",
        help=(
            "Upload BUCKET_NAME LOCAL_FILE, or pair with --download-file and provide "
            "BUCKET_NAME"
        ),
    )
    parser.add_argument(
        "--magic-group",
        action="store_true",
        help="Group an uploaded file under its detected MIME type in S3",
    )
    args = parser.parse_args()

    other_actions = any(
        (
            args.list_buckets,
            args.create_bucket,
            args.check_bucket,
            args.read_bucket_policy,
            args.disable_block_public,
            args.delete_bucket,
            args.delete_object,
            args.enable_versioning,
            args.disable_versioning,
            args.public_read_bucket,
            args.public_read_object,
            args.list_object,
            args.size_by_extension,
        )
    )
    if args.upload_file:
        expected_upload_arguments = 1 if args.download_file else 2
        if len(args.upload_file) != expected_upload_arguments:
            parser.error(
                "--upload-file requires BUCKET_NAME LOCAL_FILE, or just BUCKET_NAME "
                "when used with --download-file"
            )
    elif args.magic_group:
        parser.error("--magic-group can only be used with --upload-file")
    if other_actions and (args.download_file or args.upload_file):
        parser.error("--download-file and --upload-file cannot be combined with other actions")

    cleanup_requested = args.cleanup_versions
    if cleanup_requested:
        if not args.cleanup_versions or not args.bucket or not args.object_key:
            parser.error(
                "--cleanup-versions requires both --bucket BUCKET_NAME "
                "and --object OBJECT_KEY"
            )
        if (
            other_actions
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
        ):
            parser.error("--cleanup-versions cannot be combined with other actions")

    object_version_info_requested = args.object_versioning_info
    if object_version_info_requested:
        if not args.bucket or not args.object_key:
            parser.error(
                "--object-versioning-info requires --bucket BUCKET_NAME "
                "and --object OBJECT_KEY"
            )
        if (
            other_actions
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
            or args.cleanup_versions
            or args.bucket_versioning_info
        ):
            parser.error("--object-versioning-info cannot be combined with other actions")

    recover_requested = args.recover_previous
    if recover_requested:
        if not args.bucket or not args.object_key:
            parser.error(
                "--recover-previous requires both --bucket BUCKET_NAME "
                "and --object OBJECT_KEY"
            )
        if (
            other_actions
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
            or args.cleanup_versions
            or args.bucket_versioning_info
            or args.object_versioning_info
        ):
            parser.error("--recover-previous cannot be combined with other actions")

    if args.object_key is not None and not (
        args.cleanup_versions or args.object_versioning_info or args.recover_previous
    ):
        parser.error(
            "--object requires --cleanup-versions, --object-versioning-info, "
            "or --recover-previous"
        )

    static_web_requested = args.static_web or args.index_file is not None
    if static_web_requested:
        if not args.static_web or not args.bucket or not args.index_file:
            parser.error("--static-web requires --bucket BUCKET_NAME and --index INDEX_FILE")
        if (
            other_actions
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
            or args.cleanup_versions
            or args.bucket_versioning_info
            or args.object_versioning_info
            or args.recover_previous
            or args.object_key is not None
        ):
            parser.error("--static-web cannot be combined with other actions")

    static_source_requested = args.static_from_source is not None
    if static_source_requested:
        if (
            other_actions
            or args.bucket
            or args.index_file
            or args.static_web
            or args.object_key is not None
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
            or args.cleanup_versions
            or args.bucket_versioning_info
            or args.object_versioning_info
            or args.recover_previous
        ):
            parser.error("--static-from-source cannot be combined with other actions")

    if args.bucket_versioning_info:
        if not args.bucket:
            parser.error("--bucket-versioning-info requires --bucket BUCKET_NAME")
        if (
            other_actions
            or args.download_file
            or args.upload_file
            or args.magic_group
            or args.group_by_extension is not None
            or cleanup_requested
            or recover_requested
        ):
            parser.error("--bucket-versioning-info cannot be combined with other actions")

    grouping_requested = (
        args.group_by_extension is not None
        or (
            args.bucket is not None
            and not cleanup_requested
            and not args.bucket_versioning_info
            and not object_version_info_requested
            and not recover_requested
            and not static_web_requested
            and not static_source_requested
        )
    )
    if grouping_requested:
        if args.bucket is None or args.group_by_extension is None:
            parser.error("--bucket and --group-by-extension must be used together")
        if other_actions or args.download_file or args.upload_file or args.magic_group:
            parser.error("--bucket/--group-by-extension cannot be combined with other actions")
        if args.group_by_extension == "false":
            print("Grouping by extension is disabled; no objects were changed.")
            return

    if args.download_file and not args.upload_file:
        try:
            downloaded_file = download_file(args.download_file)
        except (OSError, URLError, ValueError) as error:
            parser.exit(1, f"Could not download '{args.download_file}': {error}\n")
        print(f"Downloaded '{args.download_file}' to '{downloaded_file}'.")
        return

    if not any(
        (
            args.list_buckets,
            args.create_bucket,
            args.check_bucket,
            args.read_bucket_policy,
            args.disable_block_public,
            args.delete_bucket,
            args.delete_object,
            args.enable_versioning,
            args.disable_versioning,
            args.public_read_bucket,
            args.public_read_object,
            args.list_object,
            args.size_by_extension,
            args.upload_file,
            args.download_file,
            args.magic_group,
            args.bucket,
            args.group_by_extension,
            args.cleanup_versions,
            args.bucket_versioning_info,
            args.object_versioning_info,
            args.recover_previous,
            args.object_key,
            args.static_web,
            args.index_file,
            args.static_from_source,
        )
    ):
        init_client()
        return

    if args.check_bucket:
        try:
            exists = bucket_exists(args.check_bucket)
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "Unknown")
            status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if (
                error_code in {"403", "AccessDenied", "BucketAlreadyExists"}
                or status_code == 403
            ):
                parser.exit(
                    1,
                    f"Cannot access bucket '{args.check_bucket}'. Its name may already be "
                    "in use by another AWS account, or your credentials may lack permission.\n",
                )
            parser.exit(
                1,
                f"Could not check bucket '{args.check_bucket}' "
                f"(AWS error {error_code}): {error}\n",
            )

        if exists:
            print(f"Bucket '{args.check_bucket}' exists.")
        else:
            print(f"Bucket '{args.check_bucket}' does not exist.")
        return

    s3_client = init_client()
    try:
        if args.static_from_source is not None:
            bucket_name, website_url = create_static_website_from_source(
                s3_client, args.static_from_source
            )
            print(f"Static website created in bucket '{bucket_name}': {website_url}")
        elif args.static_web:
            create_static_website(s3_client, args.bucket, args.index_file)
            print(
                f"Static website bucket '{args.bucket}' created with "
                f"'{args.index_file}' as index.html."
            )
        elif args.recover_previous:
            version_id = recover_previous_object_version(
                s3_client, args.bucket, args.object_key
            )
            if version_id:
                print(
                    f"Recovered the previous version of '{args.object_key}' in "
                    f"bucket '{args.bucket}' as new version '{version_id}'."
                )
            else:
                print(
                    f"Recovered the previous version of '{args.object_key}' "
                    f"in bucket '{args.bucket}' as the new latest version."
                )
        elif args.object_versioning_info:
            versions = get_object_versions(s3_client, args.bucket, args.object_key)
            if versions:
                print(json.dumps(versions, indent=2))
            else:
                print(
                    f"No versions found for '{args.object_key}' "
                    f"in bucket '{args.bucket}'."
                )
        elif args.bucket_versioning_info:
            status = get_bucket_versioning_status(s3_client, args.bucket)
            if status == "Enabled":
                print(f"Versioning is enabled for bucket '{args.bucket}'.")
            elif status == "Suspended":
                print(f"Versioning is suspended for bucket '{args.bucket}'.")
            else:
                print(f"Versioning has never been enabled for bucket '{args.bucket}'.")
        elif args.cleanup_versions:
            deleted_count = cleanup_old_object_versions(
                s3_client, args.bucket, args.object_key
            )
            print(
                f"Deleted {deleted_count} noncurrent version(s) or delete marker(s) "
                f"older than six months for '{args.object_key}' in bucket '{args.bucket}'."
            )
        elif args.group_by_extension == "true":
            moved_count = group_objects_by_extension(s3_client, args.bucket)
            print(
                f"Moved {moved_count} object(s) into extension-based prefixes "
                f"in bucket '{args.bucket}'."
            )
        elif args.list_buckets:
            bucket_names = list_buckets(s3_client)
            if bucket_names:
                print("\n".join(bucket_names))
            else:
                print("No buckets found.")
        elif args.create_bucket:
            create_bucket(s3_client, args.create_bucket)
            print(f"Bucket '{args.create_bucket}' created.")
        elif args.read_bucket_policy:
            policy = read_bucket_policy(s3_client, args.read_bucket_policy)
            print(json.dumps(policy, indent=2))
        elif args.disable_block_public:
            disable_bucket_public_access_block(s3_client, args.disable_block_public)
            print(
                f"Bucket-level Block Public Access settings disabled for "
                f"'{args.disable_block_public}'."
            )
        elif args.delete_bucket:
            s3_client.delete_bucket(Bucket=args.delete_bucket)
            print(f"Bucket '{args.delete_bucket}' deleted.")
        elif args.delete_object:
            bucket_name, object_key = args.delete_object
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            print(f"Object '{object_key}' deleted from bucket '{bucket_name}'.")
        elif args.public_read_bucket or args.public_read_object:
            if args.public_read_bucket:
                bucket_name = args.public_read_bucket
                grant_public_read(s3_client, bucket_name)
                print(f"Objects in bucket '{bucket_name}' are publicly readable.")
            else:
                bucket_name, object_key = args.public_read_object
                grant_public_read(s3_client, bucket_name, object_key)
                print(f"Object '{object_key}' in bucket '{bucket_name}' is publicly readable.")
        elif args.list_object:
            object_keys = list_objects(s3_client, args.list_object)
            if object_keys:
                print("\n".join(object_keys))
            else:
                print(f"No objects found in bucket '{args.list_object}'.")
        elif args.size_by_extension:
            sizes = total_size_by_extension(s3_client, args.size_by_extension)
            if sizes:
                for extension, size in sorted(sizes.items()):
                    print(f"{extension}: {size} bytes")
            else:
                print(f"No objects found in bucket '{args.size_by_extension}'.")
        elif args.upload_file:
            if args.download_file:
                bucket_name = args.upload_file[0]
                downloaded_file = download_file(args.download_file)
                print(f"Downloaded '{args.download_file}' to '{downloaded_file}'.")
                local_file = str(downloaded_file)
            else:
                bucket_name, local_file = args.upload_file
            object_key = upload_local_file(
                s3_client, bucket_name, local_file, magic_group=args.magic_group
            )
            print(f"Uploaded '{local_file}' to '{bucket_name}/{object_key}'.")
        else:
            bucket_name = args.enable_versioning or args.disable_versioning
            enabled = args.enable_versioning is not None
            set_bucket_versioning(s3_client, bucket_name, enabled)
            status = "enabled" if enabled else "suspended"
            print(f"Versioning {status} for bucket '{bucket_name}'.")
    except (ClientError, S3UploadFailedError, OSError, URLError, ValueError) as error:
        if isinstance(error, ClientError):
            error_code = error.response.get("Error", {}).get("Code", "Unknown")
            message = error.response.get("Error", {}).get("Message", str(error))
            detail = f"AWS error {error_code}: {message}"
        else:
            detail = f"{type(error).__name__}: {error}"
        parser.exit(
            1,
            f"Could not perform the requested S3 operation ({detail}).\n",
        )


if __name__ == "__main__":
    main()
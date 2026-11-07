# boto3-tool
This is a comfortable CLI tool to do quick tasks on AWS using python boto3 module

Install the Python dependencies with `pip install -r requirements.txt`. The
`python-magic` package also requires the system `libmagic` library.

Use `--magic-group` with `--upload-file` to classify a local file by MIME type
and store it under a matching S3 prefix:

```sh
python main.py --upload-file my-bucket ./photo.jpg --magic-group
```

For example, an image detected as `image/jpeg` is uploaded as
`image/jpeg/photo.jpg`.

To delete noncurrent versions and delete markers older than six months for a
specific object, while preserving its current version, use:

```sh
python main.py --cleanup-versions --bucket my-bucket --object path/to/file.txt
```

This permanently deletes old versions and requires the appropriate S3
`ListBucketVersions` and `DeleteObjectVersion` permissions.

To check a bucket's versioning status, use:

```sh
python main.py --bucket-versioning-info --bucket my-bucket
```

The command reports whether versioning is enabled, suspended, or has never
been enabled. It requires the S3 `GetBucketVersioning` permission.

To list all versions and delete markers for an object, use:

```sh
python main.py --object-versioning-info --bucket my-bucket --object path/to/file.txt
```

The results include the version ID, last-modified timestamp, latest-version
status, and size for object versions. This requires the S3
`ListBucketVersions` permission.

To restore the newest noncurrent data version as a new latest version, use:

```sh
python main.py --recover-previous --bucket my-bucket --object path/to/file.txt
```

This copies the previous version to the same key, creating a new version while
preserving the existing version history. Bucket versioning must be enabled.
The operation requires `GetBucketVersioning`, `ListBucketVersions`, and
`GetObjectVersion`/`PutObject` permissions.

To create a static website bucket and upload a local HTML file as `index.html`,
use:

```sh
python main.py --static-web --bucket my-unique-bucket --index ./index.html
```

This disables the bucket's Block Public Access settings and grants anonymous
read access to `index.html` so the website can serve it. Account-level Block
Public Access settings are not changed and may still prevent public access.
The bucket name must be globally unique.

To create a new static website from a public GitHub repository, use:

```sh
python main.py --static-from-source https://github.com/do-community/html_demo_site
```

The command downloads the repository's default-branch archive, requires a
root-level `index.html`, creates a uniquely named bucket, uploads the site
files, and prints the S3 website URL. The bucket contents are made publicly
readable. Account-level Block Public Access settings may still prevent access.
Use only repositories whose contents you are authorized to publish. The
operation requires permission to create buckets, upload objects, configure
website hosting, and set bucket public-access and policy settings.

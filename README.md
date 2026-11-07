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

import boto3
from os import getenv
from dotenv import load_dotenv

load_dotenv()


def init_client():
    client = boto3.client(
        "s3",
        aws_access_key_id=getenv("aws_access_key_id"),
        aws_secret_access_key=getenv("aws_secret_access_key"),
        aws_session_token=getenv("aws_session_token"),
        region_name=getenv("aws_region_name"),
        #  config=botocore.client.Config(
        #      connect_timeout=conf.remote_cfg["remote_timeout"],
        #      read_timeout=conf.remote_cfg["remote_timeout"],
        #      region_name=conf.remote_cfg["aws_default_region"],
        #      retries={
        #          "max_attempts": conf.remote_cfg["remote_retries"]}
    )
    # check if credentials are correct
    client.list_buckets()

    return client

import boto3
from botocore.config import Config

from .config import settings

# Credentials come from boto3's default chain: the task role on ECS, the
# instance profile on EC2, or the environment and ~/.aws locally — never
# hardcoded here. The endpoint comes from settings, so this points at an
# emulator when SERDSAFE_LOCAL_STACK is set and at real AWS otherwise.
#
# This client only ever reads: objects are streamed out of the bucket with
# get_object, and the index is built with list_objects_v2. Nothing here writes,
# and the task role is not expected to allow it.
_client_kwargs = dict(settings.aws_client_kwargs)

# Against an emulator the endpoint is a plain host name, so S3's default
# virtual-host addressing would look for <bucket>.localhost, which resolves
# nowhere. Path addressing keeps the bucket in the URL instead. Real AWS keeps
# the default, where virtual-host addressing is what S3 expects.
if settings.use_local_stack:
    _client_kwargs["config"] = Config(s3={"addressing_style": "path"})

s3_client = boto3.client("s3", **_client_kwargs)

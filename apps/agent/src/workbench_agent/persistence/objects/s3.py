"""Bounded S3-compatible content-addressed objects with read-back verification."""
import asyncio
import base64
import hashlib
import re

from botocore.exceptions import ClientError

MAX_OBJECT_BYTES = 20 * 1024 * 1024
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class S3Objects:
    def __init__(self, client, bucket):
        if not bucket or '/' in bucket:
            raise ValueError('invalid bucket')
        self.client, self.bucket = client, bucket

    @staticmethod
    def _key(sha256):
        if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
            raise ValueError('invalid digest')
        return 'objects/' + sha256

    async def put_verified(self, data, sha256):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise ValueError('invalid object size')
        key = self._key(sha256)
        actual = hashlib.sha256(data).digest()
        if actual.hex() != sha256:
            raise ValueError('digest mismatch')
        try:
            await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=data,
                IfNoneMatch='*', ChecksumSHA256=base64.b64encode(actual).decode('ascii'))
        except ClientError as error:
            if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 412:
                raise
        if await self.read_verified(key, sha256, len(data)) != data:
            raise ValueError('object collision')
        return key

    async def read_verified(self, key, sha256, size):
        if key != self._key(sha256) or not isinstance(size, int) or size < 0 or size > MAX_OBJECT_BYTES:
            raise ValueError('invalid object identity or size')
        def fetch():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response['Body']
            try:
                if response.get('ContentLength') != size:
                    raise ValueError('object size mismatch')
                data = body.read(size + 1)
                if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                    raise ValueError('object digest mismatch')
                return data
            finally:
                body.close()
        return await asyncio.to_thread(fetch)

    async def sign(self, key, *, sha256, size, expires_in):
        if key != self._key(sha256) or not isinstance(size, int) or not 0 <= size <= MAX_OBJECT_BYTES or not 1 <= expires_in <= 60:
            raise ValueError('invalid object authorization')
        return await asyncio.to_thread(self.client.generate_presigned_url, 'get_object', Params={'Bucket': self.bucket, 'Key': key}, ExpiresIn=expires_in)


def configured_s3(settings, *, environment):
    """Resolve deployment-controlled references only in the worker process."""
    from urllib.parse import urlsplit
    import boto3
    from botocore.config import Config
    from workbench_agent.connectors.secrets import SecretProvider
    secrets = SecretProvider()
    endpoint = secrets.resolve(settings.endpoint_ref)
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('invalid S3 endpoint')
    if parsed.scheme == 'http':
        from ipaddress import ip_address
        try:
            numeric_loopback='%' not in parsed.hostname and ip_address(parsed.hostname).is_loopback
        except ValueError:
            numeric_loopback=False
        if environment != 'development' or not settings.allow_http or not numeric_loopback:
            raise ValueError('invalid S3 endpoint')
    client = boto3.client('s3', endpoint_url=endpoint, region_name=settings.region,
        aws_access_key_id=secrets.resolve(settings.access_key_ref),
        aws_secret_access_key=secrets.resolve(settings.secret_key_ref),
        config=Config(retries={'max_attempts':0},connect_timeout=5,read_timeout=30,
            proxies={} if parsed.scheme=='http' else None))
    return S3Objects(client,settings.bucket)

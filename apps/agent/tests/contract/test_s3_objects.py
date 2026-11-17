import hashlib
from io import BytesIO
import pytest
from botocore.stub import Stubber
import boto3

from workbench_agent.persistence.objects.s3 import S3Objects


def make_client():
    return boto3.client('s3', region_name='us-east-1', endpoint_url='https://s3.example.invalid', aws_access_key_id='test', aws_secret_access_key='test')


@pytest.mark.asyncio
async def test_put_verifies_server_bytes_and_uses_conditional_write():
    data = b'hello'; digest = hashlib.sha256(data).hexdigest()
    client = make_client(); stub = Stubber(client)
    stub.add_response('put_object', {}, {'Bucket': 'bucket', 'Key': 'objects/' + digest, 'Body': data, 'IfNoneMatch': '*', 'ChecksumSHA256': 'LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ='})
    stub.add_response('get_object', {'Body': BytesIO(data), 'ContentLength': len(data)}, {'Bucket': 'bucket', 'Key': 'objects/' + digest})
    with stub:
        assert await S3Objects(client, 'bucket').put_verified(data, digest) == 'objects/' + digest


@pytest.mark.asyncio
async def test_read_rejects_wrong_digest_and_size():
    client = make_client(); stub = Stubber(client)
    stub.add_response('get_object', {'Body': BytesIO(b'evil'), 'ContentLength': 4}, {'Bucket': 'bucket', 'Key': 'objects/' + 'a' * 64})
    with stub, pytest.raises(ValueError, match='digest'):
        await S3Objects(client, 'bucket').read_verified('objects/' + 'a' * 64, 'a' * 64, 4)


def test_worker_factory_rejects_http_in_production_and_uses_secret_references(monkeypatch):
    from workbench_agent.persistence.objects.s3 import configured_s3
    from workbench_agent.settings import S3Deployment
    monkeypatch.setenv('TEST_S3_ENDPOINT', 'http://127.0.0.1:9000')
    monkeypatch.setenv('TEST_S3_ACCESS', 'access')
    monkeypatch.setenv('TEST_S3_SECRET', 'secret')
    config = S3Deployment(endpoint_ref='env://TEST_S3_ENDPOINT', access_key_ref='env://TEST_S3_ACCESS', secret_key_ref='env://TEST_S3_SECRET', bucket='bucket', allow_http=True)
    with pytest.raises(ValueError): configured_s3(config, environment='production')
    objects = configured_s3(config, environment='development')
    assert objects.client.meta.endpoint_url == 'http://127.0.0.1:9000'
    assert objects.bucket == 'bucket'


@pytest.mark.parametrize('endpoint,environment,enabled,allowed', [
    ('http://127.0.0.1:19000','development',True,True),
    ('http://[::1]:19000','development',True,True),
    ('http://localhost:19000','development',True,False),
    ('http://192.168.1.2:19000','development',True,False),
    ('http://127.0.0.1:19000','test',True,False),
    ('http://127.0.0.1:19000','production',True,False),
    ('http://127.0.0.1:19000','development',False,False),
    ('https://s3.example.invalid','production',False,True),
])
def test_s3_http_only_explicit_numeric_development_loopback(monkeypatch,endpoint,environment,enabled,allowed):
    from workbench_agent.persistence.objects.s3 import configured_s3
    from workbench_agent.settings import S3Deployment
    monkeypatch.setenv('TEST_S3_ENDPOINT',endpoint)
    monkeypatch.setenv('TEST_S3_ACCESS','synthetic')
    monkeypatch.setenv('TEST_S3_SECRET','synthetic')
    config=S3Deployment(endpoint_ref='env://TEST_S3_ENDPOINT',access_key_ref='env://TEST_S3_ACCESS',secret_key_ref='env://TEST_S3_SECRET',bucket='bucket',allow_http=enabled)
    if allowed:
        assert configured_s3(config,environment=environment).client.meta.endpoint_url==endpoint
    else:
        with pytest.raises(ValueError): configured_s3(config,environment=environment)


def test_local_http_minio_bypasses_external_proxy(monkeypatch):
    from workbench_agent.persistence.objects.s3 import configured_s3
    from workbench_agent.settings import S3Deployment
    monkeypatch.setenv('TEST_S3_ENDPOINT','http://127.0.0.1:19000')
    monkeypatch.setenv('TEST_S3_ACCESS','synthetic')
    monkeypatch.setenv('TEST_S3_SECRET','synthetic')
    config=S3Deployment(endpoint_ref='env://TEST_S3_ENDPOINT',access_key_ref='env://TEST_S3_ACCESS',secret_key_ref='env://TEST_S3_SECRET',bucket='bucket',allow_http=True)
    assert configured_s3(config,environment='development').client.meta.config.proxies=={}

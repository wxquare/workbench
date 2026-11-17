import pytest
from support import minio_live


def config():
    return {'environment':'development','storage':{'s3':{
        'endpoint_ref':'env://WORKBENCH_S3_ENDPOINT','access_key_ref':'env://WORKBENCH_S3_ACCESS_KEY',
        'secret_key_ref':'env://WORKBENCH_S3_SECRET_KEY','bucket':'dod-artifacts',
        'region':'us-east-1','allow_http':True}}}


def test_different_endpoint_reference_is_rejected_before_client_creation(monkeypatch):
    monkeypatch.setenv('WORKBENCH_S3_ENDPOINT','http://127.0.0.1:19000')
    monkeypatch.setenv('OTHER_S3_ENDPOINT','https://remote.invalid')
    value=config();value['storage']['s3']['endpoint_ref']='env://OTHER_S3_ENDPOINT'
    calls=[]
    monkeypatch.setattr(minio_live,'configured_s3',lambda *a,**kw:calls.append('client'))
    with pytest.raises(ValueError): minio_live.local_minio(value)
    assert calls==[]


def test_actual_client_endpoint_is_checked_before_any_object_operation(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('WORKBENCH_S3_ENDPOINT','http://127.0.0.1:19000')
    calls=[]
    objects=SimpleNamespace(client=SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://remote.invalid')))
    monkeypatch.setattr(minio_live,'configured_s3',lambda *a,**kw:(calls.append('client'),objects)[1])
    with pytest.raises(ValueError): minio_live.local_minio(config())
    assert calls==['client']


@pytest.mark.parametrize('field,value',[
    ('endpoint_ref','env://OTHER_S3_ENDPOINT'),
    ('access_key_ref','env://OTHER_ACCESS_KEY'),
    ('secret_key_ref','env://OTHER_SECRET_KEY'),
])
def test_unapproved_references_never_construct_client(monkeypatch,field,value):
    monkeypatch.setenv('WORKBENCH_S3_ENDPOINT','http://127.0.0.1:19000')
    requested=config();requested['storage']['s3'][field]=value
    calls=[];monkeypatch.setattr(minio_live,'configured_s3',lambda *a,**kw:calls.append('client'))
    with pytest.raises(ValueError): minio_live.local_minio(requested)
    assert calls==[]


def test_remote_value_for_allowed_reference_never_constructs_client(monkeypatch):
    monkeypatch.setenv('WORKBENCH_S3_ENDPOINT','https://remote.invalid')
    calls=[];monkeypatch.setattr(minio_live,'configured_s3',lambda *a,**kw:calls.append('client'))
    with pytest.raises(ValueError): minio_live.local_minio(config())
    assert calls==[]

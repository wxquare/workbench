"""Opt-in, local-only real MinIO upload acceptance with isolated test objects."""
import asyncio
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from botocore.exceptions import ClientError
from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.persistence.objects.s3 import S3Objects
from support.minio_live import local_minio
from workbench_agent.execution.uploads import ArtifactUploadWorker, UploadStaging

pytestmark=pytest.mark.live


class RecordingS3Client:
    """Record only a successful conditional create, never credential values."""
    def __init__(self,client): self.client,self.created=client,False
    def put_object(self,**kwargs):
        result=self.client.put_object(**kwargs)
        self.created=True
        return result
    def __getattr__(self,name): return getattr(self.client,name)


async def test_http_job_worker_writes_and_verifies_local_minio(platform,client,tmp_path,monkeypatch):
    config_path=Path(__file__).resolve().parents[3]/'var/minio-local.yaml'
    if not config_path.is_file(): pytest.skip('local MinIO configuration not present')
    with config_path.open(encoding='utf-8') as handle:
        config=yaml.safe_load(handle)
    # Fail before creating a client or issuing any network request if the
    # configured reference or actual client endpoint is not this local MinIO.
    try: s3,objects=local_minio(config)
    except ValueError: pytest.fail('explicit local MinIO endpoint and bucket are required',pytrace=False)
    # This fixture does not call a model; make its model reference available without a real key.
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-fixture-only')
    recorded=RecordingS3Client(objects.client)
    objects=S3Objects(recorded,s3.bucket)
    data=b'dod-m2-local-minio-worker-'+uuid4().bytes
    digest=sha256(data).hexdigest();key='objects/'+digest
    stage=UploadStaging(tmp_path/'stage')
    object.__setattr__(platform.settings.storage,'upload_staging_root',tmp_path/'stage')
    try:
        run=await platform.create_run(key=str(uuid4()))
        created=await client.post(f'/v1/runs/{run.run_id}/uploads',json={'kind':'report','sha256':digest,'size':len(data)})
        assert created.status_code==201
        artifact_id=created.json()['artifact_id']
        accepted=await client.post(f'/v1/uploads/{artifact_id}/complete',content=data,headers={'Idempotency-Key':str(uuid4())})
        assert accepted.status_code==202 and accepted.json()['kind']=='artifact_upload'
        artifacts=ArtifactService(platform.backend,objects=objects)
        registry=ExecutorRegistry();registry.register_admin('artifact_upload',ArtifactUploadWorker(platform.backend,artifacts,stage).execute)
        assert await Worker(platform.backend,'live-minio-worker',registry).run_admin_once()=='admin_completed'
        assert recorded.created, 'test object was not newly created'
        artifact=await artifacts._authorized(platform.auth,artifact_id)
        assert artifact['object_key']==key and artifact['status']=='available'
        assert await objects.read_verified(key,digest,len(data))==data
        with pytest.raises(ValueError,match='size'):
            await objects.read_verified(key,digest,len(data)+1)
    finally:
        if recorded.created:
            await asyncio.to_thread(recorded.client.delete_object,Bucket=s3.bucket,Key=key)
            try:
                await asyncio.to_thread(recorded.client.head_object,Bucket=s3.bucket,Key=key)
            except ClientError as error:
                assert error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==404
            else:
                pytest.fail('test object cleanup could not be verified',pytrace=False)

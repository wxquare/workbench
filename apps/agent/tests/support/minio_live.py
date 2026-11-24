"""Strictly constrain live acceptance to the explicitly authorized local MinIO."""

import os
from workbench_agent.persistence.objects.s3 import configured_s3
from workbench_agent.settings import S3Deployment

LOCAL_ENDPOINT = 'http://127.0.0.1:19000'


def local_minio(config):
    if not isinstance(config, dict) or config.get('environment') != 'development':
        raise ValueError('local MinIO configuration required')
    s3 = S3Deployment.model_validate(config.get('storage', {}).get('s3'))
    if (
        s3.endpoint_ref != 'env://WORKBENCH_S3_ENDPOINT'
        or s3.access_key_ref != 'env://WORKBENCH_S3_ACCESS_KEY'
        or s3.secret_key_ref != 'env://WORKBENCH_S3_SECRET_KEY'
        or s3.bucket != 'dod-artifacts'
        or not s3.allow_http
        or os.environ.get('WORKBENCH_S3_ENDPOINT') != LOCAL_ENDPOINT
    ):
        raise ValueError('local MinIO configuration required')
    objects = configured_s3(s3, environment='development')
    # Do not issue any object request unless the constructed client's actual
    # endpoint is still exactly the single authorized loopback destination.
    if objects.client.meta.endpoint_url != LOCAL_ENDPOINT:
        raise ValueError('local MinIO client endpoint mismatch')
    return s3, objects

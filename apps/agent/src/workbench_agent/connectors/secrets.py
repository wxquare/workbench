"""Resolve references only at the worker boundary; never expose values in errors."""

import os
from urllib.parse import urlsplit
from workbench_agent.application.errors import DomainError
from workbench_agent.settings import secret_reference


class SecretProvider:
    def resolve(self, reference):
        ref = urlsplit(secret_reference(reference))
        value = os.environ.get(ref.netloc) if ref.scheme == 'env' and not ref.path else None
        if not value:
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
        return value


class SecretReferenceAvailability:
    """API-side reference inventory. Inspect environment names, never values.

    The API and workers need a shared reference inventory in distributed
    deployments; custom secret stores must inject their own availability check.
    """

    def __init__(self, managed=None):
        self.managed = managed

    def available(self, reference):
        if reference.startswith('secret://managed-model/'):
            return self.managed is not None and self.managed.available(reference)
        ref = urlsplit(secret_reference(reference))
        return (
            ref.scheme == 'env' and not ref.path and any(name == ref.netloc for name in os.environ)
        )

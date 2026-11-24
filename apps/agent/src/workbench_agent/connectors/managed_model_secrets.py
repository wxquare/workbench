"""Encrypted, immutable model credentials shared by API and workers."""

import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from workbench_agent.application.errors import DomainError


class ManagedModelSecrets:
    PREFIX = 'secret://managed-model/'

    def __init__(self, root, master_key, allowed_endpoints=('https://api.deepseek.com',)):
        self.root = Path(root)
        self.cipher = Fernet(master_key.encode('ascii'))
        self.allowed_endpoints = tuple(allowed_endpoints)

    def _path(self, ref):
        if not ref.startswith(self.PREFIX) or not re.fullmatch(
            r'[0-9a-f]{32}', ref[len(self.PREFIX) :]
        ):
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
        return self.root / ref[len(self.PREFIX) :]

    def _directory(self, create=False):
        try:
            if create:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
            ):
                os.close(fd)
                raise OSError('insecure model credential directory')
            return fd
        except OSError:
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409) from None

    def put(self, value, *, team_id, agent_id, profile_id, base_url):
        if base_url not in self.allowed_endpoints or not value or len(value) > 4096:
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
        payload = json.dumps(
            dict(
                value=value,
                team_id=team_id,
                agent_id=agent_id,
                profile_id=profile_id,
                base_url=base_url,
            ),
            separators=(',', ':'),
        )
        encrypted = self.cipher.encrypt(payload.encode())
        ref = self.PREFIX + uuid4().hex
        directory = self._directory(create=True)
        try:
            fd = os.open(
                self._path(ref).name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, 'wb') as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409) from None
        finally:
            os.close(directory)
        return ref

    def _read(self, ref):
        path = self._path(ref)
        directory = self._directory()
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.geteuid()
                    or info.st_size > 16384
                ):
                    raise OSError('insecure model credential file')
                return json.loads(self.cipher.decrypt(handle.read(16385)))
        except (OSError, ValueError, InvalidToken):
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409) from None
        finally:
            os.close(directory)

    def available(self, ref):
        try:
            self._read(ref)
            return True
        except DomainError:
            return False

    def resolve(self, ref, *, team_id, agent_id, profile_id, base_url):
        data = self._read(ref)
        if (
            base_url not in self.allowed_endpoints
            or data.get('agent_id') not in (agent_id, '*')
            or any(
                data.get(k) != v
                for k, v in dict(team_id=team_id, profile_id=profile_id, base_url=base_url).items()
            )
        ):
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
        value = data.get('value')
        if not isinstance(value, str) or not value:
            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
        return value


def configured_managed_model_secrets(settings):
    key = os.environ.get('WORKBENCH_MODEL_SECRET_MASTER_KEY')
    if not key:
        return None
    if settings.storage.persistent_root is None:
        raise ValueError('managed model credentials require a persistent root')
    try:
        return ManagedModelSecrets(
            settings.storage.persistent_root / 'model-secrets',
            key,
            settings.service.managed_model_endpoints,
        )
    except (ValueError, UnicodeError):
        raise ValueError('invalid model credential master key') from None

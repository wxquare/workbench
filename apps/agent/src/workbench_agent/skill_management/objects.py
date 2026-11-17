"""Local content-addressed object adapter; object names are never caller paths."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

_REF = re.compile(r'^sha256:([0-9a-f]{64})$')
_MAX_BYTES = 20 * 1024 * 1024

class LocalSkillObjects:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('invalid object root')

    def _directory(self):
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def put(self, data: bytes) -> str:
        if len(data) > _MAX_BYTES:
            raise ValueError('object too large')
        value = hashlib.sha256(data).hexdigest()
        directory = self._directory()
        temporary = '.pending-' + secrets.token_hex(16)
        created = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            created = True
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, value, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if self.get('sha256:' + value) != data:
                    raise ValueError('object collision')
            os.fsync(directory)
        finally:
            try:
                if created: os.unlink(temporary, dir_fd=directory)
            finally:
                os.close(directory)
        return 'sha256:' + value

    def get(self, ref: str) -> bytes:
        match = _REF.fullmatch(ref)
        if not match:
            raise ValueError('invalid object ref')
        directory = self._directory()
        try:
            descriptor = os.open(match.group(1), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                    raise ValueError('invalid object')
                data = handle.read(_MAX_BYTES + 1)
            if hashlib.sha256(data).hexdigest() != match.group(1):
                raise ValueError('object hash mismatch')
            return data
        finally:
            os.close(directory)

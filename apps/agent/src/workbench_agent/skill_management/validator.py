"""Validate untrusted skill archives without extracting their paths to disk."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping
import json
import re
import stat
import zipfile
import yaml

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 256
_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


class InvalidBundle(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedBundle:
    name: str
    description: str
    resources: Mapping[str, bytes]
    hashes: Mapping[str, str]
    bundle_hash: str


def validate_bundle_path(path: str) -> str:
    if not path or '\\' in path or '\x00' in path:
        raise InvalidBundle('invalid skill resource path')
    parts = path.split('/')
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise InvalidBundle('invalid skill resource path')
    return candidate.as_posix()


def validate_bundle(
    raw: bytes, name: str, *, allow_binary: bool = False, allow_scripts: bool = False
) -> ValidatedBundle:
    if not _NAME.fullmatch(name) or len(raw) > MAX_ARCHIVE_BYTES:
        raise InvalidBundle('invalid skill bundle')
    resources = {}
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise InvalidBundle('too many resources')
            total = 0
            for entry in entries:
                path = entry.filename[:-1] if entry.is_dir() else entry.filename
                validate_bundle_path(path)
                if not path.startswith(name + '/') and path != name:
                    raise InvalidBundle('invalid skill root')
                if entry.is_dir():
                    continue
                if path == name or entry.flag_bits & 1:
                    raise InvalidBundle('invalid resource')
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise InvalidBundle('unsupported link or special file')
                relative = path[len(name) + 1 :]
                if relative in resources or entry.file_size > MAX_FILE_BYTES:
                    raise InvalidBundle('duplicate or oversized resource')
                total += entry.file_size
                if total > MAX_ARCHIVE_BYTES:
                    raise InvalidBundle('bundle exceeds size limit')
                content = archive.read(entry)
                if len(content) != entry.file_size:
                    raise InvalidBundle('resource length mismatch')
                if '.claude' in relative.split('/'):
                    raise InvalidBundle('unsupported Claude resource')
                if not allow_scripts and (
                    'scripts' in relative.split('/')
                    or relative.endswith(('.sh', '.py', '.bash', '.js'))
                    or content.startswith(b'#!')
                    or mode & 0o111
                ):
                    raise InvalidBundle('unsupported executable or Claude resource')
                if not allow_binary:
                    try:
                        decoded = content.decode('utf-8')
                        if '\x00' in decoded:
                            raise ValueError()
                    except (UnicodeError, ValueError) as exc:
                        raise InvalidBundle('unsupported binary resource') from exc
                resources[relative] = content
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
        raise InvalidBundle('invalid skill archive') from exc
    try:
        text = resources['SKILL.md'].decode('utf-8')
        if not text.startswith('---\n'):
            raise ValueError()
        front, body = text[4:].split('\n---\n', 1)
        metadata = yaml.safe_load(front)
        if (
            not isinstance(metadata, dict)
            or metadata.get('name') != name
            or not isinstance(metadata.get('description'), str)
            or not metadata['description'].strip()
            or not body.strip()
        ):
            raise ValueError()
        # Claude-specific executable configuration is never interpreted implicitly.
        if set(metadata) - {'name', 'description'}:
            raise InvalidBundle('unsupported skill metadata')
        if any(path.startswith('.claude/') for path in resources):
            raise InvalidBundle('unsupported Claude configuration')
    except InvalidBundle:
        raise
    except (KeyError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise InvalidBundle('invalid SKILL.md') from exc
    hashes = {path: sha256(value).hexdigest() for path, value in sorted(resources.items())}
    encoded = json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()
    return ValidatedBundle(
        name,
        metadata['description'],
        MappingProxyType(resources),
        MappingProxyType(hashes),
        sha256(encoded).hexdigest(),
    )

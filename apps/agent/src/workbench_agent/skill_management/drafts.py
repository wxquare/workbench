"""Canonical draft resources and the explicit publication-only script policy."""
import base64
import binascii
from io import BytesIO
from pathlib import PurePosixPath
import re
import zipfile

import yaml

from workbench_agent.application.errors import DomainError
from workbench_agent.skill_management.validator import InvalidBundle, validate_bundle, validate_bundle_path

MAX_DRAFT_FILES = 64
MAX_DRAFT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_DRAFT_PAYLOAD = 12 * 1024 * 1024
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}


def image_matches(path, raw):
    suffix = PurePosixPath(path).suffix.lower()
    return ((suffix == '.png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))
            or (suffix in ('.jpg', '.jpeg') and raw.startswith(b'\xff\xd8\xff'))
            or (suffix == '.gif' and raw.startswith((b'GIF87a', b'GIF89a')))
            or (suffix == '.webp' and len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'))


def normalize_files(files):
    """Validate JSON resources without extracting caller-controlled paths."""
    try:
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_DRAFT_FILES:
            raise ValueError('invalid resource count')
        result, total = {}, 0
        for path, value in files.items():
            if (not isinstance(path, str) or len(path) > 256 or re.match(r'^[A-Za-z]:', path)
                    or validate_bundle_path(path) != path or '.claude' in path.split('/')):
                raise ValueError('invalid resource path')
            path.encode('utf-8')
            if isinstance(value, str):
                value = {'encoding': 'utf-8', 'content': value}
            if not isinstance(value, dict) or set(value) != {'encoding', 'content'}:
                raise ValueError('invalid resource')
            encoding, content = value['encoding'], value['content']
            if not isinstance(content, str):
                raise ValueError('invalid resource content')
            if encoding == 'utf-8':
                if '\x00' in content or PurePosixPath(path).suffix.lower() in IMAGE_TYPES:
                    raise ValueError('invalid text')
                raw = content.encode('utf-8')
                if len(raw) > (32000 if path == 'SKILL.md' else MAX_TEXT_BYTES):
                    raise ValueError('text too large')
            elif encoding == 'base64':
                if path == 'SKILL.md' or len(content) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                    raise ValueError('invalid image')
                raw = base64.b64decode(content, validate=True)
                if len(raw) > MAX_IMAGE_BYTES or not image_matches(path, raw):
                    raise ValueError('invalid image')
                # Canonicalize padding bits for stable JSON receipts.
                content = base64.b64encode(raw).decode('ascii')
            else:
                raise ValueError('unsupported encoding')
            total += len(raw)
            if total > MAX_DRAFT_BYTES:
                raise ValueError('draft too large')
            result[path] = {'encoding': encoding, 'content': content}
        main = result.get('SKILL.md')
        if not main or main['encoding'] != 'utf-8' or not main['content'].strip():
            raise ValueError('main instruction required')
        for path in result:
            parts = path.split('/')
            if any('/'.join(parts[:i]) in result for i in range(1, len(parts))):
                raise ValueError('file-directory collision')
        return result
    except (ValueError, TypeError, UnicodeError, binascii.Error, InvalidBundle):
        raise DomainError('INVALID_SKILL_CONTENT', 422) from None


def bundle_for_files(name, description, files):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 128:
        raise DomainError('INVALID_SKILL_NAME', 422)
    if not description.strip() or len(description) > 1000:
        raise DomainError('INVALID_SKILL_CONTENT', 422)
    normalized = normalize_files(files)
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for path, value in normalized.items():
            content = value['content']
            raw = content.encode('utf-8') if value['encoding'] == 'utf-8' else base64.b64decode(content)
            if path == 'SKILL.md':
                metadata = yaml.safe_dump({'name': name, 'description': description}, allow_unicode=True, sort_keys=False)
                raw = ('---\n' + metadata + '---\n' + content.strip() + '\n').encode('utf-8')
            archive.writestr(name + '/' + path, raw)
    try:
        return validate_bundle(output.getvalue(), name, allow_binary=True, allow_scripts=True)
    except InvalidBundle:
        raise DomainError('INVALID_SKILL_CONTENT', 422) from None


def encode_resources(resources):
    result = {}
    for path, raw in resources.items():
        try:
            if PurePosixPath(path).suffix.lower() in IMAGE_TYPES or b'\x00' in raw:
                raise UnicodeError()
            result[path] = {'encoding': 'utf-8', 'content': raw.decode('utf-8')}
        except UnicodeError:
            result[path] = {'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')}
    return result

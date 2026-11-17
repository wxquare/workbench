"""Versioned content-addressed skill directories and individual resources."""
from hashlib import sha256
from io import BytesIO
import json
import re
import zipfile
from workbench_agent.skill_management.validator import MAX_FILES, InvalidBundle, validate_bundle, validate_bundle_path

PREFIX = 'skill-manifest-v1:'
_HASH = re.compile(r'^[0-9a-f]{64}$')


def _directory_hash(hashes):
    return sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_manifest(objects, bundle):
    hashes = dict(bundle.hashes)
    for path, content in bundle.resources.items():
        if objects.put(content) != 'sha256:' + hashes[path]:
            raise InvalidBundle('resource storage hash mismatch')
    manifest = {'format': 1, 'name': bundle.name, 'description': bundle.description, 'hashes': hashes}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    return PREFIX + objects.put(raw)


def read_manifest(objects, reference, name, expected_hash):
    if not reference.startswith(PREFIX): raise InvalidBundle('unsupported skill manifest')
    try:
        raw = objects.get(reference[len(PREFIX):])
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or set(manifest) != {'format', 'name', 'description', 'hashes'} or manifest['format'] != 1 or manifest['name'] != name:
            raise ValueError()
        hashes = manifest['hashes']
        if not isinstance(hashes, dict) or not hashes or len(hashes) > MAX_FILES or 'SKILL.md' not in hashes:
            raise ValueError()
        if not isinstance(manifest['description'], str) or not manifest['description'].strip(): raise ValueError()
        for path, value in hashes.items():
            if not isinstance(path, str) or validate_bundle_path(path) != path or not isinstance(value, str) or not _HASH.fullmatch(value):
                raise ValueError()
        if _directory_hash(hashes) != expected_hash: raise ValueError()
        return manifest
    except (UnicodeError, ValueError, KeyError, TypeError, OSError) as exc:
        raise InvalidBundle('invalid skill manifest') from exc


def read_resource(objects, manifest, path):
    try:
        digest = manifest['hashes'][path]
        value = objects.get('sha256:' + digest)
        if sha256(value).hexdigest() != digest: raise ValueError()
        return value
    except (OSError, ValueError, KeyError) as exc:
        raise InvalidBundle('skill resource unavailable') from exc


def validate_skill_metadata(manifest, content):
    # Reuse the import contract without reading unrelated resources.
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(manifest['name'] + '/SKILL.md', content)
    parsed = validate_bundle(output.getvalue(), manifest['name'])
    if parsed.description != manifest['description']:
        raise InvalidBundle('skill metadata mismatch')

"""Publish reviewed seed knowledge through authenticated public APIs."""

from __future__ import annotations

from pathlib import Path

from workbench_agent.evaluation.api_client import EvaluationAPI
from workbench_agent.evaluation.contracts import canonical, hash_json, validate_seed_manifest
from workbench_agent.evaluation.journal import atomic_json, fsync_directory, read_json


class ImportConflict(Exception):
    pass


def _read_state(path: Path, namespace: str) -> dict:
    if not path.exists():
        return {
            'schema_version': 'knowledge-import-state-v1',
            'namespace': namespace,
            'seeds': {},
        }
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get('schema_version') != 'knowledge-import-state-v1'
        or value.get('namespace') != namespace
        or not isinstance(value.get('seeds'), dict)
    ):
        raise ValueError('invalid import state')
    return value


def _snapshot(seed: dict, namespace: str) -> dict:
    return {
        'seed_id': seed['seed_id'],
        'namespace': namespace,
        'content': seed['draft'],
        'provenance': seed['provenance'],
    }


async def import_seed(
    api: EvaluationAPI,
    manifest: dict,
    *,
    namespace: str,
    state_path: str | Path,
) -> dict:
    seeds = validate_seed_manifest(manifest)
    if not namespace or namespace != namespace.strip() or len(namespace) > 128:
        raise ValueError('invalid namespace')
    state_path = Path(state_path)
    state = _read_state(state_path, namespace)
    for seed in seeds:
        seed_id = seed['seed_id']
        snapshot = _snapshot(seed, namespace)
        content_hash = hash_json(snapshot)
        existing = state['seeds'].get(seed_id)
        if existing:
            if existing.get('content_hash') != content_hash:
                raise ImportConflict(
                    f'seed {seed_id} changed after import in namespace {namespace}'
                )
            mapped = existing.get('mapping')
            if mapped:
                continue
            entry_id, revision = existing['entry_id'], existing['created_revision']
        else:
            created = await api.create_kb_entry(
                seed['draft'], key=f'kb-import-create:{hash_json(snapshot)}'
            )
            entry_id, revision = created['entry_id'], created['revision']
            if created.get('title') != seed['draft']['title']:
                raise ImportConflict(
                    f'namespace collision for seed {seed_id}; create a distinct namespace'
                )
            state['seeds'][seed_id] = {
                'content_hash': content_hash,
                'entry_id': entry_id,
                'created_revision': revision,
                'published_revision': None,
                'mapping': None,
            }
            atomic_json(state_path, state)
        entry = await api.get_kb_entry(entry_id)
        if entry.get('status') != 'published':
            current = entry.get('draft')
            if canonical(current) != canonical(seed['draft']):
                saved = await api.save_kb_entry(
                    entry_id,
                    seed['draft'],
                    revision,
                    key=f'kb-import-save:{content_hash}:{revision}',
                )
                revision = saved['revision']
                state['seeds'][seed_id]['created_revision'] = revision
                atomic_json(state_path, state)
            if not isinstance(revision, int) or revision < 0:
                raise ImportConflict(f'seed {seed_id} has an invalid draft revision')
            await api.publish_kb_entry(
                entry_id,
                revision,
                key=f'kb-import-publish:{content_hash}:{revision}',
            )
            entry_refresh = await api.get_kb_entry(entry_id)
            revision = entry_refresh['published_version']
            if revision is None:
                raise ImportConflict(f'seed {seed_id} publication did not become visible')
            state['seeds'][seed_id]['published_revision'] = revision
            # The mapping remains null until exact chunks have been read.
            atomic_json(state_path, state)
        else:
            revision = entry['published_version']
        version = await api.get_kb_version(entry_id, int(revision))
        source_ids = [
            f"kb:{entry_id}@{revision}#{chunk['chunk_id']}"
            for chunk in version.get('chunks', [])
        ]
        entry_refresh = await api.get_kb_entry(entry_id)
        if entry_refresh.get('status') != 'published':
            raise ImportConflict(f'seed {seed_id} is not published')
        mapping = {
            'entry_id': entry_id,
            'version': int(revision),
            'content_hash': version['content_hash'],
            'chunk_source_ids': source_ids,
        }
        state['seeds'][seed_id]['mapping'] = mapping
        atomic_json(state_path, state)
    fsync_directory(state_path.parent)
    return state

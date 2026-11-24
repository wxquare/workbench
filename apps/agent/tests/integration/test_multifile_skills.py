"""Real draft/manifest lifecycle: attachments must survive edits and version pinning."""

import base64
import json
from uuid import uuid4

import pytest

from workbench_agent.domain.profiles import VersionRef
from workbench_agent.skill_management.loader import SkillLoader
from workbench_agent.skill_management.objects import LocalSkillObjects
from support.skills import pinned_execution


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
)
SCRIPT = '#!/usr/bin/env python3\nprint("analysis only")\n'


def resource(content, encoding='utf-8'):
    return {'encoding': encoding, 'content': content}


async def create(client, files, **extra):
    return await client.post(
        '/v1/skills',
        headers={'Idempotency-Key': str(uuid4())},
        json={'name': 'log-analysis', 'description': '日志排障', 'files': files, **extra},
    )


def headers(revision, key=None):
    return {'If-Match': str(revision), 'Idempotency-Key': key or str(uuid4())}


async def test_publish_all_resources_and_load_frozen_script_image(client, platform, tmp_path):
    root = tmp_path / 'objects'
    client._transport.app.state.settings.storage.skill_object_root = root
    files = {
        'SKILL.md': resource('Use evidence.'),
        'scripts/analyse.py': resource(SCRIPT),
        'assets/topology.png': resource(base64.b64encode(PNG).decode(), 'base64'),
        'references/empty.md': resource(''),
    }
    response = await create(client, files)
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill['draft']['files'] == files
    assert skill['draft']['content'] == 'Use evidence.'
    detail = await client.get('/v1/skills/' + skill['skill_id'])
    assert detail.json()['draft']['files'] == files
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(
            uow, 'skill_drafts', skill_id=skill['skill_id']
        )
        assert json.loads(row['files_json']) == files
    result = await client.post(f'/v1/skills/{skill["skill_id"]}/publish', headers=headers(0))
    assert result.status_code == 201, result.text
    version = await client.get(f'/v1/skills/{skill["skill_id"]}/versions/1')
    assert version.status_code == 200, version.text
    assert version.json()['files']['scripts/analyse.py'] == resource(SCRIPT)
    assert version.json()['files']['assets/topology.png'] == files['assets/topology.png']
    assert version.json()['files']['references/empty.md'] == resource('')
    assert version.json()['content'].startswith('---\nname: log-analysis\n')
    assert version.json()['files']['SKILL.md']['content'] == version.json()['content']
    execution = await pinned_execution(platform, (VersionRef.model_validate(result.json()),))
    loader = SkillLoader(platform.backend, LocalSkillObjects(root))
    script = await loader.load(
        execution.claim.context,
        execution.claim.context.skills[0],
        'scripts/analyse.py',
        execution=execution,
    )
    image = await loader.load(
        execution.claim.context,
        execution.claim.context.skills[0],
        'assets/topology.png',
        execution=execution,
    )
    assert script.content == SCRIPT
    assert image.content == PNG


async def test_legacy_save_preserves_attachments_and_revision_conflicts(client):
    first = await create(client, {'SKILL.md': 'first', 'references/info.md': 'keep'})
    assert first.status_code == 201, first.text
    path = '/v1/skills/' + first.json()['skill_id']
    changed = await client.patch(
        path, headers=headers(0), json={'description': 'updated', 'content': 'second'}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()['draft']['files'] == {
        'SKILL.md': resource('second'),
        'references/info.md': resource('keep'),
    }
    stale = await client.patch(
        path, headers=headers(0), json={'description': 'stale', 'files': {'SKILL.md': 'lost'}}
    )
    assert stale.status_code == 409
    assert (await client.get(path)).json()['draft']['files']['references/info.md'] == resource(
        'keep'
    )


async def test_replace_map_renames_deletes_and_history_remains_frozen(client, tmp_path):
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'objects'
    first = await create(
        client, {'SKILL.md': 'first', 'references/old.md': 'old', 'remove.md': 'delete'}
    )
    assert first.status_code == 201, first.text
    path = '/v1/skills/' + first.json()['skill_id']
    key = headers(0)
    v1 = await client.post(path + '/publish', headers=key)
    assert v1.status_code == 201, v1.text
    assert (await client.post(path + '/publish', headers=key)).json() == v1.json()
    changed = await client.patch(
        path,
        headers=headers(1),
        json={
            'description': 'second',
            'files': {'SKILL.md': 'second', 'references/renamed.md': 'new'},
        },
    )
    assert changed.status_code == 200, changed.text
    v2 = await client.post(path + '/publish', headers=headers(2))
    assert v2.status_code == 201, v2.text
    old = (await client.get(path + '/versions/1')).json()['files']
    new = (await client.get(path + '/versions/2')).json()['files']
    assert old['references/old.md'] == resource('old')
    assert 'remove.md' in old and 'remove.md' not in new
    assert 'references/old.md' not in new
    assert new['references/renamed.md'] == resource('new')


@pytest.mark.parametrize(
    'files',
    [
        {'reference.md': 'missing main'},
        {'SKILL.md': 'valid', '../escape.md': 'bad'},
        {'SKILL.md': 'valid', 'a': 'file', 'a/b.md': 'collision'},
        {'SKILL.md': 'valid', '.claude/hooks.json': '{}'},
        {'SKILL.md': resource('aGk=', 'base64')},
        {'SKILL.md': 'valid', 'assets/a.png': resource('not base64!', 'base64')},
        {
            'SKILL.md': 'valid',
            'assets/a.png': resource(base64.b64encode(b'not an image').decode(), 'base64'),
        },
        {'SKILL.md': 'valid', 'assets/a.txt': resource(base64.b64encode(PNG).decode(), 'base64')},
        {'SKILL.md': 'valid', 'zero.md': 'bad\x00text'},
        {'SKILL.md': 'x' * 32001},
    ],
)
async def test_invalid_draft_files_leave_no_skill(client, platform, files):
    count = await platform.count_rows('skills')
    response = await create(client, files)
    assert response.status_code == 422, response.text
    assert await platform.count_rows('skills') == count


async def test_content_alias_must_agree_with_main_file(client):
    response = await create(client, {'SKILL.md': 'new'}, content='different')
    assert response.status_code == 422


async def test_large_attachment_idempotency_includes_entire_payload(client):
    payload = {
        'name': 'large-text',
        'description': 'large',
        'files': {'SKILL.md': 'main', 'references/details.md': '证据' * 20000},
    }
    h = {'Idempotency-Key': str(uuid4())}
    first = await client.post('/v1/skills', headers=h, json=payload)
    assert first.status_code == 201, first.text
    retry = await client.post('/v1/skills', headers=h, json=payload)
    assert retry.status_code == 201 and retry.json() == first.json()
    payload['files']['references/details.md'] += '变更'
    assert (await client.post('/v1/skills', headers=h, json=payload)).status_code == 409


async def test_inventory_does_not_depend_on_editable_resource_payload(client, platform):
    created = await create(client, {'SKILL.md': 'main'})
    assert created.status_code == 201
    skill_id = created.json()['skill_id']
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.change(
            uow, 'skill_drafts', {'skill_id': skill_id}, {'files_json': 'unavailable draft content'}
        )
    # Catalog metadata must remain available without decoding any draft images/files.
    response = await client.get('/v1/skills')
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()['items'] if item['skill_id'] == skill_id)
    assert item['name'] == 'log-analysis' and 'draft' not in item


@pytest.mark.parametrize('field', ['path', 'description'])
async def test_invalid_unicode_is_a_validation_error(client, field):
    payload = {'name': 'unicode', 'description': 'valid', 'files': {'SKILL.md': 'valid'}}
    if field == 'path':
        payload['files']['\ud800.md'] = 'reference'
    else:
        payload['description'] = '\ud800'
    response = await client.post(
        '/v1/skills',
        headers={'Idempotency-Key': str(uuid4()), 'Content-Type': 'application/json'},
        content=json.dumps(payload),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    'files',
    [
        {'SKILL.md': 'valid', **{f'ref/{i}.md': '' for i in range(64)}},
        {'SKILL.md': 'valid', 'references/large.md': 'x' * (256 * 1024 + 1)},
        {'SKILL.md': 'valid', 'x' * 257: 'too long'},
        {
            'SKILL.md': 'valid',
            'assets/large.png': resource(
                base64.b64encode(PNG + b'\0' * (2 * 1024 * 1024)).decode(), 'base64'
            ),
        },
    ],
)
async def test_draft_limits_are_rejected_without_persistence(client, platform, files):
    count = await platform.count_rows('skill_drafts')
    response = await create(client, files)
    assert response.status_code == 422, response.text
    assert await platform.count_rows('skill_drafts') == count


async def test_history_fails_if_any_published_attachment_is_missing(client, platform, tmp_path):
    from workbench_agent.skill_management.manifest import read_manifest

    root = tmp_path / 'objects'
    client._transport.app.state.settings.storage.skill_object_root = root
    created = await create(client, {'SKILL.md': 'valid', 'scripts/check.py': SCRIPT})
    assert created.status_code == 201
    path = '/v1/skills/' + created.json()['skill_id']
    published = await client.post(path + '/publish', headers=headers(0))
    assert published.status_code == 201
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(
            uow, 'skill_versions', skill_version_id=published.json()['resource_id']
        )
    manifest = read_manifest(
        LocalSkillObjects(root), row['bundle_ref'], 'log-analysis', row['hash']
    )
    (root / manifest['hashes']['scripts/check.py']).unlink()
    response = await client.get(path + '/versions/1')
    assert response.status_code == 409 and response.json()['code'] == 'SKILL_VERSION_UNAVAILABLE'

from uuid import uuid4
import pytest
from sqlalchemy import text

from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.maintenance import KnowledgeMaintenance


async def test_check_and_rebuild_recover_missing_index_rows_without_creating_versions(
    client, platform
):
    item = (
        await client.post(
            '/v1/kb/entries',
            headers={'Idempotency-Key': str(uuid4())},
            json={'title': '库存', 'kind': 'faq', 'body_md': '# 排查\n检查库存扣减。'},
        )
    ).json()
    entry_id = item['entry_id']
    await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            text('DELETE FROM kb_fts WHERE team_id=:team AND entry_id=:entry'),
            {'team': platform.auth.team_id, 'entry': entry_id},
        )
    maintenance = KnowledgeMaintenance(platform.backend)
    report = await maintenance.check(platform.auth.team_id)
    assert report['entries'][entry_id]['missing']
    with pytest.raises(DomainError, match='REVISION_CONFLICT'):
        await maintenance.rebuild(platform.auth.team_id, entry_id, 0)
    await maintenance.rebuild(platform.auth.team_id, entry_id, 1)
    assert (await maintenance.check(platform.auth.team_id))['entries'] == {}
    assert len((await client.get(f'/v1/kb/entries/{entry_id}/versions')).json()['items']) == 1


async def test_check_detects_duplicate_chunks_and_changed_index_metadata(client, platform):
    item = (
        await client.post(
            '/v1/kb/entries',
            headers={'Idempotency-Key': str(uuid4())},
            json={'title': '库存', 'kind': 'faq', 'body_md': '检查库存扣减。', 'tags': ['stock']},
        )
    ).json()
    entry_id = item['entry_id']
    await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            text('UPDATE kb_fts SET title=:title WHERE team_id=:team AND entry_id=:entry'),
            {'title': '错误标题', 'team': platform.auth.team_id, 'entry': entry_id},
        )
        await uow.execute(
            text("""INSERT INTO kb_fts(title,content,tags,aliases,team_id,entry_id,version,chunk_id,
            heading_path,line_start,line_end,content_hash,chunker_version)
            SELECT title,content,tags,aliases,team_id,entry_id,version,chunk_id,heading_path,line_start,line_end,
            content_hash,chunker_version FROM kb_fts WHERE team_id=:team AND entry_id=:entry"""),
            {'team': platform.auth.team_id, 'entry': entry_id},
        )
    maintenance = KnowledgeMaintenance(platform.backend)
    report = (await maintenance.check(platform.auth.team_id))['entries'][entry_id]
    assert report['duplicate_chunks']
    assert report['metadata_mismatch']
    await maintenance.rebuild(platform.auth.team_id, entry_id, 1)
    assert (await maintenance.check(platform.auth.team_id))['entries'] == {}

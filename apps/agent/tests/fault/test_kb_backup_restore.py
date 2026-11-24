import sqlite3
from uuid import uuid4

from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup, restore


async def test_backup_and_restore_preserve_knowledge_versions_and_search_index(
    client, platform, tmp_path
):
    created = await client.post(
        '/v1/kb/entries',
        headers={'Idempotency-Key': str(uuid4())},
        json={'title': '库存', 'kind': 'faq', 'body_md': '检查库存扣减失败。'},
    )
    entry_id = created.json()['entry_id']
    result = await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    assert result.status_code == 201
    backup = tmp_path / 'kb-backup.sqlite3'
    restored = tmp_path / 'kb-restored.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(backup)
    report = restore(backup, restored)
    assert report.external_writes_enabled is False
    with sqlite3.connect(restored) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute(
            'SELECT count(*) FROM kb_entry_versions WHERE entry_id=?', (entry_id,)
        ).fetchone() == (1,)
        assert (
            db.execute(
                'SELECT count(*) FROM kb_fts WHERE kb_fts MATCH ?', ('扣减失败',)
            ).fetchone()[0]
            > 0
        )

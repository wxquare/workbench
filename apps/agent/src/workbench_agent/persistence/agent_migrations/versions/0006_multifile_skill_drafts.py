"""Preserve legacy draft bodies as the main entry of a resource map."""
import json
from alembic import op
from sqlalchemy import text

revision = '0006_multifile_skill_drafts'
down_revision = '0005_graph_id'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(text('SELECT skill_id, content FROM skill_drafts')).mappings().all()
    for row in rows:
        encoded = json.dumps({'SKILL.md': {'encoding': 'utf-8', 'content': row['content']}},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        connection.execute(text('UPDATE skill_drafts SET content=:files WHERE skill_id=:id'),
                           {'files': encoded, 'id': row['skill_id']})
    op.execute('ALTER TABLE skill_drafts RENAME COLUMN content TO files_json')


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

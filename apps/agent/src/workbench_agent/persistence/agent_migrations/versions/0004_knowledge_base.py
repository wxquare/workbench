"""Add small team knowledge base with transactional FTS index."""
from alembic import op
from sqlalchemy import BigInteger, Column, String, Text
from workbench_agent.persistence.schema_agent_v4 import kb_entries, kb_entry_versions

revision = '0004_knowledge_base'
down_revision = '0003_run_trace_contexts'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    kb_entries.create(bind, checkfirst=False)
    kb_entry_versions.create(bind, checkfirst=False)
    with op.batch_alter_table('knowledge_gaps', recreate='always') as batch:
        for name, kind in [('query_scope', Text), ('dedup_digest', String(64)),
                           ('resolved_entry_id', String(36)), ('resolved_version', BigInteger),
                           ('replay_run_id', String(36)), ('verification_status', String(32))]:
            batch.add_column(Column(name, kind))
        batch.add_column(Column('gap_revision', BigInteger, nullable=False, server_default='0'))
        batch.create_foreign_key('fk_knowledge_gaps_team_id_kb_entry_versions', 'kb_entry_versions',
            ['team_id', 'resolved_entry_id', 'resolved_version'], ['team_id', 'entry_id', 'version'], ondelete='RESTRICT')
        batch.create_foreign_key('fk_knowledge_gaps_team_id_runs_replay', 'runs',
            ['team_id', 'replay_run_id'], ['team_id', 'run_id'], ondelete='RESTRICT')
        batch.create_index('ix_knowledge_gaps_team_dedup', ['team_id', 'dedup_digest'], unique=True)
    op.execute("""CREATE VIRTUAL TABLE kb_fts USING fts5(
        title, content, tags, aliases, team_id UNINDEXED, entry_id UNINDEXED,
        version UNINDEXED, chunk_id UNINDEXED, heading_path UNINDEXED,
        line_start UNINDEXED, line_end UNINDEXED, content_hash UNINDEXED,
        chunker_version UNINDEXED, tokenize='trigram')""")


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

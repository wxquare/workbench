"""Knowledge entries and immutable published snapshots, extending v3."""
from sqlalchemy import BigInteger, Column, ForeignKeyConstraint, Index, MetaData, String, Table, Text
from workbench_agent.persistence.schema_agent_v3 import metadata as baseline

MIGRATION_HEAD = '0004_knowledge_base'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

kb_entries = Table('kb_entries', metadata,
    Column('team_id', String(36), primary_key=True, nullable=False),
    Column('entry_id', String(36), primary_key=True, nullable=False),
    Column('status', String(32), nullable=False),
    Column('published_version', BigInteger),
    Column('draft_json', Text, nullable=False),
    Column('revision', BigInteger, nullable=False, server_default='0'),
    Column('owner', String(128), nullable=False),
    Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    ForeignKeyConstraint(['team_id'], ['teams.team_id'], ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id', 'entry_id', 'published_version'],
                         ['kb_entry_versions.team_id', 'kb_entry_versions.entry_id', 'kb_entry_versions.version'],
                         ondelete='RESTRICT'))

kb_entry_versions = Table('kb_entry_versions', metadata,
    Column('team_id', String(36), primary_key=True, nullable=False),
    Column('entry_id', String(36), primary_key=True, nullable=False),
    Column('version', BigInteger, primary_key=True, nullable=False),
    Column('content_json', Text, nullable=False),
    Column('content_hash', String(64), nullable=False),
    Column('author', String(128), nullable=False),
    Column('change_note', Text),
    Column('created_at', BigInteger, nullable=False),
    ForeignKeyConstraint(['team_id', 'entry_id'], ['kb_entries.team_id', 'kb_entries.entry_id'], ondelete='RESTRICT'))

knowledge_gaps = metadata.tables['knowledge_gaps']
for name, kind in [('query_scope', Text), ('dedup_digest', String(64)),
                   ('resolved_entry_id', String(36)), ('resolved_version', BigInteger),
                   ('replay_run_id', String(36)), ('verification_status', String(32))]:
    knowledge_gaps.append_column(Column(name, kind))
knowledge_gaps.append_column(Column('gap_revision', BigInteger, nullable=False, server_default='0'))
knowledge_gaps.append_constraint(ForeignKeyConstraint(
    ['team_id', 'resolved_entry_id', 'resolved_version'],
    ['kb_entry_versions.team_id', 'kb_entry_versions.entry_id', 'kb_entry_versions.version'],
    ondelete='RESTRICT'))
knowledge_gaps.append_constraint(ForeignKeyConstraint(
    ['team_id', 'replay_run_id'], ['runs.team_id', 'runs.run_id'], ondelete='RESTRICT'))
Index('ix_knowledge_gaps_team_dedup', knowledge_gaps.c.team_id, knowledge_gaps.c.dedup_digest, unique=True)

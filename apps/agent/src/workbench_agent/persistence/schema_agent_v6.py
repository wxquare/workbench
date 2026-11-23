"""Agent schema with a canonical multi-resource draft JSON column."""
from sqlalchemy import Column, MetaData, Text
from workbench_agent.persistence.schema_agent_v5 import metadata as baseline

MIGRATION_HEAD = '0006_multifile_skill_drafts'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

drafts = metadata.tables['skill_drafts']
drafts._columns.remove(drafts.c.content)
drafts.append_column(Column('files_json', Text, nullable=False))

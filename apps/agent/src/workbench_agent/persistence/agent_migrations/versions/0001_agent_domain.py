"""Create the fresh Agent domain without transforming existing Workspace data."""
from alembic import op
from sqlalchemy import insert
from workbench_agent.persistence.schema_agent_v1 import (
    CHECKPOINT_CODEC_VERSION, SCHEMA_VERSION, metadata,
)

revision = '0001_agent_domain'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' AND name != 'alembic_version'"
    ).fetchall()
    if existing:
        raise RuntimeError('Agent baseline requires a fresh database; existing database left untouched')
    metadata.create_all(connection, checkfirst=False)
    connection.execute(insert(metadata.tables['dod_storage_metadata']).values(
        schema_version=SCHEMA_VERSION, checkpoint_codec_version=CHECKPOINT_CODEC_VERSION))


def downgrade():
    raise RuntimeError('Agent downgrade requires separately reviewed backup restoration')

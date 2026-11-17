"""Portable v1 core. Snapshot imports are immutable across future migrations."""
from alembic import op
from sqlalchemy import insert
from workbench_agent.persistence.schema_v1 import metadata
SCHEMA_VERSION = "dod-storage-v1"
CHECKPOINT_CODEC_VERSION = "dod-checkpoint-v1"

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    connection = op.get_bind()
    metadata.create_all(connection,checkfirst=False)
    connection.execute(insert(metadata.tables["dod_storage_metadata"]).values(
        schema_version=SCHEMA_VERSION,checkpoint_codec_version=CHECKPOINT_CODEC_VERSION))

def downgrade():
    # Destructive rollback requires a separately reviewed maintenance procedure.
    raise RuntimeError("v1 downgrade requires backup restoration")

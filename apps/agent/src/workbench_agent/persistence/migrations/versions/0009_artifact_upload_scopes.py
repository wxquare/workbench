"""Bind newly uploaded artifacts to their original query scope."""
from alembic import op
from workbench_agent.persistence.schema_v8 import artifact_upload_scopes
revision = '0009_artifact_upload_scopes'
down_revision = '0008_evidence_payloads'
branch_labels = None
depends_on = None

def upgrade():
    artifact_upload_scopes.create(op.get_bind(), checkfirst=False)

def downgrade():
    raise RuntimeError('artifact scope downgrade requires backup restoration')

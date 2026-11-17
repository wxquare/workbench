"""Preserve precise connector identity; legacy sources remain unproven."""
from alembic import op
from sqlalchemy import Column,String
revision='0016_synthetic_identity'
down_revision='0015_synthetic_result_bindings'
branch_labels=None
depends_on=None

def upgrade(): op.add_column('synthetic_alert_sources',Column('identity_id',String(128),nullable=True))
def downgrade(): raise RuntimeError('synthetic identity downgrade requires backup restoration')

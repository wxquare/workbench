"""Add durable final results and versioned public events, preserving legacy history."""
from alembic import op
from sqlalchemy import Column,String,BigInteger
from workbench_agent.persistence.schema_v4 import run_outcomes
revision='0004_outcomes_events'
down_revision='0003_run_timing'
branch_labels=None
depends_on=None

def upgrade():
    run_outcomes.create(op.get_bind(),checkfirst=False)
    op.add_column('run_events',Column('schema_version',String(16),nullable=False,server_default='1'))
    op.add_column('run_events',Column('message_id',String(36)))
    op.add_column('runs',Column('event_floor',BigInteger,nullable=False,server_default='0'))

def downgrade(): raise RuntimeError('result downgrade requires backup restoration')

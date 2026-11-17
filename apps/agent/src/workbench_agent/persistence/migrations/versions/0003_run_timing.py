"""Persist exact timing; refuse unobservable executed legacy history before DDL.

This migration was corrected during the unreleased T07 review. Historical
0001/0002 and their schema snapshots remain immutable. See the timing migration
runbook for the explicit offline reconciliation policy.
"""
from alembic import op
from sqlalchemy import Column, BigInteger, select
from workbench_agent.persistence.schema_v2 import metadata as previous
revision='0003_run_timing'
down_revision='0002_team_receipts'
branch_labels=None
depends_on=None

def upgrade():
    connection=op.get_bind()
    runs=previous.tables['runs']
    executed=connection.execute(select(runs.c.run_id).where(runs.c.run_epoch>0).limit(1)).first()
    if executed is not None:
        # A mutable deadline, created_at, updated_at or retained Events cannot
        # reconstruct first start or active intervals across waits/retries.
        raise RuntimeError('executed legacy Runs have unknown timing; migration stopped before DDL; '
            'restore/retain the backup and arrange reviewed offline reconciliation; '
            'no automatic reconstruction or data deletion is performed')
    op.add_column('runs',Column('started_at',BigInteger))
    op.add_column('runs',Column('active_elapsed_ms',BigInteger,nullable=False,server_default='0'))
    op.add_column('runs',Column('active_since',BigInteger))


def downgrade(): raise RuntimeError('timing downgrade requires backup restoration')

"""Add explicit team command receipts, preserving v1 Workspace receipts verbatim."""
from collections import defaultdict
from alembic import op
from sqlalchemy import insert, select, text
from workbench_agent.persistence.schema_v2 import team_idempotency_records
from workbench_agent.persistence.schema_v1 import metadata as historical_metadata

revision = '0002_team_receipts'
down_revision = '0001_core'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    team_idempotency_records.create(connection, checkfirst=False)
    old = historical_metadata.tables['idempotency_records']
    groups = defaultdict(list)
    for row in connection.execute(select(old).where(old.c.route.in_(('team.create', 'team.member')))).mappings():
        groups[(row['team_id'], row['subject'], row['route'], row['key'])].append(row)
    now = connection.execute(text("SELECT CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)")).scalar_one()
    for group in groups.values():
        active = [row for row in group if row['expires_at'] > now]
        signatures = {(row['request_hash'], row['response_ref'], row['status_code']) for row in active}
        if len(signatures) > 1:
            # Do not pretend duplicate side effects have been repaired by selecting a response.
            raise RuntimeError('ambiguous active team command receipts require reconciliation')
        # A live reuse window always wins over expired history. Identical live responses
        # preserve the latest persisted expiry, without inventing an extended retention.
        candidates = active or group
        original = max(candidates, key=lambda row: (row['expires_at'], row['created_at'], row['record_id']))
        values = {column.name: original[column.name] for column in team_idempotency_records.columns}
        connection.execute(insert(team_idempotency_records).values(**values))


def downgrade():
    raise RuntimeError('team receipt downgrade requires backup restoration')

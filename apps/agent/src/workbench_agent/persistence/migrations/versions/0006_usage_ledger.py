"""Preserve old history, refusing to invent network attempts for legacy usage."""
from alembic import op
from workbench_agent.persistence.schema_v4 import metadata as previous
from workbench_agent.persistence.schema_v5 import metadata,usage_quarantine,usage_budget_policies
revision='0006_usage_ledger'
down_revision='0005_resume_protocol'
branch_labels=None
depends_on=None

def upgrade():
    db=op.get_bind()
    # Existing records contain no network-attempt count or durable send marker.
    # Even a zero-valued cancelled hold cannot prove that no request was sent.
    bad=db.exec_driver_sql('SELECT 1 FROM usage_reservations LIMIT 1').first()
    records=db.exec_driver_sql('SELECT 1 FROM usage_records LIMIT 1').first()
    if bad or records:
        raise RuntimeError('ambiguous legacy usage requires offline reconciliation; no migration applied')
    for name in ('usage_reservations','usage_records'):
        for column in metadata.tables[name].columns:
            if column.name not in previous.tables[name].columns:
                op.add_column(name,column._copy())
    usage_quarantine.create(db,checkfirst=False)
    usage_budget_policies.create(db,checkfirst=False)

def downgrade(): raise RuntimeError('usage downgrade requires backup restoration')

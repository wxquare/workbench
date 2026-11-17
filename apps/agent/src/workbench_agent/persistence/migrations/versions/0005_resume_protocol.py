"""Canonicalize only unambiguous historical resume aliases, without rewriting history."""
from alembic import op
revision='0005_resume_protocol'
down_revision='0004_outcomes_events'
branch_labels=None
depends_on=None

def upgrade():
    db=op.get_bind()
    # Validate every alias before changing anything. Never infer a consumed input
    # from the Interrupt alone or adopt a request not selected by its CAS pointer.
    bad=db.exec_driver_sql('''SELECT 1 FROM resume_requests r LEFT JOIN interrupts i
        ON i.team_id=r.team_id AND i.run_id=r.run_id AND i.interrupt_id=r.interrupt_id
        WHERE r.status='accepted' AND (i.accepted_resume_request_id IS NULL
        OR i.accepted_resume_request_id!=r.request_id OR i.status!='response_submitted') LIMIT 1''').first()
    bad_interrupt=db.exec_driver_sql('''SELECT 1 FROM interrupts i LEFT JOIN resume_requests r
        ON r.team_id=i.team_id AND r.run_id=i.run_id AND r.interrupt_id=i.interrupt_id
        AND r.request_id=i.accepted_resume_request_id
        WHERE i.status='applied' AND (r.request_id IS NULL OR r.status!='applied') LIMIT 1''').first()
    if bad or bad_interrupt:
        raise RuntimeError('ambiguous legacy resume aliases require offline reconciliation; no migration applied')
    db.exec_driver_sql("UPDATE resume_requests SET status='submitted' WHERE status='accepted'")
    db.exec_driver_sql("UPDATE interrupts SET status='consumed' WHERE status='applied'")

def downgrade(): raise RuntimeError('resume protocol downgrade requires backup restoration')

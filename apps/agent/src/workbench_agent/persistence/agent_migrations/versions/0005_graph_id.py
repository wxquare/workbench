"""Replace graph version with graph ID only on an empty runtime database."""
from alembic import op

revision = '0005_graph_id'
down_revision = '0004_knowledge_base'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table in ('runtime_profiles', 'runs', 'run_jobs'):
        if connection.exec_driver_sql(f'SELECT 1 FROM {table} LIMIT 1').first():
            raise RuntimeError('Graph ID migration requires an empty database; clear or recreate it first')
    # SQLite's native rename preserves foreign keys to runtime_profiles.
    op.execute('ALTER TABLE runtime_profiles RENAME COLUMN graph_version TO graph_id')


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

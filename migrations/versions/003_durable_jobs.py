"""Job lifecycle, idempotency retention, attempt links and endpoint health."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column('terminal_at',sa.String), sa.Column('metadata_expires_at',sa.String),
                   sa.Column('idempotency_expires_at',sa.String), sa.Column('next_attempt_at',sa.String),
                   sa.Column('last_decision_id',sa.String), sa.Column('content_deleted_at',sa.String),
                   sa.Column('attempt_count',sa.Integer,nullable=False,server_default='0')]:
        op.add_column('jobs',column)
    op.add_column('attempts',sa.Column('job_id',sa.String))
    op.add_column('decisions',sa.Column('job_id',sa.String))
    for column in [sa.Column('consecutive_failures',sa.Integer,nullable=False,server_default='0'),
                   sa.Column('disabled_reason',sa.String),sa.Column('probe_after',sa.String)]:
        op.add_column('endpoints',column)
    op.create_index('ix_jobs_scheduling','jobs',['state','created_at'])
    op.create_index('ix_attempts_job','attempts',['job_id'])
    op.create_index('ix_decisions_job','decisions',['job_id'])


def downgrade():
    op.drop_index('ix_decisions_job',table_name='decisions')
    op.drop_index('ix_attempts_job',table_name='attempts')
    op.drop_index('ix_jobs_scheduling',table_name='jobs')
    for name in ['consecutive_failures','disabled_reason','probe_after']:
        op.drop_column('endpoints',name)
    op.drop_column('decisions','job_id')
    op.drop_column('attempts','job_id')
    for name in ['terminal_at','metadata_expires_at','idempotency_expires_at','next_attempt_at',
                 'last_decision_id','content_deleted_at','attempt_count']:
        op.drop_column('jobs',name)

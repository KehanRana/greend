"""Versioned estimate status and persistent non-destructive demo scenarios."""
from alembic import op
import sqlalchemy as sa

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column('status',sa.String),sa.Column('reason',sa.String),sa.Column('created_at',sa.String),
                   sa.Column('mode',sa.String),sa.Column('tokens',sa.Integer),sa.Column('usage_provenance',sa.String),
                   sa.Column('baseline_endpoint',sa.String),sa.Column('selected_endpoint',sa.String)]:
        op.add_column('emissions_estimates',column)
    op.create_table('demo_scenario',sa.Column('id',sa.Integer,primary_key=True),
                    sa.Column('scenario',sa.String,nullable=False),sa.Column('started_at',sa.String),
                    sa.Column('revision',sa.String,nullable=False))
    op.execute("INSERT INTO demo_scenario (id,scenario,revision) VALUES (1,'scheduled','initial')")


def downgrade():
    op.drop_table('demo_scenario')
    for name in ['status','reason','created_at','mode','tokens','usage_provenance','baseline_endpoint','selected_endpoint']:
        op.drop_column('emissions_estimates',name)

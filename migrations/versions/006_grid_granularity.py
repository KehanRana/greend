"""Preserve source granularity without inferring it for historical snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('grid_snapshots', sa.Column('temporal_granularity', sa.String))


def downgrade():
    op.drop_column('grid_snapshots', 'temporal_granularity')

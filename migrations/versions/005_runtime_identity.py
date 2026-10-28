"""Bind each database to one runtime mode and reviewed deployment manifest."""
from alembic import op
import sqlalchemy as sa

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('runtime_identity', sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('mode', sa.String, nullable=False), sa.Column('manifest_digest', sa.String))
    op.execute("INSERT INTO runtime_identity(id,mode) VALUES (1,'demo')")
    op.add_column('endpoints', sa.Column('locality_evidence', sa.String))


def downgrade():
    op.drop_column('endpoints', 'locality_evidence')
    op.drop_table('runtime_identity')

"""Explicit simulated grid zones and persisted quality metadata."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("endpoints", sa.Column("grid_zone", sa.String))
    for region, zone in [("us-east-1", "demo:virginia"), ("eu-central-1", "demo:frankfurt"),
                         ("ap-southeast-2", "demo:sydney")]:
        op.execute(sa.text("UPDATE endpoints SET grid_zone=:zone WHERE id=:region").bindparams(zone=zone, region=region))
    op.add_column("grid_snapshots", sa.Column("quality_flags", sa.JSON))
    op.create_index("ix_grid_snapshots_zone_retrieved", "grid_snapshots", ["zone", "retrieved_at"])


def downgrade():
    op.drop_index("ix_grid_snapshots_zone_retrieved", table_name="grid_snapshots")
    op.drop_column("grid_snapshots", "quality_flags")
    op.drop_column("endpoints", "grid_zone")

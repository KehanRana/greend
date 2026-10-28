"""Initial durable records and explicitly simulated endpoint registry."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("endpoints", sa.Column("id", sa.String, primary_key=True),
        sa.Column("region", sa.String, nullable=False), sa.Column("model", sa.String, nullable=False),
        sa.Column("model_version", sa.String, nullable=False), sa.Column("latency_rank", sa.Integer, nullable=False),
        sa.Column("healthy", sa.Boolean, nullable=False), sa.Column("locality", sa.String, nullable=False))
    endpoints = sa.table("endpoints", *[sa.column(x) for x in
        ["id", "region", "model", "model_version", "latency_rank", "healthy", "locality"]])
    op.bulk_insert(endpoints, [dict(id=r, region=r, model="demo-model", model_version="fixture-v1",
        latency_rank=i, healthy=True, locality="simulated") for i,r in enumerate(
        ["us-east-1", "eu-central-1", "ap-southeast-2"])])
    op.create_table("grid_snapshots", sa.Column("id", sa.String, primary_key=True),
        sa.Column("zone", sa.String), sa.Column("intensity", sa.Float),
        sa.Column("observed_at", sa.String), sa.Column("retrieved_at", sa.String),
        sa.Column("source", sa.String), sa.Column("mode", sa.String))
    op.create_table("jobs", sa.Column("id", sa.String, primary_key=True),
        sa.Column("owner_id", sa.String, nullable=False), sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("body_digest", sa.String, nullable=False), sa.Column("payload", sa.JSON),
        sa.Column("policy", sa.JSON), sa.Column("state", sa.String, nullable=False),
        sa.Column("created_at", sa.String), sa.Column("deadline_at", sa.String),
        sa.Column("lease_owner", sa.String), sa.Column("lease_expires_at", sa.String),
        sa.Column("result", sa.JSON), sa.Column("error", sa.JSON), sa.Column("cleanup_at", sa.String),
        sa.UniqueConstraint("owner_id", "idempotency_key"))
    op.create_table("decisions", sa.Column("request_id", sa.String, primary_key=True),
        sa.Column("created_at", sa.String, nullable=False), sa.Column("data", sa.JSON, nullable=False))
    op.create_table("attempts", sa.Column("id", sa.String, primary_key=True),
        sa.Column("request_id", sa.String, sa.ForeignKey("decisions.request_id"), nullable=False),
        sa.Column("endpoint_id", sa.String, sa.ForeignKey("endpoints.id"), nullable=False),
        sa.Column("started_at", sa.String, nullable=False), sa.Column("ended_at", sa.String),
        sa.Column("outcome", sa.String, nullable=False), sa.Column("upstream_request_id", sa.String),
        sa.Column("usage", sa.JSON), sa.Column("uncertain", sa.Boolean, nullable=False))
    op.create_table("emissions_estimates", sa.Column("id", sa.String, primary_key=True),
        sa.Column("execution_id", sa.String, sa.ForeignKey("attempts.id"), nullable=False, unique=True),
        sa.Column("method_version", sa.String), sa.Column("assumptions", sa.JSON),
        sa.Column("energy_kwh", sa.Float), sa.Column("actual_g", sa.Float),
        sa.Column("baseline_g", sa.Float), sa.Column("difference_g", sa.Float))


def downgrade():
    for name in ["emissions_estimates", "attempts", "decisions", "jobs", "grid_snapshots", "endpoints"]:
        op.drop_table(name)

"""Budgets, alerts, retention, and sampling.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from app.models import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "retention_days" not in columns:
        op.add_column("projects", sa.Column("retention_days", sa.Integer(), nullable=True))
    if "sample_rate" not in columns:
        op.add_column("projects", sa.Column("sample_rate", sa.Float(), nullable=True))
    op.execute("UPDATE projects SET retention_days = 30 WHERE retention_days IS NULL")
    op.execute("UPDATE projects SET sample_rate = 1.0 WHERE sample_rate IS NULL")
    for name in ("budgets", "alert_rules", "alert_events"):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in ("alert_events", "alert_rules", "budgets"):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column("projects", "sample_rate")
    op.drop_column("projects", "retention_days")

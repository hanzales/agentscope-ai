"""Project RBAC, webhooks, and deployment metadata.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import Base

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("traces")}
    additions = {
        "deployment_environment": sa.Column("deployment_environment", sa.String(120)),
        "service_version": sa.Column("service_version", sa.String(120)),
        "git_commit": sa.Column("git_commit", sa.String(64)),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("traces", column)
    for name in ("project_access_tokens", "project_members", "webhook_endpoints"):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("webhook_endpoints")
    op.drop_table("project_members")
    op.drop_table("project_access_tokens")
    op.drop_column("traces", "git_commit")
    op.drop_column("traces", "service_version")
    op.drop_column("traces", "deployment_environment")

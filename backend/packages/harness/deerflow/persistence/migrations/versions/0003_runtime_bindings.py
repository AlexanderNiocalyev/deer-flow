"""Add runtime bindings for external sandbox resources.

Revision ID: 0003_runtime_bindings
Revises: 0002_runs_token_usage
Create Date: 2026-07-01

The table stores DeerFlow-owned sandbox identifiers separately from
provider-owned runtime ids, e.g. ``vercel-...`` -> ``sbx_...``. This lets
production deployments such as Cloud Run recover Vercel Sandbox mappings after
process restart or instance rescheduling without relying on local JSON files.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_runtime_bindings"
down_revision: str | Sequence[str] | None = "0002_runs_token_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "runtime_bindings" in inspector.get_table_names():
        return

    op.create_table(
        "runtime_bindings",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("sandbox_id", sa.String(length=64), nullable=False),
        sa.Column("provider_sandbox_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=64), nullable=True),
        sa.Column("vcpus", sa.Integer(), nullable=True),
        sa.Column("memory_mb", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "sandbox_id", name="uq_runtime_bindings_provider_sandbox"),
    )
    with op.batch_alter_table("runtime_bindings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runtime_bindings_thread_id"), ["thread_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runtime_bindings_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_runtime_bindings_thread_provider", ["thread_id", "provider"], unique=False)
        batch_op.create_index("ix_runtime_bindings_user_provider", ["user_id", "provider"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "runtime_bindings" not in inspector.get_table_names():
        return
    op.drop_table("runtime_bindings")

"""ORM model for external runtime bindings.

Runtime bindings keep DeerFlow-owned sandbox/session identifiers separate from
provider-owned resources such as Vercel Sandbox ids. The table is intentionally
provider-neutral so future runtime providers can share the same production
mapping surface.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RuntimeBindingRow(Base):
    __tablename__ = "runtime_bindings"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_sandbox_id: Mapped[str] = mapped_column(String(128), nullable=False)

    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    runtime: Mapped[str | None] = mapped_column(String(64))
    vcpus: Mapped[int | None] = mapped_column(Integer)
    memory_mb: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("provider", "sandbox_id", name="uq_runtime_bindings_provider_sandbox"),
        Index("ix_runtime_bindings_thread_provider", "thread_id", "provider"),
        Index("ix_runtime_bindings_user_provider", "user_id", "provider"),
    )

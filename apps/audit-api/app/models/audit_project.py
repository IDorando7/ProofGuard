from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditProject(Base):
    __tablename__ = "audit_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="created")
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


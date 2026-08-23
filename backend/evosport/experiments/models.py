from datetime import datetime, timezone

from sqlalchemy import Column, DDL, DateTime, String, event

from models.database import Base


event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE SCHEMA IF NOT EXISTS evosport").execute_if(dialect="postgresql"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidencePublication(Base):
    __tablename__ = "evidence_publications"
    __table_args__ = {"schema": "evosport"}

    fingerprint = Column(String(64), primary_key=True)
    homerun_run_id = Column(String, nullable=False)
    dataset_manifest_id = Column(String(64), nullable=False)
    effective_dataset_sha256 = Column(String(64), nullable=False)
    artifact_manifest_sha256 = Column(String(64), nullable=False)
    result_sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

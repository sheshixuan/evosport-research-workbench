from __future__ import annotations

import inspect

import pytest

from evosport.experiments.models import EvidencePublication
from evosport.experiments.registry import InMemoryRunRegistry


@pytest.mark.asyncio
async def test_registry_publishes_only_minimal_immutable_evidence_binding() -> None:
    registry = InMemoryRunRegistry()
    published = await registry.publish(
        fingerprint="a" * 64,
        homerun_run_id="hr-1",
        dataset_manifest_id="b" * 64,
        effective_dataset_sha256="c" * 64,
        artifact_manifest_sha256="d" * 64,
        result_sha256="e" * 64,
    )

    assert await registry.get_by_fingerprint("a" * 64) == published
    assert set(published.__dataclass_fields__) == {
        "fingerprint",
        "homerun_run_id",
        "dataset_manifest_id",
        "effective_dataset_sha256",
        "artifact_manifest_sha256",
        "result_sha256",
    }
    assert not hasattr(registry, "mark_running")
    assert not hasattr(registry, "mark_succeeded")
    assert not hasattr(registry, "mark_failed")


def test_registry_model_has_no_duplicate_result_or_lifecycle_columns() -> None:
    assert EvidencePublication.__tablename__ == "evidence_publications"
    assert set(EvidencePublication.__table__.columns.keys()) == {
        "fingerprint",
        "homerun_run_id",
        "dataset_manifest_id",
        "effective_dataset_sha256",
        "artifact_manifest_sha256",
        "result_sha256",
        "created_at",
    }


def test_migration_downgrade_issues_idempotent_drop_never_raises() -> None:
    """The head migration's downgrade must be symmetric (roundtrip-able per
    tests/test_alembic_roundtrip.py) and safe on databases that never reached
    head: it drops ONLY evosport.evidence_publications, idempotently, and must
    not throw in a stubbed (no real bind) context.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/202608190001_create_evosport_registry.py"
    spec = importlib.util.spec_from_file_location("evosport_registry_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    source = inspect.getsource(migration.downgrade)
    assert "DROP TABLE IF EXISTS" in source
    assert "evosport.evidence_publications" in source

    class _MigrationOps:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, statement: str) -> None:
            self.executed.append(statement)

    operations = _MigrationOps()
    migration.op = operations
    migration.downgrade()  # must not raise
    assert operations.executed == ["DROP TABLE IF EXISTS evosport.evidence_publications"]

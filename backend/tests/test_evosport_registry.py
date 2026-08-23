from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from evosport.experiments.models import EvidencePublication
from evosport.experiments.registry import InMemoryRunRegistry, SqlRunRegistry
from models.database import Base
from models.model_registry import register_all_models
from tests.postgres_test_db import build_postgres_session_factory


def _values(**overrides: str) -> dict[str, str]:
    values = {
        "fingerprint": "a" * 64,
        "homerun_run_id": "hr-1",
        "dataset_manifest_id": "b" * 64,
        "effective_dataset_sha256": "c" * 64,
        "artifact_manifest_sha256": "d" * 64,
        "result_sha256": "e" * 64,
    }
    values.update(overrides)
    return values


class _MigrationOps:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.tables: dict[str, sa.Table] = {}

    def execute(self, statement: str) -> None:
        self.executed.append(statement)

    def create_table(self, name: str, *elements: object, schema: str) -> None:
        self.tables[name] = sa.Table(name, sa.MetaData(), *elements, schema=schema)


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/202608190001_create_evosport_registry.py"
    spec = spec_from_file_location("create_evosport_registry", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_postgres_metadata_bootstrap_creates_schema_before_minimal_table() -> None:
    register_all_models()
    statements: list[str] = []

    def capture(statement, *args, **kwargs) -> None:
        statements.append(str(statement.compile(dialect=engine.dialect)))

    engine = sa.create_mock_engine("postgresql://", capture)
    Base.metadata.create_all(bind=engine)

    schema_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("CREATE SCHEMA IF NOT EXISTS evosport")
    )
    table_index = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE evosport.evidence_publications" in statement
    )
    assert schema_index < table_index


def test_migration_matches_minimal_model_and_is_only_alembic_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _MigrationOps()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "202608190001"
    assert migration.down_revision == "202606160003"
    assert operations.executed == ["CREATE SCHEMA IF NOT EXISTS evosport"]
    assert set(operations.tables) == {"evidence_publications"}
    table = operations.tables["evidence_publications"]
    assert table.schema == EvidencePublication.__table__.schema
    assert set(table.columns.keys()) == set(EvidencePublication.__table__.columns.keys())
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == ["202608190001"]


@pytest.mark.asyncio
async def test_in_memory_registry_is_idempotent_only_for_identical_publication() -> None:
    registry = InMemoryRunRegistry()
    first = await registry.publish(**_values())

    assert await registry.publish(**_values()) == first
    with pytest.raises(ValueError, match="different evidence"):
        await registry.publish(**_values(result_sha256="f" * 64))


@pytest.mark.db
@pytest.mark.asyncio
async def test_sql_registry_round_trips_minimal_publication_in_real_postgres() -> None:
    register_all_models()
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_registry")
    try:
        registry = SqlRunRegistry(session_factory)

        published = await registry.publish(**_values())

        assert await registry.get_by_fingerprint("a" * 64) == published
        assert await registry.publish(**_values()) == published
        with pytest.raises(ValueError, match="different evidence"):
            await registry.publish(**_values(result_sha256="f" * 64))
    finally:
        await engine.dispose()

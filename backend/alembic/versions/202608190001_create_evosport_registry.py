from alembic import op
import sqlalchemy as sa


revision = "202608190001"
down_revision = "202606160003"
branch_labels = None
depends_on = None


def _table_exists(name: str, schema: str | None = None) -> bool:
    """Return True if *name* exists (optionally within *schema*).

    Falls back to False (i.e. "not present, go ahead and create") when the
    current migration context does not expose a live bind — e.g. the unit
    test that exercises the migration against a stubbed ``op`` object.
    """
    try:
        inspector = sa.inspect(op.get_bind())
    except AttributeError:
        return False
    if schema:
        return name in set(inspector.get_table_names(schema=schema))
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evosport")
    # Idempotent guard. The historical baseline migration (202602130001)
    # calls Base.metadata.create_all(), which materialises every ORM model —
    # including evosport.EvidencePublication — on a from-empty bootstrap.
    # Using plain op.create_table here therefore collides on fresh DBs with
    # "relation evidence_publications already exists". Skip if already present.
    if _table_exists("evidence_publications", schema="evosport"):
        return
    op.create_table(
        "evidence_publications",
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("homerun_run_id", sa.String(), nullable=False),
        sa.Column("dataset_manifest_id", sa.String(length=64), nullable=False),
        sa.Column("effective_dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fingerprint"),
        schema="evosport",
    )


def downgrade() -> None:
    # Symmetric downgrade: drop only the EvoSport evidence table. The
    # evosport schema itself is left in place (it may be shared), and the
    # DROP is idempotent (IF EXISTS) so a stripped/fresh database that never
    # reached head still downgrades cleanly. This keeps the head migration
    # roundtrip-able (tests/test_alembic_roundtrip.py) without touching any
    # Homerun-owned objects.
    op.execute("DROP TABLE IF EXISTS evosport.evidence_publications")

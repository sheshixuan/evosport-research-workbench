from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from evosport.experiments.models import EvidencePublication


@dataclass(frozen=True)
class RunRecord:
    fingerprint: str
    homerun_run_id: str
    dataset_manifest_id: str
    effective_dataset_sha256: str
    artifact_manifest_sha256: str
    result_sha256: str


class RunRegistry(Protocol):
    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None: ...

    async def publish(
        self,
        *,
        fingerprint: str,
        homerun_run_id: str,
        dataset_manifest_id: str,
        effective_dataset_sha256: str,
        artifact_manifest_sha256: str,
        result_sha256: str,
    ) -> RunRecord: ...


def _record_from_values(
    *,
    fingerprint: str,
    homerun_run_id: str,
    dataset_manifest_id: str,
    effective_dataset_sha256: str,
    artifact_manifest_sha256: str,
    result_sha256: str,
) -> RunRecord:
    return RunRecord(
        fingerprint=fingerprint,
        homerun_run_id=homerun_run_id,
        dataset_manifest_id=dataset_manifest_id,
        effective_dataset_sha256=effective_dataset_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
        result_sha256=result_sha256,
    )


class InMemoryRunRegistry:
    def __init__(self) -> None:
        self._by_fingerprint: dict[str, RunRecord] = {}

    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None:
        return self._by_fingerprint.get(fingerprint)

    async def publish(self, **values: str) -> RunRecord:
        record = _record_from_values(**values)
        existing = self._by_fingerprint.get(record.fingerprint)
        if existing is not None:
            if existing != record:
                raise ValueError("fingerprint already published with different evidence")
            return existing
        self._by_fingerprint[record.fingerprint] = record
        return record


def _to_record(row: EvidencePublication) -> RunRecord:
    return _record_from_values(
        fingerprint=str(row.fingerprint),
        homerun_run_id=str(row.homerun_run_id),
        dataset_manifest_id=str(row.dataset_manifest_id),
        effective_dataset_sha256=str(row.effective_dataset_sha256),
        artifact_manifest_sha256=str(row.artifact_manifest_sha256),
        result_sha256=str(row.result_sha256),
    )


class SqlRunRegistry:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EvidencePublication).where(EvidencePublication.fingerprint == fingerprint)
                )
            ).scalar_one_or_none()
            return _to_record(row) if row is not None else None

    async def publish(self, **values: str) -> RunRecord:
        record = _record_from_values(**values)
        async with self._session_factory() as session:
            session.add(EvidencePublication(**values))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                existing = await self.get_by_fingerprint(record.fingerprint)
                if existing is None or existing != record:
                    raise ValueError(
                        "fingerprint already published with different evidence"
                    ) from exc
                return existing
        return record

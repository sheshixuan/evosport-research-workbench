from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evosport.data.freeze import load_verified_snapshot, materialize_verified_market_data
from evosport.data.manifest import CATALOG_SCHEMA_VERSION, DatasetManifest
from evosport.experiments.environment import EnvironmentIdentity, verify_environment_lock
from evosport.experiments.fingerprint import compute_run_fingerprint
from evosport.experiments.gateway import BacktestGateway, BacktestRequest
from evosport.experiments.registry import RunRecord, RunRegistry
from evosport.experiments.spec import ExperimentSpec, load_experiment_spec
from evosport.reports.render import render_report, render_report_bytes
from evosport.semantics.football_binding import (
    frozen_projected_market_identity,
    load_current_football_identity,
    validate_effective_football_evidence,
)
from utils.logger import get_logger


_ARTIFACT_SCHEMA_VERSION = "evosport.artifact.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DECISION = {
    "decision": "NOT_EVALUATED",
    "reason": "P0-P2 records reproducible backtests only; statistical promotion is unavailable.",
}
_ROOT_ARTIFACT_FILES = {
    "experiment.yaml",
    "dataset-manifest.json",
    "environment.lock",
    "environment.json",
    "result.json",
    "decision.json",
    "report.html",
    "artifact-manifest.json",
}
logger = get_logger(__name__)


@dataclass(frozen=True)
class ExperimentOutcome:
    run_id: str
    fingerprint: str
    status: str
    decision: str
    dataset_manifest_id: str
    homerun_run_id: str
    artifact_dir: Path
    result: dict[str, Any]


@dataclass(frozen=True)
class _OwnedArtifactDirectory:
    path: Path
    entries: tuple["_OwnedArtifactEntry", ...]


@dataclass(frozen=True)
class _OwnedArtifactEntry:
    relative_path: str
    kind: str
    device: int
    inode: int


@dataclass(frozen=True)
class _OwnedArtifactRoot:
    path: Path
    device: int
    inode: int


class ExperimentRunner:
    def __init__(
        self,
        *,
        registry: RunRegistry,
        gateway: BacktestGateway,
        artifact_root: Path,
        homerun_commit: str,
        evaluator_version: str,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._artifact_root = artifact_root
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._homerun_commit = homerun_commit
        self._evaluator_version = evaluator_version

    async def run(self, spec_path: Path) -> ExperimentOutcome:
        resolved_spec_path = spec_path.resolve()
        spec = load_experiment_spec(resolved_spec_path)
        manifest_path = _resolve(resolved_spec_path.parent, spec.dataset_manifest_path)
        source_path = _resolve(resolved_spec_path.parent, spec.strategy.source_path)
        lock_path = _resolve(resolved_spec_path.parent, spec.strategy.dependency_lock_path)
        manifest, manifest_bytes = load_verified_snapshot(manifest_path)
        if manifest.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError("only a catalog manifest is run eligible")
        if (
            spec.window.validation_start != manifest.start
            or spec.window.validation_end != manifest.end
        ):
            raise ValueError("experiment validation window must exactly match the catalog manifest window")
        strategy_bytes = source_path.read_bytes()
        lock_bytes = lock_path.read_bytes()
        environment = verify_environment_lock(lock_bytes)
        fingerprint = compute_run_fingerprint(
            spec=spec,
            strategy_bytes=strategy_bytes,
            lock_bytes=lock_bytes,
            manifest_bytes=manifest_bytes,
            evaluator_version=self._evaluator_version,
            homerun_commit=self._homerun_commit,
            environment_identity_bytes=environment.canonical_bytes(),
        )
        artifact_dir = self._artifact_root / fingerprint
        current_identity = await _load_current_execution_identity(manifest)
        cached = await self._registry.get_by_fingerprint(fingerprint)
        if cached is not None:
            if cached.dataset_manifest_id != manifest.manifest_id:
                raise RuntimeError("cached publication dataset manifest does not match fingerprint inputs")
            result = _load_result_artifact(artifact_dir)
            homerun_run_id = _validate_gateway_result(result, manifest, current_identity["football"])
            if homerun_run_id != cached.homerun_run_id:
                raise RuntimeError("cached result does not match published Homerun run ID")
            evidence = _evidence_bytes(
                spec=spec,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                strategy_bytes=strategy_bytes,
                lock_bytes=lock_bytes,
                environment=environment,
                result=result,
                homerun_run_id=homerun_run_id,
                run_id=homerun_run_id,
                fingerprint=fingerprint,
            )
            _verify_artifact_package(
                artifact_dir,
                run_id=homerun_run_id,
                fingerprint=fingerprint,
                expected_evidence=evidence,
            )
            if _sha256_file(artifact_dir / "result.json") != cached.result_sha256:
                raise RuntimeError("cached result hash does not match publication record")
            if _sha256_file(artifact_dir / "artifact-manifest.json") != cached.artifact_manifest_sha256:
                raise RuntimeError("cached artifact manifest hash does not match publication record")
            effective_sha256 = _effective_dataset_sha256(result)
            if effective_sha256 != cached.effective_dataset_sha256:
                raise RuntimeError("cached effective dataset hash does not match publication record")
            return _outcome(
                fingerprint=fingerprint,
                manifest=manifest,
                homerun_run_id=homerun_run_id,
                artifact_dir=artifact_dir,
                result=result,
            )

        if os.path.lexists(artifact_dir):
            result = _load_result_artifact(artifact_dir)
            homerun_run_id = _validate_gateway_result(result, manifest, current_identity["football"])
            evidence = _evidence_bytes(
                spec=spec,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                strategy_bytes=strategy_bytes,
                lock_bytes=lock_bytes,
                environment=environment,
                result=result,
                homerun_run_id=homerun_run_id,
                run_id=homerun_run_id,
                fingerprint=fingerprint,
            )
            _verify_artifact_package(
                artifact_dir,
                run_id=homerun_run_id,
                fingerprint=fingerprint,
                expected_evidence=evidence,
            )
            await _publish_with_reconciliation(
                self._registry,
                fingerprint=fingerprint,
                homerun_run_id=homerun_run_id,
                dataset_manifest_id=manifest.manifest_id,
                effective_dataset_sha256=_effective_dataset_sha256(result),
                artifact_manifest_sha256=_sha256_file(artifact_dir / "artifact-manifest.json"),
                result_sha256=_sha256_file(artifact_dir / "result.json"),
            )
            return _outcome(
                fingerprint=fingerprint,
                manifest=manifest,
                homerun_run_id=homerun_run_id,
                artifact_dir=artifact_dir,
                result=result,
            )

        identity_before = await _load_current_execution_identity(manifest)
        with materialize_verified_market_data(manifest) as selected_market_data:
            gateway_result = await self._gateway.run(
                _backtest_request(
                    spec,
                    manifest,
                    strategy_bytes,
                    market_data_view=selected_market_data.view,
                )
            )
            selected_market_data.verify()
        identity_after = await _load_current_execution_identity(manifest)
        if identity_after != identity_before:
            raise RuntimeError(
                "current football catalog, settlement, or selected coverage changed "
                "during gateway execution"
            )
        result = _strict_json_object(gateway_result)
        homerun_run_id = _validate_gateway_result(result, manifest, identity_after["football"])
        evidence = _evidence_bytes(
            spec=spec,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            strategy_bytes=strategy_bytes,
            lock_bytes=lock_bytes,
            environment=environment,
            result=result,
            homerun_run_id=homerun_run_id,
            run_id=homerun_run_id,
            fingerprint=fingerprint,
        )
        published = _publish_artifacts(
            artifact_root=self._artifact_root,
            artifact_dir=artifact_dir,
            run_id=homerun_run_id,
            publication_key=fingerprint,
            fingerprint=fingerprint,
            evidence=evidence,
            manifest=manifest,
            homerun_run_id=homerun_run_id,
            result=result,
        )
        await _publish_with_reconciliation(
            self._registry,
            owned_artifact=published,
            fingerprint=fingerprint,
            homerun_run_id=homerun_run_id,
            dataset_manifest_id=manifest.manifest_id,
            effective_dataset_sha256=_effective_dataset_sha256(result),
            artifact_manifest_sha256=_sha256_file(published.path / "artifact-manifest.json"),
            result_sha256=_sha256_file(published.path / "result.json"),
        )
        return _outcome(
            fingerprint=fingerprint,
            manifest=manifest,
            homerun_run_id=homerun_run_id,
            artifact_dir=published.path,
            result=result,
        )


async def _publish_with_reconciliation(
    registry: RunRegistry,
    *,
    fingerprint: str,
    homerun_run_id: str,
    dataset_manifest_id: str,
    effective_dataset_sha256: str,
    artifact_manifest_sha256: str,
    result_sha256: str,
    owned_artifact: _OwnedArtifactDirectory | None = None,
) -> RunRecord:
    intended = RunRecord(
        fingerprint=fingerprint,
        homerun_run_id=homerun_run_id,
        dataset_manifest_id=dataset_manifest_id,
        effective_dataset_sha256=effective_dataset_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
        result_sha256=result_sha256,
    )
    try:
        return await registry.publish(
            fingerprint=fingerprint,
            homerun_run_id=homerun_run_id,
            dataset_manifest_id=dataset_manifest_id,
            effective_dataset_sha256=effective_dataset_sha256,
            artifact_manifest_sha256=artifact_manifest_sha256,
            result_sha256=result_sha256,
        )
    except Exception as publish_error:
        try:
            observed = await registry.get_by_fingerprint(fingerprint)
        except Exception as lookup_error:
            _add_note(
                publish_error,
                "publication reconciliation lookup failed; evidence was preserved: "
                f"{lookup_error}",
            )
            raise publish_error
        if observed == intended:
            return observed
        if observed is not None:
            raise RuntimeError(
                "publication conflict after an ambiguous publish; evidence was preserved "
                f"for fingerprint {fingerprint}"
            ) from publish_error
        if owned_artifact is not None:
            if not _cleanup_owned_directory(owned_artifact, original_error=publish_error):
                _add_note(
                    publish_error,
                    f"unregistered evidence was preserved at {owned_artifact.path}",
                )
        raise publish_error


async def _load_current_execution_identity(manifest: DatasetManifest) -> dict[str, object]:
    football = await load_current_football_identity(
        manifest.football,
        manifest.provider_dataset_ids,
    )
    coverage = await _resolve_manifest_coverage_identity(manifest)
    return {"football": football, "coverage": coverage}


async def _resolve_manifest_coverage_identity(manifest: DatasetManifest) -> dict[str, object]:
    from services.marketdata.coverage import resolve_coverage

    coverage = await resolve_coverage(
        token_ids=manifest.token_ids,
        start=manifest.start,
        end=manifest.end,
        dataset_ids=manifest.provider_dataset_ids,
        ensure_scan=False,
    )
    expected_dataset_ids_by_path = {
        item.path: item.provider_dataset_ids for item in manifest.files
    }
    actual_paths = set(coverage.all_files())
    if actual_paths != set(expected_dataset_ids_by_path):
        raise ValueError("current selected coverage paths do not match the frozen manifest")
    actual_dataset_ids_by_path = {
        path: coverage.dataset_ids_by_path.get(path, ()) for path in sorted(actual_paths)
    }
    if actual_dataset_ids_by_path != expected_dataset_ids_by_path:
        raise ValueError("current selected coverage lineage does not match the frozen manifest")

    expected_paths_by_token = {
        token_id: tuple(
            item.path for item in manifest.files if token_id in item.token_ids
        )
        for token_id in manifest.token_ids
    }
    actual_paths_by_token = {
        token_id: tuple(sorted(coverage.files_for(token_id)))
        for token_id in manifest.token_ids
    }
    if actual_paths_by_token != expected_paths_by_token:
        raise ValueError("current selected token coverage does not match the frozen manifest")
    return {
        "paths": sorted(actual_paths),
        "dataset_ids_by_path": {
            path: list(dataset_ids)
            for path, dataset_ids in actual_dataset_ids_by_path.items()
        },
        "paths_by_token": {
            token_id: list(paths) for token_id, paths in actual_paths_by_token.items()
        },
    }


def _backtest_request(
    spec: ExperimentSpec,
    manifest: DatasetManifest,
    strategy_bytes: bytes,
    *,
    market_data_view: Any,
) -> BacktestRequest:
    return BacktestRequest(
        source_code=strategy_bytes.decode("utf-8"),
        slug=spec.strategy.slug,
        config=spec.strategy.config,
        token_ids=manifest.token_ids,
        provider_dataset_ids=manifest.provider_dataset_ids,
        market_data_view=market_data_view,
        projected_market=frozen_projected_market_identity(manifest.football),
        start=spec.window.validation_start.isoformat(),
        end=spec.window.validation_end.isoformat(),
        initial_capital_usd=spec.execution.initial_capital_usd,
        submit_p50_ms=spec.execution.submit_p50_ms,
        submit_p95_ms=spec.execution.submit_p95_ms,
        cancel_p50_ms=spec.execution.cancel_p50_ms,
        cancel_p95_ms=spec.execution.cancel_p95_ms,
        seed=spec.seed,
        n_trials=spec.max_trials,
    )


def _canonical_experiment_bytes(spec: ExperimentSpec) -> bytes:
    return yaml.safe_dump(
        spec.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")


def _resolve(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()


def _strict_json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("gateway result must be a strict JSON object")
    return json.loads(_strict_json_bytes(value))


def _strict_json_bytes(value: object) -> bytes:
    _validate_json_value(value, seen=set())
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be strict JSON") from exc


def _validate_json_value(value: object, *, seen: set[int]) -> None:
    if value is None or isinstance(value, bool | str | int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("value must be strict JSON with finite numbers")
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            raise ValueError("value must be strict JSON without cycles")
        seen.add(identity)
        try:
            for item in value:
                _validate_json_value(item, seen=seen)
        finally:
            seen.remove(identity)
        return
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            raise ValueError("value must be strict JSON without cycles")
        seen.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("value must be strict JSON with string object keys")
                _validate_json_value(item, seen=seen)
        finally:
            seen.remove(identity)
        return
    raise ValueError("value must be strict JSON")


def _homerun_run_id(result: dict[str, Any]) -> str:
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
        raise ValueError("gateway result must contain a usable non-empty run_id")
    return run_id


def _validate_gateway_result(
    result: dict[str, Any],
    manifest: DatasetManifest,
    current_football: dict[str, object],
) -> str:
    homerun_run_id = _homerun_run_id(result)
    execution = result.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("gateway result must contain execution evidence")
    if execution.get("success") is not True:
        raise ValueError("gateway execution did not succeed")
    validation_errors = execution.get("validation_errors")
    if validation_errors != []:
        raise ValueError("gateway execution has validation errors")
    if execution.get("runtime_error") is not None:
        raise ValueError("gateway execution has a runtime error")
    if not isinstance(execution.get("settlement_summary"), dict):
        raise ValueError("gateway execution is missing settlement evidence")

    effective = result.get("effective_dataset")
    if not isinstance(effective, dict):
        raise ValueError("gateway result is missing effective dataset evidence")
    actual_dataset_ids = effective.get("provider_dataset_ids")
    if actual_dataset_ids != list(manifest.provider_dataset_ids):
        raise ValueError("effective provider dataset IDs do not match the manifest")
    entries = effective.get("entries")
    if not isinstance(entries, list):
        raise ValueError("effective dataset files are malformed")
    expected_by_path = {item.path: item for item in manifest.files}
    actual_by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("effective dataset files are malformed")
        path = entry.get("path")
        if not isinstance(path, str) or path in actual_by_path:
            raise ValueError("effective dataset files contain an invalid or duplicate path")
        actual_by_path[path] = entry
    if set(actual_by_path) != set(expected_by_path):
        raise ValueError("effective dataset files do not match the manifest")
    for path, expected in expected_by_path.items():
        actual = actual_by_path[path]
        if (
            actual.get("sha256") != expected.sha256
            or actual.get("size_bytes") != expected.size_bytes
            or actual.get("provider_dataset_ids") != list(expected.provider_dataset_ids)
            or sorted(actual.get("token_ids") or []) != list(expected.token_ids)
        ):
            raise ValueError(f"effective dataset files do not match the manifest: {path}")

    expected_content_hash = _manifest_effective_content_hash(manifest)
    if effective.get("content_hash") != expected_content_hash:
        raise ValueError("effective dataset content hash does not match the manifest")
    validate_effective_football_evidence(
        manifest.football,
        manifest.provider_dataset_ids,
        current_football,
        result.get("effective_football"),
    )
    return homerun_run_id


def _manifest_effective_content_hash(manifest: DatasetManifest) -> str:
    digest = hashlib.sha256()
    for item in sorted(manifest.files, key=lambda value: value.path or ""):
        normalized_path = item.path.replace("\\", "/")
        fingerprint = f"{normalized_path}|{item.sha256}|{item.size_bytes}"
        digest.update(fingerprint.encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _effective_dataset_sha256(result: dict[str, Any]) -> str:
    effective = result.get("effective_dataset")
    if not isinstance(effective, dict):
        raise ValueError("gateway result is missing effective dataset evidence")
    content_hash = effective.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash.startswith("sha256:"):
        raise ValueError("effective dataset content hash is malformed")
    digest = content_hash.removeprefix("sha256:")
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("effective dataset content hash is malformed")
    return digest


def _load_result_artifact(artifact_dir: Path) -> dict[str, Any]:
    try:
        path = artifact_dir / "result.json"
        _require_regular_file(path)
        return _strict_json_object(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cached result artifact is invalid at {artifact_dir}") from exc


def _publish_artifacts(
    *,
    artifact_root: Path,
    artifact_dir: Path,
    run_id: str,
    publication_key: str,
    fingerprint: str,
    evidence: dict[str, bytes],
    manifest: DatasetManifest,
    homerun_run_id: str,
    result: dict[str, Any],
) -> _OwnedArtifactDirectory:
    staging = Path(tempfile.mkdtemp(prefix=f".{publication_key}.", dir=artifact_root))
    owned_staging_root = _owned_artifact_root(staging)
    owned_staging: _OwnedArtifactDirectory | None = None
    published: _OwnedArtifactDirectory | None = None
    staging_cleaned = False
    try:
        for relative_path, content in evidence.items():
            if relative_path != "report.html":
                destination = staging / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        render_report(
            run_id=run_id,
            fingerprint=fingerprint,
            manifest=manifest,
            homerun_run_id=homerun_run_id,
            result=result,
            decision=_DECISION,
            artifact_dir=staging,
        )
        if (staging / "report.html").read_bytes() != evidence["report.html"]:
            raise RuntimeError("deterministic report rendering produced unexpected evidence bytes")
        (staging / "artifact-manifest.json").write_bytes(
            _artifact_manifest_bytes(run_id=run_id, fingerprint=fingerprint, evidence=evidence)
        )
        _verify_artifact_package(
            staging,
            run_id=run_id,
            fingerprint=fingerprint,
            expected_evidence=evidence,
            require_read_only=False,
        )
        _make_evidence_read_only(staging)
        _verify_artifact_package(
            staging,
            run_id=run_id,
            fingerprint=fingerprint,
            expected_evidence=evidence,
        )
        owned_staging = _owned_artifact_tree(staging)
        published = _publish_staging_directory(
            artifact_root,
            publication_key,
            staging,
            artifact_dir,
            owned_staging,
        )
        return published
    except Exception as exc:
        _cleanup_staging_directory(owned_staging_root, owned_staging, original_error=exc)
        staging_cleaned = True
        raise
    finally:
        if published is None and not staging_cleaned:
            _cleanup_staging_directory(owned_staging_root, owned_staging)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_strict_json_bytes(value) + b"\n")


def _evidence_bytes(
    *,
    spec: ExperimentSpec,
    manifest: DatasetManifest,
    manifest_bytes: bytes,
    strategy_bytes: bytes,
    lock_bytes: bytes,
    environment: EnvironmentIdentity,
    result: dict[str, Any],
    homerun_run_id: str,
    run_id: str,
    fingerprint: str,
) -> dict[str, bytes]:
    strategy_filename = spec.strategy.source_path.name
    if not strategy_filename or strategy_filename in {".", ".."} or "/" in strategy_filename:
        raise ValueError("strategy source filename is unsafe")
    return {
        "experiment.yaml": _canonical_experiment_bytes(spec),
        "dataset-manifest.json": manifest_bytes,
        f"strategy-package/{strategy_filename}": strategy_bytes,
        "environment.lock": lock_bytes,
        "environment.json": environment.canonical_bytes() + b"\n",
        "result.json": _strict_json_bytes(result) + b"\n",
        "decision.json": _strict_json_bytes(_DECISION) + b"\n",
        "report.html": render_report_bytes(
            run_id=run_id,
            fingerprint=fingerprint,
            manifest=manifest,
            homerun_run_id=homerun_run_id,
            result=result,
            decision=_DECISION,
        ),
    }


def _artifact_manifest_bytes(*, run_id: str, fingerprint: str, evidence: dict[str, bytes]) -> bytes:
    records = [
        {
            "relative_path": relative_path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        for relative_path, content in sorted(evidence.items())
    ]
    return (
        _strict_json_bytes(
            {
                "schema_version": _ARTIFACT_SCHEMA_VERSION,
                "run_id": run_id,
                "fingerprint": fingerprint,
                "artifacts": records,
            }
        )
        + b"\n"
    )


def _verify_artifact_package(
    artifact_dir: Path,
    *,
    run_id: str,
    fingerprint: str,
    expected_evidence: dict[str, bytes],
    require_read_only: bool = True,
) -> None:
    try:
        _require_regular_directory(artifact_dir)
        if require_read_only:
            _require_read_only(artifact_dir)
        entries = {entry.name: entry for entry in artifact_dir.iterdir()}
        expected_entries = _ROOT_ARTIFACT_FILES | {"strategy-package"}
        if set(entries) != expected_entries:
            raise ValueError("artifact tree has missing or extra root paths")
        for filename in _ROOT_ARTIFACT_FILES:
            _require_regular_file(entries[filename])
            if require_read_only:
                _require_read_only(entries[filename])
        strategy_dir = entries["strategy-package"]
        _require_regular_directory(strategy_dir)
        if require_read_only:
            _require_read_only(strategy_dir)
        records = _load_artifact_manifest(entries["artifact-manifest.json"], run_id, fingerprint)
        evidence_files = _evidence_files(artifact_dir)
        actual_paths = [relative_path for relative_path, _ in evidence_files]
        record_paths = [record["relative_path"] for record in records]
        if record_paths != actual_paths:
            raise ValueError("artifact manifest paths do not match the exact artifact tree")
        for record, (_, path) in zip(records, evidence_files, strict=True):
            if path.stat().st_size != record["size_bytes"] or _sha256_file(path) != record["sha256"]:
                raise ValueError(f"artifact hash or size mismatch: {record['relative_path']}")
        if set(expected_evidence) != set(actual_paths):
            raise ValueError("artifact evidence paths do not match the registry-anchored expectation")
        for relative_path, path in evidence_files:
            if require_read_only:
                _require_read_only(path)
            if path.read_bytes() != expected_evidence[relative_path]:
                raise ValueError(f"artifact evidence does not match captured inputs: {relative_path}")
        if entries["artifact-manifest.json"].read_bytes() != _artifact_manifest_bytes(
            run_id=run_id,
            fingerprint=fingerprint,
            evidence=expected_evidence,
        ):
            raise ValueError("artifact manifest does not match the registry-anchored expectation")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"artifact package verification failed for {artifact_dir}") from exc


def _load_artifact_manifest(path: Path, run_id: str, fingerprint: str) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    if not isinstance(document, dict) or set(document) != {"schema_version", "run_id", "fingerprint", "artifacts"}:
        raise ValueError("artifact manifest has malformed or unknown fields")
    if document["schema_version"] != _ARTIFACT_SCHEMA_VERSION:
        raise ValueError("artifact manifest schema version is unsupported")
    if document["run_id"] != run_id or document["fingerprint"] != fingerprint:
        raise ValueError("artifact manifest run identity does not match the registry")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list):
        raise ValueError("artifact manifest artifacts must be a list")
    records: list[dict[str, object]] = []
    for record in artifacts:
        if not isinstance(record, dict) or set(record) != {"relative_path", "sha256", "size_bytes"}:
            raise ValueError("artifact manifest record has malformed or unknown fields")
        relative_path = record["relative_path"]
        sha256 = record["sha256"]
        size_bytes = record["size_bytes"]
        if (
            not isinstance(relative_path, str)
            or not _is_safe_evidence_path(relative_path)
            or not isinstance(sha256, str)
            or _SHA256_PATTERN.fullmatch(sha256) is None
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise ValueError("artifact manifest record has invalid fields")
        records.append(record)
    paths = [record["relative_path"] for record in records]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("artifact manifest records must be unique and sorted")
    return records


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _evidence_files(artifact_dir: Path) -> list[tuple[str, Path]]:
    strategy_dir = artifact_dir / "strategy-package"
    _require_regular_directory(strategy_dir)
    strategy_entries = list(strategy_dir.iterdir())
    if len(strategy_entries) != 1:
        raise ValueError("strategy package must contain exactly one source file")
    strategy_file = strategy_entries[0]
    _require_regular_file(strategy_file)
    files = [
        ("dataset-manifest.json", artifact_dir / "dataset-manifest.json"),
        ("decision.json", artifact_dir / "decision.json"),
        ("environment.lock", artifact_dir / "environment.lock"),
        ("environment.json", artifact_dir / "environment.json"),
        ("experiment.yaml", artifact_dir / "experiment.yaml"),
        ("report.html", artifact_dir / "report.html"),
        ("result.json", artifact_dir / "result.json"),
        (f"strategy-package/{strategy_file.name}", strategy_file),
    ]
    for _, path in files:
        _require_regular_file(path)
    return sorted(files, key=lambda item: item[0])


def _is_safe_evidence_path(value: str) -> bool:
    return value in {
        "dataset-manifest.json",
        "decision.json",
        "environment.lock",
        "environment.json",
        "experiment.yaml",
        "report.html",
        "result.json",
    } or (
        value.startswith("strategy-package/")
        and value.count("/") == 1
        and value.removeprefix("strategy-package/") not in {"", ".", ".."}
        and "\\" not in value
    )


def _require_regular_directory(path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError(f"artifact path is not a regular directory: {path}")


def _require_regular_file(path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError(f"artifact path is not a regular file: {path}")


def _require_read_only(path: Path) -> None:
    if path.lstat().st_mode & 0o222:
        raise ValueError(f"artifact path is writable: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_evidence_read_only(artifact_dir: Path) -> None:
    for _, path in _evidence_files(artifact_dir):
        os.chmod(path, 0o444)
    os.chmod(artifact_dir / "artifact-manifest.json", 0o444)
    os.chmod(artifact_dir / "strategy-package", 0o555)
    os.chmod(artifact_dir, 0o555)


def _publish_staging_directory(
    artifact_root: Path,
    run_id: str,
    staging: Path,
    artifact_dir: Path,
    owned_staging: _OwnedArtifactDirectory,
) -> _OwnedArtifactDirectory:
    lock_path = _create_publication_lock(artifact_root, run_id)
    published = False
    try:
        if os.path.lexists(artifact_dir):
            raise RuntimeError(f"artifact directory already exists: {artifact_dir}")
        os.rename(staging, artifact_dir)
        published = True
    except Exception as exc:
        _release_publication_lock(lock_path, published=published, original_error=exc)
        raise
    else:
        _release_publication_lock(lock_path, published=True)
        return _OwnedArtifactDirectory(path=artifact_dir, entries=owned_staging.entries)


def _create_publication_lock(artifact_root: Path, run_id: str) -> Path:
    lock_path = artifact_root / f".{run_id}.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"run publication lock exists at {lock_path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
        lock_file.write(f"pid={os.getpid()}\n")
    return lock_path


def _release_publication_lock(
    lock_path: Path,
    *,
    published: bool,
    original_error: Exception | None = None,
) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        if published:
            logger.warning(
                "Run evidence was published but its lock could not be removed; remove the stale lock before retrying",
                lock_path=str(lock_path),
                exc_info=exc,
            )
            return
        if original_error is not None:
            _add_note(original_error, f"publication lock cleanup also failed: {exc}")
            return
        raise


def _owned_artifact_tree(path: Path) -> _OwnedArtifactDirectory:
    entries: list[_OwnedArtifactEntry] = []

    def visit(current: Path, relative_path: str) -> None:
        state = current.lstat()
        if stat.S_ISLNK(state.st_mode):
            raise RuntimeError(f"owned artifact tree contains a symlink: {current}")
        if stat.S_ISDIR(state.st_mode):
            kind = "directory"
        elif stat.S_ISREG(state.st_mode):
            kind = "file"
        else:
            raise RuntimeError(f"owned artifact tree contains a special path: {current}")
        entries.append(
            _OwnedArtifactEntry(
                relative_path=relative_path,
                kind=kind,
                device=state.st_dev,
                inode=state.st_ino,
            )
        )
        if kind == "directory":
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                child_relative_path = child.name if relative_path == "." else f"{relative_path}/{child.name}"
                visit(child, child_relative_path)

    visit(path, ".")
    return _OwnedArtifactDirectory(path=path, entries=tuple(entries))


def _owned_artifact_root(path: Path) -> _OwnedArtifactRoot:
    state = path.lstat()
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise RuntimeError(f"owned artifact root is not a regular directory: {path}")
    return _OwnedArtifactRoot(path=path, device=state.st_dev, inode=state.st_ino)


def _verify_owned_artifact_tree(owned: _OwnedArtifactDirectory) -> None:
    current = _owned_artifact_tree(owned.path)
    if current.entries != owned.entries:
        raise RuntimeError("artifact tree identity changed before cleanup")


def _owned_entry_path(owned: _OwnedArtifactDirectory, entry: _OwnedArtifactEntry) -> Path:
    return owned.path if entry.relative_path == "." else owned.path / entry.relative_path


def _verify_owned_entry(owned: _OwnedArtifactDirectory, entry: _OwnedArtifactEntry) -> None:
    path = _owned_entry_path(owned, entry)
    state = path.lstat()
    kind = "directory" if stat.S_ISDIR(state.st_mode) else "file" if stat.S_ISREG(state.st_mode) else None
    if (
        stat.S_ISLNK(state.st_mode)
        or kind != entry.kind
        or state.st_dev != entry.device
        or state.st_ino != entry.inode
    ):
        raise RuntimeError(f"artifact tree identity changed before cleanup: {path}")


def _cleanup_staging_directory(
    owned_staging_root: _OwnedArtifactRoot,
    owned_staging: _OwnedArtifactDirectory | None,
    *,
    original_error: Exception | None = None,
) -> bool:
    if owned_staging is not None:
        return _cleanup_owned_directory(owned_staging, original_error=original_error)
    try:
        root = _owned_artifact_root(owned_staging_root.path)
        if root.device != owned_staging_root.device or root.inode != owned_staging_root.inode:
            raise RuntimeError("staging root identity changed before cleanup")
        if next(owned_staging_root.path.iterdir(), None) is not None:
            raise RuntimeError("staging directory contains paths before complete ownership snapshot")
        os.rmdir(owned_staging_root.path)
        return True
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "Staging artifact cleanup was refused",
            artifact_dir=str(owned_staging_root.path),
            exc_info=exc,
        )
        if original_error is not None:
            _add_note(original_error, f"staging artifact cleanup was refused: {exc}")
        return False


def _cleanup_owned_directory(
    owned: _OwnedArtifactDirectory,
    *,
    original_error: Exception | None = None,
) -> bool:
    try:
        _verify_owned_artifact_tree(owned)
        directories = sorted(
            (entry for entry in owned.entries if entry.kind == "directory"),
            key=lambda entry: 0 if entry.relative_path == "." else len(entry.relative_path.split("/")),
            reverse=True,
        )
        for directory in directories:
            _verify_owned_entry(owned, directory)
            os.chmod(_owned_entry_path(owned, directory), 0o700)
        _verify_owned_artifact_tree(owned)
        files = sorted(
            (entry for entry in owned.entries if entry.kind == "file"),
            key=lambda entry: entry.relative_path.count("/"),
            reverse=True,
        )
        for file in files:
            _verify_owned_entry(owned, file)
            os.unlink(_owned_entry_path(owned, file))
        for directory in directories:
            _verify_owned_entry(owned, directory)
            os.rmdir(_owned_entry_path(owned, directory))
        return True
    except OSError as exc:
        logger.warning("Owned artifact cleanup failed", artifact_dir=str(owned.path), exc_info=exc)
        if original_error is not None:
            _add_note(original_error, f"owned artifact cleanup also failed: {exc}")
        return False
    except RuntimeError as exc:
        logger.warning("Owned artifact cleanup was refused", artifact_dir=str(owned.path), exc_info=exc)
        if original_error is not None:
            _add_note(original_error, f"owned artifact cleanup was refused: {exc}")
        return False


def _add_note(error: BaseException, note: str) -> None:
    try:
        add_note = getattr(error, "add_note", None)
    except BaseException:
        add_note = None
    if callable(add_note):
        try:
            add_note(note)
            return
        except BaseException:
            pass
    try:
        notes = getattr(error, "__notes__", None)
    except BaseException:
        return
    if isinstance(notes, list):
        try:
            notes.append(note)
        except BaseException:
            return
    else:
        try:
            setattr(error, "__notes__", [note])
        except BaseException:
            return


def _outcome(
    *,
    fingerprint: str,
    manifest: DatasetManifest,
    homerun_run_id: str,
    artifact_dir: Path,
    result: dict[str, Any],
) -> ExperimentOutcome:
    return ExperimentOutcome(
        run_id=homerun_run_id,
        fingerprint=fingerprint,
        status="SUCCEEDED",
        decision="NOT_EVALUATED",
        dataset_manifest_id=manifest.manifest_id,
        homerun_run_id=homerun_run_id,
        artifact_dir=artifact_dir,
        result=result,
    )

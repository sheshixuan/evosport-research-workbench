import hashlib

from evosport.data.manifest import canonical_json
from evosport.experiments.spec import ExperimentSpec


def compute_run_fingerprint(
    *,
    spec: ExperimentSpec,
    strategy_bytes: bytes,
    lock_bytes: bytes,
    manifest_bytes: bytes,
    evaluator_version: str,
    homerun_commit: str,
    environment_identity_bytes: bytes = b"",
) -> str:
    spec_payload = spec.model_dump(mode="json")
    spec_payload.pop("dataset_manifest_path")
    spec_payload["strategy"].pop("source_path")
    spec_payload["strategy"].pop("dependency_lock_path")
    payload = {
        "spec": spec_payload,
        "strategy_sha256": hashlib.sha256(strategy_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "environment_identity_sha256": hashlib.sha256(environment_identity_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "evaluator_version": evaluator_version,
        "homerun_commit": homerun_commit,
        "seed": spec.seed,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()

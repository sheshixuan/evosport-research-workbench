from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError
import yaml

from evosport.experiments.fingerprint import compute_run_fingerprint
from evosport.experiments.spec import (
    ExecutionModelSpec,
    ExperimentSpec,
    StrategyPackageSpec,
    TimeWindow,
    load_experiment_spec,
)


@pytest.fixture
def valid_spec_dict(tmp_path: Path) -> dict[str, object]:
    strategy = tmp_path / "strategy.py"
    lock = tmp_path / "requirements.lock"
    manifest = tmp_path / "manifest.json"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    lock.write_text("pydantic==2.7.0\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    return {
        "name": "football-over25-v001",
        "family_id": "football-over25",
        "dataset_manifest_path": str(manifest),
        "strategy": {
            "slug": "football_over25_v001",
            "source_path": str(strategy),
            "dependency_lock_path": str(lock),
            "config": {"minimum_edge": 0.03},
        },
        "window": {
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-05-01T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
        },
        "execution": {
            "initial_capital_usd": 1000.0,
            "submit_p50_ms": 50.0,
            "submit_p95_ms": 100.0,
            "cancel_p50_ms": 50.0,
            "cancel_p95_ms": 100.0,
        },
        "split_method": "time",
        "max_trials": 1,
        "seed": 7,
    }


def fingerprint_for(spec: ExperimentSpec, **changes: object) -> str:
    values: dict[str, object] = {
        "strategy_bytes": b"strategy",
        "lock_bytes": b"lock",
        "manifest_bytes": b"manifest",
        "evaluator_version": "none",
        "homerun_commit": "c8e647f",
    }
    values.update(changes)
    return compute_run_fingerprint(spec=spec, **values)  # type: ignore[arg-type]


def test_spec_rejects_non_time_split(valid_spec_dict: dict[str, object]) -> None:
    valid_spec_dict["split_method"] = "random"

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(valid_spec_dict)


def test_yaml_round_trip_and_stable_fingerprint(
    tmp_path: Path, valid_spec_dict: dict[str, object]
) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(__import__("yaml").safe_dump(valid_spec_dict), encoding="utf-8")

    spec = load_experiment_spec(path)

    assert fingerprint_for(spec) == fingerprint_for(spec)
    assert len(fingerprint_for(spec)) == 64


def test_time_window_rejects_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        TimeWindow.model_validate(
            {
                "train_start": "2026-01-01T00:00:00",
                "train_end": "2026-05-01T00:00:00Z",
                "validation_start": "2026-05-01T00:00:00Z",
                "validation_end": "2026-08-01T00:00:00Z",
            }
        )


def test_time_window_normalizes_aware_datetimes_to_utc() -> None:
    window = TimeWindow.model_validate(
        {
            "train_start": "2025-12-31T19:00:00-05:00",
            "train_end": "2026-04-30T20:00:00-04:00",
            "validation_start": "2026-04-30T20:00:00-04:00",
            "validation_end": "2026-07-31T20:00:00-04:00",
        }
    )

    assert window.train_start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert window.train_start.tzinfo is timezone.utc
    assert window.train_end.tzinfo is timezone.utc
    assert window.validation_start.tzinfo is timezone.utc
    assert window.validation_end.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "window",
    [
        {
            "train_start": "2026-06-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-05-01T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
        },
        {
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-04-30T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
        },
        {
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-09-01T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
        },
    ],
)
def test_time_window_rejects_each_invalid_ordering_relation(window: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TimeWindow.model_validate(window)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (TimeWindow, {
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-05-01T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
            "unexpected": True,
        }),
        (StrategyPackageSpec, {
            "slug": "strategy",
            "source_path": "strategy.py",
            "dependency_lock_path": "requirements.lock",
            "unexpected": True,
        }),
        (ExecutionModelSpec, {
            "initial_capital_usd": 1.0,
            "submit_p50_ms": 1.0,
            "submit_p95_ms": 1.0,
            "cancel_p50_ms": 1.0,
            "cancel_p95_ms": 1.0,
            "unexpected": True,
        }),
    ],
)
def test_component_models_reject_unknown_fields(
    model: type[TimeWindow] | type[StrategyPackageSpec] | type[ExecutionModelSpec],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_experiment_spec_rejects_unknown_fields(valid_spec_dict: dict[str, object]) -> None:
    valid_spec_dict["unexpected"] = True

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(valid_spec_dict)


@pytest.mark.parametrize(
    "field",
    [
        "initial_capital_usd",
        "submit_p50_ms",
        "submit_p95_ms",
        "cancel_p50_ms",
        "cancel_p95_ms",
    ],
)
@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_execution_model_rejects_non_finite_values(field: str, non_finite: float) -> None:
    payload = {
        "initial_capital_usd": 1.0,
        "submit_p50_ms": 1.0,
        "submit_p95_ms": 1.0,
        "cancel_p50_ms": 1.0,
        "cancel_p95_ms": 1.0,
    }
    payload[field] = non_finite

    with pytest.raises(ValidationError):
        ExecutionModelSpec.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_capital_usd", 0.0),
        ("submit_p50_ms", -1.0),
        ("submit_p95_ms", -1.0),
        ("cancel_p50_ms", -1.0),
        ("cancel_p95_ms", -1.0),
    ],
)
def test_execution_model_rejects_values_outside_field_ranges(field: str, value: float) -> None:
    payload = {
        "initial_capital_usd": 1.0,
        "submit_p50_ms": 1.0,
        "submit_p95_ms": 1.0,
        "cancel_p50_ms": 1.0,
        "cancel_p95_ms": 1.0,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ExecutionModelSpec.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "initial_capital_usd": 1.0,
            "submit_p50_ms": 2.0,
            "submit_p95_ms": 1.0,
            "cancel_p50_ms": 1.0,
            "cancel_p95_ms": 1.0,
        },
        {
            "initial_capital_usd": 1.0,
            "submit_p50_ms": 1.0,
            "submit_p95_ms": 1.0,
            "cancel_p50_ms": 2.0,
            "cancel_p95_ms": 1.0,
        },
    ],
)
def test_execution_model_rejects_inverted_percentiles(payload: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        ExecutionModelSpec.model_validate(payload)


@pytest.mark.parametrize(
    "config",
    [
        {"labels": {"alpha", "bravo"}},
        {"values": (1, 2)},
        {1: "non-string key"},
        {"nested": [{"value": float("nan")}]},
        {"nested": [{"value": float("inf")}]},
        {"nested": [{"value": float("-inf")}]},
        {"value": object()},
    ],
)
def test_strategy_config_rejects_non_json_values(config: dict[object, object]) -> None:
    with pytest.raises(ValidationError):
        StrategyPackageSpec.model_validate(
            {
                "slug": "strategy",
                "source_path": "strategy.py",
                "dependency_lock_path": "requirements.lock",
                "config": config,
            }
        )


def test_yaml_config_rejects_set(tmp_path: Path, valid_spec_dict: dict[str, object]) -> None:
    strategy = valid_spec_dict["strategy"]
    assert isinstance(strategy, dict)
    strategy["config"] = {"labels": {"alpha", "bravo"}}
    path = tmp_path / "set-config.yaml"
    path.write_text(yaml.safe_dump(valid_spec_dict), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_experiment_spec(path)


def test_strategy_config_is_recursively_immutable_and_serializes_as_json(
    valid_spec_dict: dict[str, object]
) -> None:
    strategy = valid_spec_dict["strategy"]
    assert isinstance(strategy, dict)
    strategy["config"] = {
        "minimum_edge": 0.03,
        "filters": {"league": "EPL"},
        "lookbacks": [3, 5, 8],
    }
    spec = ExperimentSpec.model_validate(valid_spec_dict)
    config = spec.strategy.config
    fingerprint = fingerprint_for(spec)

    assert isinstance(config, dict)
    assert spec.model_dump(mode="json")["strategy"]["config"] == {
        "minimum_edge": 0.03,
        "filters": {"league": "EPL"},
        "lookbacks": [3, 5, 8],
    }
    with pytest.raises(TypeError):
        config["minimum_edge"] = 0.04
    with pytest.raises(TypeError):
        config["filters"]["league"] = "La Liga"  # type: ignore[index]
    with pytest.raises(TypeError):
        config["lookbacks"][0] = 13  # type: ignore[index]

    assert fingerprint_for(spec) == fingerprint
    assert spec.strategy.config is config


def test_valid_nested_config_has_stable_fingerprint_across_hash_seeds() -> None:
    payload = {
        "name": "football-over25-v001",
        "family_id": "football-over25",
        "dataset_manifest_path": "manifest.json",
        "strategy": {
            "slug": "football_over25_v001",
            "source_path": "strategy.py",
            "dependency_lock_path": "requirements.lock",
            "config": {
                "filters": {"leagues": ["EPL", "La Liga"], "minimum_edge": 0.03},
                "lookbacks": [3, 5, {"features": ["form", "xg"]}],
            },
        },
        "window": {
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-05-01T00:00:00Z",
            "validation_start": "2026-05-01T00:00:00Z",
            "validation_end": "2026-08-01T00:00:00Z",
        },
        "execution": {
            "initial_capital_usd": 1000.0,
            "submit_p50_ms": 50.0,
            "submit_p95_ms": 100.0,
            "cancel_p50_ms": 50.0,
            "cancel_p95_ms": 100.0,
        },
        "split_method": "time",
        "max_trials": 1,
        "seed": 7,
    }
    script = "\n".join(
        [
            "import json",
            "from evosport.experiments.fingerprint import compute_run_fingerprint",
            "from evosport.experiments.spec import ExperimentSpec",
            f"spec = ExperimentSpec.model_validate(json.loads({json.dumps(json.dumps(payload))}))",
            "print(compute_run_fingerprint(spec=spec, strategy_bytes=b'strategy', lock_bytes=b'lock', "
            "manifest_bytes=b'manifest', evaluator_version='none', homerun_commit='c8e647f'))",
        ]
    )
    fingerprints = []
    for seed in ("1", "2", "3"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        )
        fingerprints.append(completed.stdout.strip())

    assert len(set(fingerprints)) == 1


def test_fingerprint_ignores_config_mapping_insertion_order(
    valid_spec_dict: dict[str, object]
) -> None:
    first_payload = deepcopy(valid_spec_dict)
    second_payload = deepcopy(valid_spec_dict)
    first_payload["strategy"] = {
        **first_payload["strategy"],  # type: ignore[dict-item]
        "config": {"minimum_edge": 0.03, "league": "EPL"},
    }
    second_payload["strategy"] = {
        **second_payload["strategy"],  # type: ignore[dict-item]
        "config": {"league": "EPL", "minimum_edge": 0.03},
    }

    assert fingerprint_for(ExperimentSpec.model_validate(first_payload)) == fingerprint_for(
        ExperimentSpec.model_validate(second_payload)
    )


def test_fingerprint_ignores_equivalent_timezone_offsets(
    valid_spec_dict: dict[str, object]
) -> None:
    offset_payload = deepcopy(valid_spec_dict)
    offset_payload["window"] = {
        "train_start": "2025-12-31T19:00:00-05:00",
        "train_end": "2026-04-30T20:00:00-04:00",
        "validation_start": "2026-04-30T20:00:00-04:00",
        "validation_end": "2026-07-31T20:00:00-04:00",
    }

    assert fingerprint_for(ExperimentSpec.model_validate(valid_spec_dict)) == fingerprint_for(
        ExperimentSpec.model_validate(offset_payload)
    )


def test_fingerprint_ignores_relocated_path_fields(
    valid_spec_dict: dict[str, object]
) -> None:
    relocated_payload = deepcopy(valid_spec_dict)
    relocated_payload["dataset_manifest_path"] = "/new/location/manifest.json"
    relocated_payload["strategy"] = {
        **relocated_payload["strategy"],  # type: ignore[dict-item]
        "source_path": "/new/location/strategy.py",
        "dependency_lock_path": "/new/location/requirements.lock",
    }

    assert fingerprint_for(ExperimentSpec.model_validate(valid_spec_dict)) == fingerprint_for(
        ExperimentSpec.model_validate(relocated_payload)
    )


@pytest.mark.parametrize(
    ("change", "replacement"),
    [
        ("strategy bytes", {"strategy_bytes": b"changed strategy"}),
        ("dependency-lock bytes", {"lock_bytes": b"changed lock"}),
        ("manifest bytes", {"manifest_bytes": b"changed manifest"}),
        ("active environment identity", {"environment_identity_bytes": b"changed environment"}),
        ("evaluator version", {"evaluator_version": "v2"}),
        ("Homerun commit", {"homerun_commit": "different-commit"}),
    ],
)
def test_fingerprint_changes_when_external_determinant_changes(
    valid_spec_dict: dict[str, object], change: str, replacement: dict[str, object]
) -> None:
    baseline = fingerprint_for(ExperimentSpec.model_validate(valid_spec_dict))

    assert fingerprint_for(ExperimentSpec.model_validate(valid_spec_dict), **replacement) != baseline, change


def test_fingerprint_changes_when_seed_changes(valid_spec_dict: dict[str, object]) -> None:
    altered_payload = deepcopy(valid_spec_dict)
    altered_payload["seed"] = 8

    assert fingerprint_for(ExperimentSpec.model_validate(altered_payload)) != fingerprint_for(
        ExperimentSpec.model_validate(valid_spec_dict)
    )


def test_fingerprint_changes_when_strategy_config_changes(valid_spec_dict: dict[str, object]) -> None:
    altered_payload = deepcopy(valid_spec_dict)
    altered_payload["strategy"] = {
        **altered_payload["strategy"],  # type: ignore[dict-item]
        "config": {"minimum_edge": 0.04},
    }

    assert fingerprint_for(ExperimentSpec.model_validate(altered_payload)) != fingerprint_for(
        ExperimentSpec.model_validate(valid_spec_dict)
    )


def replace_spec_field(payload: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = payload
    for part in path[:-1]:
        child = current[part]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (("name",), "football-over25-v002"),
        (("family_id",), "football-over25-alt"),
        (("strategy", "slug"), "football_over25_v002"),
        (("strategy", "config"), {"minimum_edge": 0.04}),
        (("window", "train_start"), "2025-12-31T00:00:00Z"),
        (("window", "train_end"), "2026-04-30T00:00:00Z"),
        (("window", "validation_start"), "2026-05-02T00:00:00Z"),
        (("window", "validation_end"), "2026-08-02T00:00:00Z"),
        (("execution", "initial_capital_usd"), 1001.0),
        (("execution", "submit_p50_ms"), 51.0),
        (("execution", "submit_p95_ms"), 101.0),
        (("execution", "cancel_p50_ms"), 51.0),
        (("execution", "cancel_p95_ms"), 101.0),
        (("max_trials",), 2),
        (("seed",), 8),
        (("hidden_oos_manifest_id",), "hidden-oos-v001"),
    ],
)
def test_fingerprint_changes_when_each_included_spec_field_changes(
    valid_spec_dict: dict[str, object], field: tuple[str, ...], replacement: object
) -> None:
    changed_payload = deepcopy(valid_spec_dict)
    replace_spec_field(changed_payload, field, replacement)

    assert fingerprint_for(ExperimentSpec.model_validate(changed_payload)) != fingerprint_for(
        ExperimentSpec.model_validate(valid_spec_dict)
    ), ".".join(field)


def test_fingerprint_includes_the_only_valid_split_method_literal(
    valid_spec_dict: dict[str, object]
) -> None:
    """model_construct bypasses Literal validation solely to guard fingerprint field inclusion."""
    spec = ExperimentSpec.model_validate(valid_spec_dict)
    probe = ExperimentSpec.model_construct(
        name=spec.name,
        family_id=spec.family_id,
        dataset_manifest_path=spec.dataset_manifest_path,
        strategy=spec.strategy,
        window=spec.window,
        execution=spec.execution,
        split_method="synthetic-time-split",
        max_trials=spec.max_trials,
        seed=spec.seed,
        hidden_oos_manifest_id=spec.hidden_oos_manifest_id,
    )

    assert fingerprint_for(probe) != fingerprint_for(spec)


@pytest.mark.parametrize("field", ["seed", "max_trials"])
def test_experiment_yaml_rejects_boolean_integer_fields(
    tmp_path: Path,
    valid_spec_dict: dict[str, object],
    field: str,
) -> None:
    payload = deepcopy(valid_spec_dict)
    payload[field] = True
    path = tmp_path / "boolean-integer.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_experiment_spec(path)

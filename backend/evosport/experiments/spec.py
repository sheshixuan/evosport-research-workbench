from datetime import timezone
import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, field_validator, model_validator


class FrozenConfig(dict[str, object]):
    def _immutable(self, *args: object, **kwargs: object) -> None:
        raise TypeError("strategy config is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_json_value(value: object, *, allow_tuples: bool = False) -> object:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("strategy config numbers must be finite")
        return value
    if isinstance(value, list):
        return tuple(_freeze_json_value(item, allow_tuples=allow_tuples) for item in value)
    if isinstance(value, tuple):
        if not allow_tuples:
            raise ValueError("strategy config must contain only JSON values")
        return tuple(_freeze_json_value(item, allow_tuples=True) for item in value)
    if isinstance(value, dict):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("strategy config mapping keys must be strings")
            frozen[key] = _freeze_json_value(item, allow_tuples=allow_tuples)
        return FrozenConfig(frozen)
    raise ValueError("strategy config must contain only JSON values")


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train_start: AwareDatetime
    train_end: AwareDatetime
    validation_start: AwareDatetime
    validation_end: AwareDatetime

    @model_validator(mode="after")
    def normalize_and_validate_order(self) -> "TimeWindow":
        object.__setattr__(self, "train_start", self.train_start.astimezone(timezone.utc))
        object.__setattr__(self, "train_end", self.train_end.astimezone(timezone.utc))
        object.__setattr__(self, "validation_start", self.validation_start.astimezone(timezone.utc))
        object.__setattr__(self, "validation_end", self.validation_end.astimezone(timezone.utc))
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end:
            raise ValueError("time windows must be ordered and non-overlapping")
        return self


class StrategyPackageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    source_path: Path
    dependency_lock_path: Path
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("config", mode="before")
    @classmethod
    def validate_json_config(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("strategy config must be a mapping")
        return _freeze_json_value(value)

    @model_validator(mode="after")
    def freeze_config(self) -> "StrategyPackageSpec":
        object.__setattr__(self, "config", _freeze_json_value(self.config, allow_tuples=True))
        return self


class ExecutionModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_capital_usd: FiniteFloat = Field(gt=0)
    submit_p50_ms: FiniteFloat = Field(ge=0)
    submit_p95_ms: FiniteFloat = Field(ge=0)
    cancel_p50_ms: FiniteFloat = Field(ge=0)
    cancel_p95_ms: FiniteFloat = Field(ge=0)

    @model_validator(mode="after")
    def validate_percentile_order(self) -> "ExecutionModelSpec":
        if self.submit_p95_ms < self.submit_p50_ms:
            raise ValueError("submit_p95_ms cannot be below submit_p50_ms")
        if self.cancel_p95_ms < self.cancel_p50_ms:
            raise ValueError("cancel_p95_ms cannot be below cancel_p50_ms")
        return self


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    family_id: str
    dataset_manifest_path: Path
    strategy: StrategyPackageSpec
    window: TimeWindow
    execution: ExecutionModelSpec
    split_method: Literal["time"] = "time"
    max_trials: StrictInt = Field(ge=1)
    seed: StrictInt
    hidden_oos_manifest_id: str | None = None


def load_experiment_spec(path: Path) -> ExperimentSpec:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("experiment YAML must contain a mapping")
    return ExperimentSpec.model_validate(payload)

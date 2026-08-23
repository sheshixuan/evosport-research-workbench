import hashlib
import json
from datetime import timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from evosport.semantics.football_binding import FootballDatasetBinding


CATALOG_SCHEMA_VERSION = "evosport.dataset.v2"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class DatasetFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    provider_dataset_ids: tuple[str, ...]
    token_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> "DatasetFile":
        if not Path(self.path).is_absolute():
            raise ValueError("path must be absolute")
        if not self.provider_dataset_ids:
            raise ValueError("provider_dataset_ids cannot be empty")
        if self.provider_dataset_ids != tuple(sorted(set(self.provider_dataset_ids))):
            raise ValueError("provider_dataset_ids must be unique and sorted")
        if not self.token_ids:
            raise ValueError("token_ids cannot be empty")
        if self.token_ids != tuple(sorted(set(self.token_ids))):
            raise ValueError("token_ids must be unique and sorted")
        return self


def _manifest_identity(
    *,
    schema_version: str,
    source: str,
    token_ids: tuple[str, ...],
    start: AwareDatetime,
    end: AwareDatetime,
    files: tuple[DatasetFile, ...],
    provider_dataset_ids: tuple[str, ...],
    football: FootballDatasetBinding,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "source": source,
        "token_ids": list(token_ids),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "files": [item.model_dump(mode="json") for item in files],
        "provider_dataset_ids": list(provider_dataset_ids),
        "football": football.model_dump(mode="json"),
    }


def manifest_id_for(
    *,
    schema_version: str,
    source: str,
    token_ids: tuple[str, ...],
    start: AwareDatetime,
    end: AwareDatetime,
    files: tuple[DatasetFile, ...],
    provider_dataset_ids: tuple[str, ...],
    football: FootballDatasetBinding,
) -> str:
    return hashlib.sha256(
        canonical_json(
            _manifest_identity(
                schema_version=schema_version,
                source=source,
                token_ids=token_ids,
                start=start,
                end=end,
                files=files,
                provider_dataset_ids=provider_dataset_ids,
                football=football,
            )
        )
    ).hexdigest()


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[CATALOG_SCHEMA_VERSION] = CATALOG_SCHEMA_VERSION
    manifest_id: str = Field(pattern=SHA256_PATTERN)
    source: Literal["homerun_catalog"] = "homerun_catalog"
    token_ids: tuple[str, ...]
    start: AwareDatetime
    end: AwareDatetime
    files: tuple[DatasetFile, ...]
    provider_dataset_ids: tuple[str, ...]
    football: FootballDatasetBinding

    @model_validator(mode="after")
    def validate_manifest(self) -> "DatasetManifest":
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.token_ids != tuple(sorted(set(self.token_ids))) or not self.token_ids:
            raise ValueError("token_ids must be non-empty, unique, and sorted")
        if (
            self.provider_dataset_ids != tuple(sorted(set(self.provider_dataset_ids)))
            or not self.provider_dataset_ids
        ):
            raise ValueError("provider_dataset_ids must be non-empty, unique, and sorted")
        paths = tuple(item.path for item in self.files)
        if not paths or len(set(paths)) != len(paths) or paths != tuple(sorted(paths)):
            raise ValueError("files must be non-empty with unique sorted paths")
        if {token for item in self.files for token in item.token_ids} != set(self.token_ids):
            raise ValueError("file token IDs must exactly cover manifest token IDs")
        if {dataset_id for item in self.files for dataset_id in item.provider_dataset_ids} != set(
            self.provider_dataset_ids
        ):
            raise ValueError("file dataset IDs must exactly cover manifest provider dataset IDs")
        expected_manifest_id = manifest_id_for(
            schema_version=self.schema_version,
            source=self.source,
            token_ids=self.token_ids,
            start=self.start,
            end=self.end,
            files=self.files,
            provider_dataset_ids=self.provider_dataset_ids,
            football=self.football,
        )
        if self.manifest_id != expected_manifest_id:
            raise ValueError("manifest_id does not match canonical manifest content")
        return self


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

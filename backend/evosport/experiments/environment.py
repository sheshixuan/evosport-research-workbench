from __future__ import annotations

import hashlib
import importlib.metadata
import platform
from dataclasses import dataclass
from typing import Iterable

from packaging.utils import canonicalize_name

from evosport.data.manifest import canonical_json


@dataclass(frozen=True)
class EnvironmentIdentity:
    python_implementation: str
    python_version: str
    distributions: tuple[str, ...]
    identity_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "distributions": list(self.distributions),
            "identity_sha256": self.identity_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


def _canonical_pins(lines: Iterable[str], *, source: str) -> tuple[str, ...]:
    pins: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.count("==") != 1:
            raise ValueError(f"environment lock {source} contains a non-pinned entry: {line!r}")
        raw_name, version = line.split("==", 1)
        name = canonicalize_name(raw_name.strip())
        version = version.strip()
        if not name or not version or any(character.isspace() for character in version):
            raise ValueError(f"environment lock {source} contains an invalid entry: {line!r}")
        if name in pins:
            raise ValueError(f"environment lock {source} contains duplicate package {name!r}")
        pins[name] = version
    if not pins:
        raise ValueError(f"environment lock {source} is empty")
    return tuple(f"{name}=={pins[name]}" for name in sorted(pins))


def active_distribution_pins() -> tuple[str, ...]:
    pins: list[str] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            pins.append(f"{name}=={distribution.version}")
    return _canonical_pins(pins, source="active environment")


def verify_environment_lock(
    lock_bytes: bytes,
    *,
    installed: Iterable[str] | None = None,
    python_implementation: str | None = None,
    python_version: str | None = None,
) -> EnvironmentIdentity:
    try:
        lock_text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("environment lock must be UTF-8") from exc
    locked = _canonical_pins(lock_text.splitlines(), source="file")
    active = (
        active_distribution_pins()
        if installed is None
        else _canonical_pins(installed, source="active environment")
    )
    if locked != active:
        locked_set = set(locked)
        active_set = set(active)
        raise ValueError(
            "environment lock does not exactly match active environment: "
            f"missing={sorted(active_set - locked_set)} extra={sorted(locked_set - active_set)}"
        )
    implementation = python_implementation or platform.python_implementation()
    version = python_version or platform.python_version()
    payload = {
        "python_implementation": implementation,
        "python_version": version,
        "distributions": list(active),
    }
    return EnvironmentIdentity(
        python_implementation=implementation,
        python_version=version,
        distributions=active,
        identity_sha256=hashlib.sha256(canonical_json(payload)).hexdigest(),
    )

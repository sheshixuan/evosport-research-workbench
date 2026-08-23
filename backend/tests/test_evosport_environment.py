from __future__ import annotations

from pathlib import Path

import pytest

from evosport.experiments.environment import verify_environment_lock


def test_environment_lock_canonicalizes_names_and_binds_python_identity() -> None:
    identity = verify_environment_lock(
        b"Other_Package==2.0\nExample.Pkg==1.0\n",
        installed=("example-pkg==1.0", "other-package==2.0"),
        python_implementation="CPython",
        python_version="3.12.7",
    )

    assert identity.distributions == ("example-pkg==1.0", "other-package==2.0")
    assert identity.python_implementation == "CPython"
    assert identity.python_version == "3.12.7"
    assert len(identity.identity_sha256) == 64


def test_stale_or_fabricated_environment_lock_fails_closed() -> None:
    with pytest.raises(ValueError, match="does not exactly match active environment"):
        verify_environment_lock(
            b"example-pkg==9.9\n",
            installed=("example-pkg==1.0",),
            python_implementation="CPython",
            python_version="3.12.7",
        )


@pytest.mark.parametrize(
    "lock_bytes",
    [
        b"example-pkg>=1.0\n",
        b"example-pkg==1.0\nexample_pkg==1.0\n",
        b"--index-url https://example.invalid\nexample-pkg==1.0\n",
    ],
)
def test_environment_lock_rejects_noncanonical_or_duplicate_entries(lock_bytes: bytes) -> None:
    with pytest.raises(ValueError, match="environment lock"):
        verify_environment_lock(
            lock_bytes,
            installed=("example-pkg==1.0",),
            python_implementation="CPython",
            python_version="3.12.7",
        )


def test_xgboost_requirements_select_installable_distribution_by_platform() -> None:
    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")

    assert 'xgboost-cpu>=3.2.0; platform_system != "Darwin"' in requirements
    assert 'xgboost>=3.2.0; platform_system == "Darwin"' in requirements
    assert "\nxgboost-cpu>=3.2.0\n" not in requirements

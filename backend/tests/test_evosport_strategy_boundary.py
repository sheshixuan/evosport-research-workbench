from __future__ import annotations

from pathlib import Path

import pytest

from services.strategy_loader import (
    StrategyLoader,
    StrategyValidationError,
    validate_strategy_source,
)


def _strategy_source(preamble: str) -> str:
    return f'''{preamble}
from services.strategies.base import BaseStrategy

class ReproducibleBoundaryStrategy(BaseStrategy):
    name = "Reproducible boundary"
    description = "Exercises the deterministic source contract."

    def detect(self, events, markets, prices):
        return []
'''


_AMBIENT_SOURCES = [
    pytest.param("from config import settings as app_settings", True, id="application-config-alias"),
    pytest.param("import services.ai as ai_service", True, id="application-service-alias"),
    pytest.param("import httpx as network_client", True, id="network-client-alias"),
    pytest.param("from pathlib import Path as FilePath", True, id="filesystem-alias"),
    pytest.param("import os as process_environment", False, id="process-environment-alias"),
    pytest.param("import subprocess as child_process", False, id="subprocess-alias"),
    pytest.param("import threading as worker_threads", True, id="thread-alias"),
    pytest.param("import asyncio as event_loop", True, id="task-event-loop-alias"),
    pytest.param("from time import monotonic as process_clock", True, id="monotonic-clock-alias"),
    pytest.param(
        "from datetime import datetime as WallClock\nMODULE_TIME = WallClock.now()",
        True,
        id="datetime-now-alias",
    ),
    pytest.param("from uuid import uuid4 as new_identifier\nMODULE_ID = new_identifier()", True, id="uuid-alias"),
    pytest.param("import random as rng", True, id="python-random-alias"),
    pytest.param("from random import random as draw\nDRAW = draw()", True, id="python-random-call-alias"),
    pytest.param("import numpy as np", True, id="numpy-alias"),
    pytest.param("import scipy.stats as stats", True, id="scipy-rng-surface"),
    pytest.param("VALUE = open('/tmp/ambient').read()", False, id="builtin-open"),
    pytest.param("VALUE = eval(\"'ambient'\")", False, id="dynamic-eval"),
    pytest.param("FILE_READER = open", True, id="aliased-builtin-open"),
    pytest.param("DYNAMIC_EVAL = eval", True, id="aliased-dynamic-eval"),
    pytest.param("DYNAMIC_EXEC = exec", True, id="aliased-dynamic-exec"),
    pytest.param("IMPORTER = __import__", True, id="aliased-dynamic-import"),
    pytest.param(
        "BUILTIN_READER = getattr(__builtins__, 'open')",
        True,
        id="dynamic-builtins-getattr",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\nCLOCK = WallClock.now",
        True,
        id="aliased-wall-clock-api",
    ),
    pytest.param("import math_random as lookalike", True, id="exact-module-boundary"),
]


_REPRODUCIBLE_BYPASS_SOURCES = [
    pytest.param(
        'from datetime import datetime as WallClock\nCLOCK = getattr(WallClock, "now")',
        id="literal-getattr-wall-clock",
    ),
    pytest.param('VALUE = hash("evosport")', id="process-varying-hash"),
    pytest.param(
        'HASHER = hash\nVALUE = HASHER("evosport")',
        id="aliased-process-varying-hash",
    ),
    pytest.param("VALUE = id(object())", id="process-varying-id"),
    pytest.param(
        "IDENTITY = id\nVALUE = IDENTITY(object())",
        id="aliased-process-varying-id",
    ),
    pytest.param(
        'from datetime import date as CalendarDate\nTODAY = getattr(CalendarDate, "today")',
        id="literal-getattr-calendar-date",
    ),
    pytest.param('VALUE = next(iter({"yes", "no"}))', id="hash-seed-set-iteration"),
    pytest.param(
        "from services.strategies.base import BaseStrategy, settings, utcnow",
        id="ambient-application-reexports",
    ),
]


_REPRODUCIBLE_UNORDERED_CONSTRUCTS = [
    pytest.param('VALUE = {"yes", "no"}', id="set-literal"),
    pytest.param('VALUE = {value for value in ("yes", "no")}', id="set-comprehension"),
    pytest.param('VALUE = set(("yes", "no"))', id="set-constructor"),
    pytest.param('VALUE = frozenset(("yes", "no"))', id="frozenset-constructor"),
    pytest.param("SET_FACTORY = set", id="aliased-set-constructor"),
    pytest.param("FROZEN_FACTORY = frozenset", id="aliased-frozenset-constructor"),
]


_REPRODUCIBLE_ASSIGNMENT_ALIAS_BYPASSES = [
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = WallClock\n"
        'CLOCK = getattr(ClockType, "now")',
        id="assigned-ambient-type",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "READ_MEMBER = getattr\n"
        'CLOCK = READ_MEMBER(WallClock, "now")',
        id="assigned-reflective-builtin",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = WallClock\n"
        "TransitiveClockType = ClockType\n"
        'CLOCK = getattr(TransitiveClockType, "now")',
        id="transitive-assigned-ambient-type",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "READ_MEMBER = getattr\n"
        "TRANSITIVE_READ_MEMBER = READ_MEMBER\n"
        'CLOCK = TRANSITIVE_READ_MEMBER(WallClock, "now")',
        id="transitive-assigned-reflective-builtin",
    ),
]


_REPRODUCIBLE_REBOUND_ALIAS_BYPASSES = [
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = object\n"
        "ClockType = WallClock\n"
        'CLOCK = getattr(ClockType, "now")',
        id="rebound-ambient-type",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "READ_MEMBER = len\n"
        "READ_MEMBER = getattr\n"
        'CLOCK = READ_MEMBER(WallClock, "now")',
        id="rebound-reflective-builtin",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType: type = object\n"
        "ClockType = WallClock\n"
        'CLOCK = getattr(ClockType, "now")',
        id="annotated-rebound",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = Alias = object\n"
        "ClockType = Alias = WallClock\n"
        'CLOCK = getattr(Alias, "now")',
        id="chained-rebound",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = object\n"
        "if True:\n"
        "    ClockType = WallClock\n"
        'CLOCK = getattr(ClockType, "now")',
        id="constant-branch-rebound",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType, OTHER = (WallClock, object)\n"
        'CLOCK = getattr(ClockType, "now")',
        id="tuple-unpacking",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        'CLOCK = getattr((ClockType := WallClock), "now")',
        id="named-expression",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "READ_MEMBER = getattr if True else len\n"
        'CLOCK = READ_MEMBER(WallClock, "now")',
        id="constant-conditional",
    ),
]


_REPRODUCIBLE_DATETIME_CLASS_FLOWS = [
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = (WallClock,)[0]\n"
        'CLOCK = getattr(ClockType, "now")',
        id="tuple-subscript",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        'ClockType = {"clock": WallClock}["clock"]\n'
        'CLOCK = getattr(ClockType, "now")',
        id="dict-subscript",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = [WallClock][0]\n"
        'CLOCK = getattr(ClockType, "now")',
        id="list-subscript",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = (lambda value: value)(WallClock)\n"
        'CLOCK = getattr(ClockType, "now")',
        id="identity-lambda",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "ClockType = WallClock or object\n"
        'CLOCK = getattr(ClockType, "now")',
        id="boolean-target",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "def choose(value=WallClock):\n"
        '    return getattr(value, "now")\n'
        "CLOCK = choose()",
        id="default-argument",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "def choose():\n"
        "    value = WallClock\n"
        "    def inner():\n"
        '        return getattr(value, "now")\n'
        "    return inner()\n"
        "CLOCK = choose()",
        id="closure",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        "def choose():\n"
        "    return WallClock\n"
        'CLOCK = getattr(choose(), "now")',
        id="helper-return",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        'CLOCK = (getattr,)[0](WallClock, "now")',
        id="tuple-reflective-callee",
    ),
    pytest.param(
        "from datetime import datetime as WallClock\n"
        'CLOCK = (getattr or len)(WallClock, "now")',
        id="boolean-reflective-callee",
    ),
]


_REPRODUCIBLE_FORBIDDEN_DATETIME_IMPORTS = [
    pytest.param("import datetime", id="datetime-module"),
    pytest.param("import datetime as clocks", id="datetime-module-alias"),
    pytest.param("from datetime import datetime", id="datetime-class"),
    pytest.param("from datetime import datetime as WallClock", id="datetime-class-alias"),
    pytest.param("from datetime import date", id="date-class"),
    pytest.param("from datetime import date as CalendarDate", id="date-class-alias"),
]


@pytest.mark.parametrize(("preamble", "ordinary_valid"), _AMBIENT_SOURCES)
def test_reproducible_validator_rejects_ambient_and_nondeterministic_sources(
    preamble: str,
    ordinary_valid: bool,
) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is ordinary_valid
    assert reproducible["valid"] is False
    assert reproducible["errors"]


def test_documented_deterministic_strategy_validates_and_loads_reproducibly() -> None:
    source = (Path(__file__).parent / "fixtures/evosport/strategy.py").read_text(encoding="utf-8")

    validation = validate_strategy_source(source, reproducible=True)
    loader = StrategyLoader()
    loaded = loader.load("documented_reproducible", source, {}, reproducible=True)

    assert validation["valid"] is True
    assert loaded.instance.detect([], [], {}) == []
    loader.unload("documented_reproducible")


def test_reproducibility_boundary_allows_deterministic_object_attribute_access() -> None:
    source = _strategy_source(
        "def market_identity(market):\n    return getattr(market, 'id', None)"
    )

    validation = validate_strategy_source(source, reproducible=True)

    assert validation["valid"] is True, validation["errors"]


def test_reproducibility_boundary_allows_assigned_deterministic_getattr() -> None:
    source = _strategy_source(
        "READ_MEMBER = getattr\n"
        "def market_identity(market):\n"
        "    return READ_MEMBER(market, 'id', None)"
    )

    validation = validate_strategy_source(source, reproducible=True)

    assert validation["valid"] is True, validation["errors"]


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_ASSIGNMENT_ALIAS_BYPASSES)
def test_reproducible_validator_rejects_assignment_alias_bypasses(
    preamble: str,
) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is True, ordinary["errors"]
    assert reproducible["valid"] is False
    assert reproducible["errors"]


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_ASSIGNMENT_ALIAS_BYPASSES)
def test_reproducible_loader_rejects_assignment_aliases_before_execution(
    preamble: str,
) -> None:
    source = _strategy_source(f"{preamble}\nraise RuntimeError('strategy module executed')")

    with pytest.raises(StrategyValidationError, match="validation failed") as exc_info:
        StrategyLoader().load("assignment_alias_bypass", source, {}, reproducible=True)

    assert "strategy module executed" not in str(exc_info.value)


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_REBOUND_ALIAS_BYPASSES)
def test_reproducible_validator_rejects_rebound_alias_bypasses(
    preamble: str,
) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is True, ordinary["errors"]
    assert reproducible["valid"] is False
    assert reproducible["errors"]


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_REBOUND_ALIAS_BYPASSES)
def test_reproducible_loader_rejects_rebound_aliases_before_execution(
    preamble: str,
) -> None:
    source = _strategy_source(f"{preamble}\nraise RuntimeError('strategy module executed')")

    with pytest.raises(StrategyValidationError, match="validation failed") as exc_info:
        StrategyLoader().load("rebound_alias_bypass", source, {}, reproducible=True)

    assert "strategy module executed" not in str(exc_info.value)


@pytest.mark.parametrize(
    "preamble",
    _REPRODUCIBLE_DATETIME_CLASS_FLOWS + _REPRODUCIBLE_FORBIDDEN_DATETIME_IMPORTS,
)
def test_reproducible_validator_rejects_datetime_class_ingress(
    preamble: str,
) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is True, ordinary["errors"]
    assert reproducible["valid"] is False
    assert reproducible["errors"]


@pytest.mark.parametrize(
    "preamble",
    _REPRODUCIBLE_DATETIME_CLASS_FLOWS + _REPRODUCIBLE_FORBIDDEN_DATETIME_IMPORTS,
)
def test_reproducible_loader_rejects_datetime_class_ingress_before_execution(
    preamble: str,
) -> None:
    source = _strategy_source(f"{preamble}\nraise RuntimeError('strategy module executed')")

    with pytest.raises(StrategyValidationError, match="validation failed") as exc_info:
        StrategyLoader().load("datetime_class_ingress", source, {}, reproducible=True)

    assert "strategy module executed" not in str(exc_info.value)


def test_reproducibility_boundary_allows_deterministic_datetime_symbols() -> None:
    source = _strategy_source(
        "from datetime import timedelta as Duration, timezone as TimeZone\n"
        "ONE_MINUTE = Duration(minutes=1)\n"
        "UTC = TimeZone.utc"
    )

    validation = validate_strategy_source(source, reproducible=True)
    loader = StrategyLoader()
    loaded = loader.load("deterministic_datetime_symbols", source, {}, reproducible=True)

    assert validation["valid"] is True, validation["errors"]
    assert loaded.instance.detect([], [], {}) == []
    loader.unload("deterministic_datetime_symbols")


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_BYPASS_SOURCES)
def test_reproducible_validator_rejects_controller_bypasses(preamble: str) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is True, ordinary["errors"]
    assert reproducible["valid"] is False
    assert reproducible["errors"]


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_BYPASS_SOURCES)
def test_reproducible_loader_rejects_controller_bypasses_before_execution(
    preamble: str,
) -> None:
    source = _strategy_source(f"{preamble}\nraise RuntimeError('strategy module executed')")

    with pytest.raises(StrategyValidationError, match="validation failed") as exc_info:
        StrategyLoader().load("controller_bypass", source, {}, reproducible=True)

    assert "strategy module executed" not in str(exc_info.value)


@pytest.mark.parametrize("preamble", _REPRODUCIBLE_UNORDERED_CONSTRUCTS)
def test_reproducible_validator_rejects_set_and_frozenset_construction(
    preamble: str,
) -> None:
    source = _strategy_source(preamble)

    ordinary = validate_strategy_source(source)
    reproducible = validate_strategy_source(source, reproducible=True)

    assert ordinary["valid"] is True, ordinary["errors"]
    assert reproducible["valid"] is False
    assert reproducible["errors"]


@pytest.mark.parametrize(
    "preamble",
    [
        "import services.strategies.base as strategy_api\nAMBIENT = strategy_api.settings",
        "import services.strategies.base as strategy_api\nAMBIENT = strategy_api.utcnow",
        "from models.opportunity import datetime as model_datetime",
        "import models.opportunity as opportunity_models\nAMBIENT = opportunity_models.datetime",
    ],
)
def test_reproducible_validator_rejects_undeclared_application_symbols(
    preamble: str,
) -> None:
    validation = validate_strategy_source(_strategy_source(preamble), reproducible=True)

    assert validation["valid"] is False
    assert validation["errors"]


def test_reproducible_validator_allows_explicit_deterministic_application_symbols() -> None:
    source = _strategy_source(
        "from services.strategies.base import "
        "DecisionCheck, ExitDecision, StrategyDecision\n"
        "from models.opportunity import "
        "AIAnalysis, ExecutionConstraints, ExecutionLeg, ExecutionPlan, "
        "MispricingType, Opportunity, ROIType\n"
        "import services.strategies.base as strategy_api\n"
        "import models.opportunity as opportunity_models\n"
        "DECISION_TYPE = strategy_api.StrategyDecision\n"
        "OPPORTUNITY_TYPE = opportunity_models.Opportunity"
    )

    validation = validate_strategy_source(source, reproducible=True)

    assert validation["valid"] is True, validation["errors"]


def test_reproducible_loader_rejects_before_strategy_module_execution() -> None:
    source = _strategy_source("import random as rng\nraise RuntimeError('strategy module executed')")

    with pytest.raises(StrategyValidationError, match="validation failed") as exc_info:
        StrategyLoader().load("must_not_execute", source, {}, reproducible=True)

    assert "strategy module executed" not in str(exc_info.value)

# EvoSport P0-P2 Quick Start

EvoSport P0-P2 is a personal research slice. It runs one strategy against explicitly selected Homerun canonical data and publishes reproducible evidence. Every result remains `NOT_EVALUATED`: it is not a profit validation, `PASS`, trading recommendation, shadow promotion, or live order.

## Prerequisites

From the repository root, install the backend, start the configured PostgreSQL database, and apply migrations:

```bash
make install-backend
cd backend
export DATABASE_URL='postgresql+asyncpg://homerun:homerun@127.0.0.1:5432/homerun'
venv/bin/alembic upgrade head
```

On macOS, requirements select the native `xgboost` distribution; non-Darwin systems select `xgboost-cpu`. XGBoost still needs the OpenMP runtime on macOS. If import reports a missing OpenMP library, run `brew install libomp`, then rerun `make install-backend`.

The remaining data prerequisite is deliberate: the chosen football market must already exist as one or more canonical parquet files registered by Homerun in `ProviderDataset`. This command does not ingest arbitrary raw files. Each selected row must have:

- `storage_type = parquet` and a local `file://` `storage_uri`;
- exactly the intended YES/NO token IDs and full requested UTC coverage;
- `provider` equal to the contract venue;
- `payload_json` containing `canonical: true`, `schema_version: snapshots_v2`, `sport: football`, matching event/condition/token IDs, `market_type: total_goals_over_under`, `threshold: "2.5"`, and matching `market_start_time`/`market_end_time`;
- no undeclared extra parquet file in the selected storage directory.

The canonical parquet must contain decision-time market data, not the final football result. The result below enters Homerun only through the offline settlement store.

## Bind and freeze the selected catalog data

Create `FOOTBALL_BINDING.json`. Replace the IDs, venue, teams, times, and score with the selected catalog market. The contract closes no later than kickoff, and the result timestamps occur after kickoff.

```json
{
  "event": {
    "event_id": "event-2026-08-01-home-away",
    "sport": "football",
    "competition": "example-league",
    "season": "2026",
    "home_team": "Home",
    "away_team": "Away",
    "scheduled_start": "2026-08-01T11:00:00Z",
    "actual_start": "2026-08-01T11:00:00Z",
    "status": "finished",
    "source_ids": {}
  },
  "contract": {
    "contract_id": "contract-over-25",
    "event_id": "event-2026-08-01-home-away",
    "venue": "your-provider-name",
    "venue_market_id": "condition-id",
    "yes_token_id": "YES_TOKEN_ID",
    "no_token_id": "NO_TOKEN_ID",
    "market_type": "total_goals_over_under",
    "threshold": "2.5",
    "side": "over",
    "opens_at": "2026-08-01T10:00:00Z",
    "closes_at": "2026-08-01T11:00:00Z",
    "rule_version": "provider-football-v1",
    "settlement": {
      "includes_extra_time": false,
      "postponed_action": "void",
      "cancelled_action": "void",
      "abandoned_action": "void"
    }
  },
  "result": {
    "regulation_home": 2,
    "regulation_away": 1,
    "extra_time_home": 0,
    "extra_time_away": 0,
    "status": "finished"
  },
  "result_time": {
    "event_time": "2026-08-01T12:55:00Z",
    "observed_at": "2026-08-01T13:00:00Z",
    "ingested_at": "2026-08-01T13:01:00Z"
  }
}
```

Freeze exact catalog selection and write its validated settlement into Homerun:

```bash
venv/bin/python -m evosport.cli dataset freeze \
  --provider-dataset-id YOUR_PROVIDER_DATASET_ID \
  --football-binding FOOTBALL_BINDING.json \
  --output-root DATASETS \
  --start 2026-08-01T10:00:00Z \
  --end 2026-08-01T13:00:00Z | tee freeze-manifest.json
MANIFEST_ID=$(venv/bin/python -c 'import json; print(json.load(open("freeze-manifest.json"))["manifest_id"])')
printf '%s\n' "$MANIFEST_ID"
```

The immutable manifest is `DATASETS/<manifest-id>/manifest.json`, concretely `DATASETS/${MANIFEST_ID}/manifest.json`. It contains the selected ProviderDataset IDs, absolute canonical parquet paths, token IDs, sizes, SHA-256 hashes, UTC window, and football binding. Unknown IDs, uncovered windows, metadata mismatch, missing/changed bytes, or extra parquet files fail closed.

The freeze directory intentionally contains only `manifest.json`; it pins existing Homerun canonical parquet by absolute path and strong hash rather than duplicating it. Reproduction on another machine therefore requires those same paths and exact bytes, or a legitimate re-import and new freeze that produces a new manifest/fingerprint.

## Create the strategy and exact environment lock

This no-op strategy verifies the plumbing without creating a position. Replace it with the strategy under study when ready; the same selected data and evidence checks still apply.

```bash
cat > strategy.py <<'PY'
from services.strategies.base import BaseStrategy


class FootballOver25NoOp(BaseStrategy):
    strategy_type = "football_over25_noop"
    name = "Football O/U 2.5 no-op"
    description = "Verifies EvoSport data and settlement plumbing without orders."

    def detect(self, events, markets, prices):
        return []
PY
venv/bin/python -m pip list --format=freeze > evosport-requirements.lock
```

The lock must exactly equal the distributions installed in this active `venv`. The runner canonicalizes package names, compares the complete set before gateway dispatch, and binds the verified Python implementation/version and environment identity into the fingerprint and `environment.json` evidence. Do not hand-edit or reuse a stale lock.

## Validate and run

Create `EXPERIMENT.yaml` beside the strategy. `validation_start` and `validation_end` must exactly match the freeze command window.

```yaml
name: football-over25-noop
family_id: football-over25
dataset_manifest_path: DATASETS/${MANIFEST_ID}/manifest.json
strategy:
  slug: football-over25-noop
  source_path: strategy.py
  dependency_lock_path: evosport-requirements.lock
  config: {}
window:
  train_start: 2026-01-01T00:00:00Z
  train_end: 2026-08-01T10:00:00Z
  validation_start: 2026-08-01T10:00:00Z
  validation_end: 2026-08-01T13:00:00Z
execution:
  initial_capital_usd: 1000.0
  submit_p50_ms: 50.0
  submit_p95_ms: 100.0
  cancel_p50_ms: 50.0
  cancel_p95_ms: 100.0
split_method: time
max_trials: 1
seed: 7
```

Substitute the shell variable in the YAML, validate the schema, then run from a clean tracked worktree:

```bash
sed -i.bak "s/\${MANIFEST_ID}/${MANIFEST_ID}/" EXPERIMENT.yaml && rm EXPERIMENT.yaml.bak
venv/bin/python -m evosport.cli experiment validate EXPERIMENT.yaml
git status --short
venv/bin/python -m evosport.cli experiment run EXPERIMENT.yaml --artifact-root RUNS
```

`experiment validate` is schema-only. `experiment run` verifies the current selected `ProviderDataset` football metadata and settlement row before cache/orphan acceptance and immediately before execution, verifies them again after execution, and rejects any mid-run change. It also checks manifest bytes, effective consumed parquet, environment identity, gateway success, and the exact projected market plus per-token redemption values actually supplied to the engine before publishing evidence.

EvoSport requests one reproducible selected-execution mode from Homerun. That mode loads strategy defaults from the frozen source file plus only `strategy.config` from this experiment, uses the existing queue-only maker matcher without loading an active fill-probability model, keeps resolution offline, and fails closed on selected catalog, coverage, projection, recorded-event import, settlement, or engine errors. It omits dashboard/live calibration, global decomposition, ambient latency/data-quality, counterfactual, and ensemble captures because those inputs are not selected or fingerprinted. Ordinary Homerun backtests keep their normal runtime configuration, fill-model, and dashboard behavior.

## Artifacts

Each immutable `RUNS/<fingerprint>/` contains:

```text
RUNS/<fingerprint>/
├── artifact-manifest.json
├── dataset-manifest.json
├── decision.json
├── environment.json
├── environment.lock
├── experiment.yaml
├── report.html
├── result.json
└── strategy-package/
    └── <strategy-source-name>.py
```

The result includes the engine's effective ProviderDataset IDs, per-file catalog lineage, strong consumed-byte identity, projected football condition/tokens/market window, and each selected token's condition, redemption price, winning outcome, resolution time, and source. The minimal SQL publication record stores only the fingerprint, Homerun run ID, manifest ID, effective-data hash, artifact-manifest hash, and result hash; the full result lives in Homerun/evidence rather than being duplicated in EvoSport SQL.

## Safety boundary and tests

P0-P2 exposes no statistical G1-G4 evaluation, OOS consumption, shadow/live promotion, order submission, or `PASS`/`REJECT` decision.

```bash
venv/bin/python -m pytest tests/test_evosport*.py -q
venv/bin/python -m pytest tests/test_backtest_engine.py tests/test_backtest_settlement.py -q
venv/bin/python -m ruff check evosport tests/test_evosport_*.py
venv/bin/alembic heads
```

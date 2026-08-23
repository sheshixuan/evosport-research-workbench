<div align="center">
  <h1>EvoSport Research Workbench</h1>
  <p><strong>Personal sports alpha research workbench — a thin fork of Homerun.</strong></p>
  <p>严格 point-in-time 数据、可复现实验、防假 Alpha 的收益验证工具</p>
  <p><em>Reproducible, leakage-proof sports prediction research built on Homerun (AGPL-3.0)</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black" alt="React" />
  <img src="https://img.shields.io/badge/PostgreSQL-Async-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/License-AGPL_v3-blue" alt="License" />
</p>

---

## 这是什么 / What this is

**EvoSport** 是一个**个人体育 Alpha 研究工作台**（personal sports alpha research workbench），第一项研究固定为**赛前足球总进球 O/U 2.5**。

它不是一个从零写的系统，而是 **Homerun 的"薄分叉"**：整个项目基于开源平台 [**Homerun**](https://github.com/braedonsaunders/homerun)（作者 Braedon Saunders，AGPL-3.0），并固定在**上游基线提交 `c8e647f`** 之上。Homerun 提供成熟的预测市场基础设施（Python 策略引擎、L2 订单簿回放、成交模拟、shadow 运行、worker 集群、前端），EvoSport 只在其上叠加一层"严格研究协议"，**不改动回测引擎本身**。

> EvoSport is a personal research / evidence tool built as a *thin fork* of the open-source prediction-market platform **Homerun** (AGPL-3.0, © Braedon Saunders), pinned at upstream commit `c8e647f`. It reuses Homerun's engine and only adds a disciplined research layer (point-in-time data, immutable snapshots, pre-registered experiments, evaluation gates, hidden OOS) — it does **not** modify the backtest engine, and it never places real orders.

### 为什么基于 Homerun 分叉，而不是从零写
- 复用成熟的 **unified backtest / L2 回放 / 成交模拟**，避免重复造轮子；
- 复用 **recording plane**（Polymarket WS/REST 实时落盘）与 **shadow runtime**；
- EvoSport 的代码**全部集中在 `backend/evosport/`**，对 Homerun 核心只做少量、可解释、可随上游升级的接入 patch（详见「改动清单」）。

---

## 研究思路 / Design intent

Homerun 已有强大的交易能力，但它**不保证研究本身没有假 Alpha**——数据泄漏、反复试探、不真实的成交模型都会制造虚假收益。EvoSport 的存在意义是把"从数据到可信证据"的周期变得严谨可复现。核心约束（详见 [设计文档](docs/superpowers/specs/2026-08-19-evosport-research-workbench-design.md)）：

1. **严格 point-in-time**：所有数据记录 `event_time` / `observed_at` / `ingested_at` 三时间戳；回测只能读 `ingested_at <= decision_time` 且当时有效的数据；修订只追加、不覆盖。
2. **不可变内容寻址数据集**：数据集快照（manifest）= 文件 SHA-256 + 时间窗 + 足球绑定，冻结后不可变；相同指纹复用结果。
3. **预注册实验（pre-registration）**：策略代码、数据、时间窗、参数空间、试验预算、评价标准在运行前固定；未预注册的运行只能标记 `EXPLORATORY`。
4. **G0–G6 评估门**：预注册 → 数据完整性（拒未来信息）→ 开发验证 → walk-forward（purge/embargo、DSR/PBO/CPCV）→ **hidden OOS（一次性 token，研究者不可读）** → shadow 校准 → 极小资金实盘（MVP 不实现）。
5. **Agent 增量价值对照**：市场概率基线 / 人工简单策略 / Agent 策略三组用同一数据与预算对比，防止 Agent 靠大量试验制造假 Alpha。
6. **fail-closed**：时间、规则、数据质量、成交模型任何异常都终止运行，绝不退化为零成本回测。

MVP（P0–P2）当前实现：**一条命令完成数据冻结 → 回测 → 评估 → 报告**，所有结果按设计返回 `NOT_EVALUATED`（不宣称任何统计晋级）。

> English summary of the intent: EvoSport adds a *research constitution* on top of Homerun — point-in-time temporal rules, immutable content-addressed dataset snapshots, pre-registered experiment specs with deterministic fingerprints, a G0–G6 evaluation gate chain ending in a one-time hidden out-of-sample evaluation, and shadow calibration — so that the only thing an experiment can prove is real, repeatable edge. The current MVP (P0–P2) wires frozen football O/U 2.5 data plus a Python strategy into one reproducible Homerun backtest and a content-addressed evidence report.

---

## 改动清单 / What was changed

### 新增（EvoSport 层，全部在 `backend/evosport/`）
```
backend/evosport/
  cli/           # evosport CLI（dataset freeze / experiment validate / run）
  domain/        # 时间安全领域模型（TemporalEnvelope、CanonicalSportsEvent/Contract）
  semantics/     # 足球 O/U 结算语义 + 离线结算存储
  data/          # 不可变内容寻址数据集快照（freeze / manifest / 校验）
  experiments/   # 实验 spec、确定性指纹、Homerun 回测网关、registry（SQL）
  reports/       # JSON/HTML 证据报告
```
对应迁移：`backend/alembic/versions/202608190001_create_evosport_registry.py`（新增 `evosport` schema）。
测试：`backend/tests/test_evosport_*.py`（含合成数据端到端与真实网关集成）。

### 对 Homerun 核心的少量接入 patch（可解释、可升级）
设计文档要求只允许四类接入；实现中对以下文件做最小修改以支撑"选定数据集"与离线结算：
- `services/backtest/unified_runner.py`、`services/backtest/settlement*.py` —— 选定数据集覆盖语义 + 离线结算；
- `services/marketdata/{coverage,view,projection}.py` —— 显式数据集选择与投影；
- `services/strategy_loader.py` 等 —— 支撑 EvoSport 策略注册。

### 本仓库修复的一个上游 bug
`202608190001` head 迁移在**全新数据库**上执行 `alembic upgrade head` 必然失败（基线迁移的 `create_all` 已建 `evosport.evidence_publications`，head 又重复创建）。已改为幂等守卫（存在则跳过），从零初始化的开发者/CI 现在可以直接跑到 head。

---

## 如何运行 / How to run

### 本地开发
```bash
make install-backend        # Python 3.10–3.13 venv + 依赖
make install-frontend       # npm install
```
准备好本地 PostgreSQL（如 `homerun/homerun@127.0.0.1:5432/homerun`）后：
```bash
export DATABASE_URL='postgresql+asyncpg://homerun:homerun@127.0.0.1:5432/homerun'
cd backend && venv/bin/python -m alembic upgrade head
venv/bin/python -m uvicorn main:app --reload --port 8000     # 后端 API
cd ../frontend && npm run dev                                # 前端 → http://localhost:3000
```
> macOS 注意：若启动报 `OMP: Error #15`（torch/sklearn/faiss 的 libomp 冲突），设 `KMP_DUPLICATE_LIB_OK=TRUE`。

### Docker（本地构建，不拉上游镜像）
```bash
cp .env.example .env      # 至少填 APP_SECRETS_KEY
docker compose up --build
```
> 分叉的 compose 已改为从本地 Dockerfile 构建（`homerun-backend:local` / `homerun-frontend:local`），不会拉取上游 ghcr 二进制。

### EvoSport CLI（P0–P2 竖切）
详见 [`docs/evosport/quickstart.md`](docs/evosport/quickstart.md)：
```bash
venv/bin/python -m evosport.cli dataset freeze --provider-dataset-id <id> --football-binding FOOTBALL_BINDING.json --output-root DATASETS --start ... --end ...
venv/bin/python -m evosport.cli experiment validate EXPERIMENT.yaml
venv/bin/python -m evosport.cli experiment run EXPERIMENT.yaml --artifact-root RUNS
```

---

## 数据从哪来 / Where the data comes from

- **自采（recommended for new matches）**：启动 `workers.host recording` plane，`RecordingFeedManager` 实时订阅 Polymarket 公开 WS 订单簿/成交 + REST baseline，`BookParquetSink` 落盘为 canonical `SNAPSHOT_SCHEMA` parquet，并在 `provider_datasets` 注册（`provider=live_ingestor`）。目录市场可用 `backend/scripts/seed_football_catalog.py` 播种。
- **历史存档**：`Telonex` / `polybacktest` 提供历史 Polymarket book 数据，导入后同样进入 `provider_datasets`。
- **合成 demo**：`backend/scripts/evosport_synthetic_demo.py` 生成一场合成足球 O/U 2.5 的端到端证据（无需任何外部数据）。
- **比赛结果**永远通过**离线结算存储**（`market_settlements`）注入，回测在结算时刻才读取——决策时点盘口与赛后结果严格隔离，杜绝泄漏。

自采数据做 EvoSport 实验时，football binding 的 `venue` 需填 `live_ingestor`（与 `provider_datasets.provider` 一致）。

---

## 许可证与归属 / License & attribution

本仓库为 **AGPL-3.0**（见 [`LICENSE`](LICENSE)），是 [Homerun](https://github.com/braedonsaunders/homerun)（AGPL-3.0，© Braedon Saunders，上游基线 `c8e647f`）的分叉。

- 原 Homerun README 保留于 [`docs/homerun-upstream-readme.md`](docs/homerun-upstream-readme.md)。
- 上游的 CI / 发布 workflow 与 greencheck/sloppy 配置被移入 [`docs/upstream-workflows/`](docs/upstream-workflows/)（本分叉不适用，保留可恢复）。
- **本软件不构成任何财务建议**；研究结论仅用于个人收益验证。
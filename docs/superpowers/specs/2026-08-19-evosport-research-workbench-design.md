# EvoSport 个人体育 Alpha 研究工作台设计

- 日期：2026-08-19
- 状态：设计已逐段确认，等待最终文档审阅
- 目标：以最低必要工程成本验证，受约束的 Agent 研究循环能否在严格的 point-in-time、成交和 OOS 条件下，产生超过简单基线的可重复净收益

## 1. 背景与决策

EvoSport 是个人研究与收益验证工具，不是对外商业产品。系统不需要客户、计费、多租户或商业护城河；它需要缩短从数据到可信证据的周期，并防止研究者或 Agent 通过数据泄漏、反复试探和不真实成交模型制造假 Alpha。

现有 Homerun 已覆盖预测市场策略加载、统一回测、L2 回放、成交模拟、autoresearch、shadow、事件记录和部分统计评估。EvoSport 采用“薄分叉 + 隔离扩展包”：直接复用 Homerun 的通用能力，只自建体育 point-in-time 语义、实验协议、独立评估门和自动报告。

第一项研究固定为赛前足球 O/U 2.5，不做滚球。MVP 同时只启用一个场所、一个体育数据源和一个市场数据源。参考回放与适配路径优先使用 Homerun 已成熟的 Polymarket 数据路径；任何实时连接及后续交易均以使用者对场所的合法可用性为前置条件。

## 2. 目标

1. 用一条命令完成数据冻结、回测、评估和报告，替代重复手工操作。
2. 保证每个实验可重现、可追溯，失败实验与成功实验同等登记。
3. 将时间完整性、合约结算语义、费用、成交和试验次数纳入统一评价。
4. 隔离 hidden OOS，防止研究端和 Agent 通过反复查询污染最终验证集。
5. 将人工/简单策略作为对照，单独测量 Agent 的增量价值。
6. 通过 Homerun shadow 校准回测与真实市场微观结构的偏差。
7. 保持对 Homerun 的核心改动少量、可解释、可随上游升级。

## 3. 非目标

MVP 不建设以下能力：

- 新回测或交易执行引擎；
- Strategy DSL；
- 通用多智能体平台；
- 自定义前端；
- 多场所套利；
- 滚球交易；
- 多运动或多市场类型；
- 组合优化；
- 自动实盘晋级；
- 钱包托管；
- 商业审计、签名证书、多租户、计费和权限后台；
- LLM 修改评估规则、数据切分、费用模型或风险限制。

## 4. 总体架构

```text
体育数据源 + 场所市场数据
             │
             ▼
不可变原始数据与三时间戳
             │
             ▼
EvoSport 体育事件—市场合约语义层
             │
             ▼
冻结并哈希的 Dataset Snapshot
             │
             ▼
Strategy Package + Experiment Spec
             │
             ▼
Homerun Unified Backtest / Replay
             │
             ▼
EvoSport Evaluation Gate G1–G4
             │
             ▼
Evidence Report ──通过──▶ Homerun Shadow
                              │
                              ▼
                    Backtest/Shadow 偏差报告
                              │
                              ▼
                         人工晋级决策
```

### 4.1 所有权边界

Homerun 负责：

- 市场接入与通用数据模型；
- Python 策略加载和版本；
- unified backtest、订单簿回放和成交模型；
- worker、任务执行和运行事件；
- shadow runtime；
- 已有 DSR/PBO/CPCV 等统计实现；
- 现有前端，仅作为辅助观察面。

EvoSport 负责：

- 体育数据 adapter；
- point-in-time 事件和合约语义；
- 数据快照、实验定义和指纹；
- 全试验谱系与预算；
- G0–G6 Evaluation Constitution；
- hidden OOS 隔离与一次性 token；
- Evidence Report；
- shadow 偏差判断和晋级门；
- 面向个人研究的 CLI 流水线。

### 4.2 代码边界

EvoSport 代码集中在 `backend/evosport/`：

```text
backend/evosport/
  adapters/       # 体育数据及 Homerun 接口适配
  domain/         # 赛事、合约、manifest、实验等领域模型
  data/           # 原始数据、规范化、快照和质量检查
  semantics/      # 体育事件与合约规则的 point-in-time 连接
  experiments/    # spec、runner、registry、fingerprint
  evaluation/     # G1–G4、试验预算、决策策略
  oos/            # 一次性 token 与隔离 evaluator
  shadow/         # 部署及 backtest/shadow 对照
  reports/        # 结构化结果与 HTML 报告
  cli/            # 命令入口
```

Homerun 核心只允许增加四类接入 patch：注册体育数据提供者、注册 EvoSport 策略、调用 unified backtest、向 shadow runtime 注册已通过候选。任何其他核心修改必须先证明现有扩展点无法满足正确性要求。

保留 Homerun 上游 remote 和固定基线 commit。每次同步上游后，运行 Homerun 原测试、EvoSport contract tests 和固定端到端实验。MVP 不修改 Homerun 前端。

## 5. 数据与语义模型

### 5.1 时间语义

所有可进入实验的数据至少记录：

- `event_time`：现实事件发生时间；
- `observed_at`：数据源宣称其可观察的时间；
- `ingested_at`：EvoSport 实际获得数据的时间；
- `effective_from` / `effective_to`：规则或事实版本的有效区间；
- `source_revision`：数据源修订标识；
- `raw_payload_hash`：原始响应哈希。

回测只能读取 `ingested_at <= decision_time` 且当时有效的数据。修订记录以新版本追加，禁止覆盖旧值。

### 5.2 CanonicalSportsEvent

统一赛事对象包含运动、联赛、赛季、参赛方、计划与实际开赛时间、状态、比赛阶段、结果及来源映射。它提供稳定内部 ID，但保留每个数据源的原始 ID 和名称。

### 5.3 CanonicalSportsContract

统一合约对象连接赛事与交易场所 market/token，包含：

- 场所及原始 market/token ID；
- 市场类型和阈值，例如 `TOTAL_GOALS_OVER_UNDER`、`2.5`；
- 交易开放/关闭时间；
- 结算数据源；
- 是否包括加时；
- 延期、取消、中止和重赛处理；
- 规则文本、规则版本和有效时间；
- 最终结算及异常说明。

Canonical 模型只统一可统一的语义，不抹掉场所差异。

### 5.4 DatasetManifest

数据快照 manifest 记录来源、时间边界、文件哈希、schema 版本、数据质量结果、赛事/合约数量、规则覆盖率和构建代码 commit。快照创建后不可修改；更正数据产生新快照。

原始及特征数据使用 Parquet/内容寻址目录，元数据使用 Homerun Postgres 中独立的 `evosport` schema。MVP 不新增 Redis、工作流数据库或 MLflow。

## 6. 实验模型与自动化

### 6.1 ExperimentSpec

每次实验通过 Pydantic/YAML 固定：

- 策略包和父策略；
- 数据 manifest；
- train、validation 和 hidden OOS 的时间边界；
- 最大特征 lookback；
- 参数空间和最大试验次数；
- 费用、滑点、延迟、成交及资金模型；
- 随机种子；
- 主要指标、基线和晋级门；
- evaluator 版本和 Homerun commit。

未预注册的运行只能标记为 `EXPLORATORY`，不能晋级。

### 6.2 可复现指纹

```text
run_fingerprint = hash(
  strategy_code
  + dependency_lock
  + dataset_manifest
  + experiment_spec
  + evaluator_version
  + Homerun_commit
  + random_seed
)
```

相同指纹默认复用已有结果。Agent 的自然语言输出必须先固化为策略代码、参数和 prompt record，之后才能进入实验。

### 6.3 CLI

```text
evosport data sync football-over25
evosport dataset freeze football-over25
evosport experiment validate experiments/over25-v001.yaml
evosport experiment run experiments/over25-v001.yaml
evosport experiment status <run-id>
evosport report open <run-id>
evosport oos evaluate <candidate-id> --confirm-burn <dataset-id>
evosport shadow start <candidate-id>
evosport shadow evaluate <candidate-id>
```

日常命令 `evosport pipeline run <spec> --through walk-forward` 自动完成数据同步、质量检查、冻结、G0–G3、结果登记和报告。Hidden OOS 永远不包含在默认流水线中。

## 7. Evaluation Constitution

### 7.1 不可修改规则

1. 数据按时间顺序切分，禁止随机切分。
2. 运行前登记策略、参数、数据、试验预算和评价标准。
3. 人工与 Agent 策略使用相同数据、算力、试验预算和 evaluator。
4. hidden OOS 对策略家族只使用一次；查看结果后的修改属于新家族。
5. 统计显著、预测准确、经济收益和成交真实性必须同时评价。

### 7.2 G0：预注册

冻结策略哈希、数据、窗口、参数空间、试验预算、成本模型、指标和晋级门。Agent 无权在运行中修改这些内容。

### 7.3 G1：数据完整性

检查未来信息、修订覆盖、时间错位、规则缺失、延期/取消/加时、盘口覆盖、重复和异常缺口。失败返回 `INVALID_DATA`，不评价策略。

### 7.4 G2：Development Validation

仅使用 train/dev，比较市场概率基线、简单固定策略和候选策略。评价 Brier Score、Log Loss、校准、费用后净收益、收益集中度、参数稳定性和悲观成交情景。允许修改策略，但每次修改均计入谱系和预算。

### 7.5 G3：Walk-forward Validation

执行时间滚动训练/验证。purge/embargo 至少覆盖特征 lookback、数据发布延迟及同场/相邻市场相关周期。评价窗口稳定性、DSR、CPCV/PBO、按联赛或比赛日聚类的 block bootstrap、费用和成交敏感性、最大回撤及收益集中度。

具体阈值不写死在代码中。`max_trials_per_family`、`min_effective_sample_size`、`max_pbo`、`min_dsr_confidence`、`min_net_ev_lower_bound`、`max_pnl_concentration`、`max_drawdown` 和 `max_backtest_shadow_gap` 在实验预注册时冻结。样本不足返回 `NEEDS_MORE_DATA`，不以任意固定场次数替代有效样本判断。

### 7.6 G4：Hidden OOS Burner

只有 G3 通过的不可变策略包可申请 token。评估器使用独立服务账户、数据库权限和只读数据挂载；研究环境和 Agent 无法列出或读取 OOS。结果为 `PASS`、`REJECT` 或 `NEEDS_MORE_DATA`。

token 在读取数据前事务性预留。如果基础设施失败且没有任何指标、日志或逐场信息泄露，可在 evaluator 内部重试；一旦研究端看到任何策略相关结果，该 OOS 即被消耗。报告失败只重建报告，不重新评估。系统不提供恢复已消耗 OOS 的命令。

### 7.7 G5：Shadow

只有 OOS PASS 的策略可引用原不可变策略包进入 Homerun shadow。比较理论信号、可成交价格、模拟成交率、持仓时间、费用后 PnL 和未成交损失。满足最低有效样本、覆盖不同流动性状态且 backtest/shadow 偏差可解释后，才可标记 `SHADOW_PASS`。

### 7.8 G6：极小资金实盘

MVP 不实现自动实盘。未来实盘只能人工批准，并预先固定可完全损失的总试验预算、单笔风险和停止条件。Agent 无权提高额度或从 shadow 切换为 live。

## 8. 状态机与故障处理

### 8.1 Run 状态

```text
CREATED → VALIDATED → QUEUED → RUNNING → EVALUATING
        → SUCCEEDED / FAILED / CANCELLED
```

重试创建新 `attempt_id`，保留旧记录。

### 8.2 Candidate 状态

```text
EXPLORATORY
  → WALK_FORWARD_PASSED
  → OOS_APPROVED
  → OOS_RUNNING
  → OOS_PASS / OOS_REJECT / NEEDS_MORE_DATA
  → SHADOW_ACTIVE
  → SHADOW_PASS / SHADOW_FAIL
  → LIVE_ELIGIBLE
```

任务故障和策略判定相互独立，不能通过重试掩盖策略失败。

### 8.3 Fail-closed 原则

- 外部 API 暂时失败：指数退避；超时后终止快照。
- 时间、规则或数据质量异常：`INVALID_DATA`，禁止静默插值。
- 策略异常：记录失败并消耗一次试验预算。
- worker 崩溃：相同不可变输入创建新 attempt。
- 费用或成交模型缺失：终止，不退化为零成本回测。
- shadow 数据断流：暂停模拟订单，恢复后不补造历史订单。
- 报告失败：保留结构化结果并单独重试。
- 场所 API/规则变化：冻结晋级并重新运行 contract tests。

## 9. 产物与报告

每次运行保存：

```text
runs/<run-id>/
  experiment.yaml
  dataset-manifest.json
  strategy-package/
  environment.lock
  execution-model.json
  trial-ledger.jsonl
  metrics.parquet
  result.json
  decision.json
  report.html
```

报告依次呈现晋级结论、指纹、数据质量、试验预算、基线对比、walk-forward 稳定性、统计证据、收益集中度、成本/成交敏感性、异常和下一步允许动作。个人自用版本使用内容哈希和只追加记录，不建设第三方证书系统。

## 10. Agent 增量价值实验

每个研究周期保持三组：市场概率基线、人工/固定规则策略、Agent 策略。三组使用同一数据、执行模型、试验预算和 Evaluation Gate。

```text
Agent 增量价值 =
  Agent 组费用后结果
  - 最佳简单基线费用后结果
  - Agent 额外数据、模型和计算成本
```

如果 Agent 只增加实验数量，而不能改善 OOS/shadow 结果或减少研究时间，则关闭自动发现，仅保留工具化数据、回测和评估。

## 11. 测试设计

### 11.1 时间与泄漏测试

构造比赛后信息进入赛前、修订覆盖历史、伤停发布时间晚于信号、市场/赛事时间错位和跨合约污染。系统必须拒绝。

### 11.2 结算 Golden Cases

覆盖正常结束、加时、延期、取消、中止、重赛以及跨场所规则差异。semantic layer 修改后必须全部通过。

### 11.3 确定性测试

相同指纹必须得到相同评价结论。报告失败、服务重启和 worker 重试不得改变结果。

### 11.4 反 Alpha 测试

随机标签、随机信号、未来泄漏、单赛季偶然策略、极端参数搜索、零成本假盈利及不可能成交策略均不得通过 Evaluation Gate。

### 11.5 OOS 权限测试

验证研究账户和 Agent 无权读取 OOS，evaluator 只能读取 token 范围，策略容器无网络出口且不能读取主机密钥或其他策略结果。

### 11.6 故障注入

主动终止 API、worker、Postgres、报告任务和 shadow feed，确认系统可恢复且不会错误地产生 PASS 或虚构订单。

### 11.7 上游兼容性

每次同步 Homerun 后运行其原测试、adapter contract tests、固定小数据端到端实验、backtest/shadow 一致性和核心 patch 清单检查。

## 12. 实施顺序

### P0：Homerun 基线

固定可运行的上游 commit，跑通原测试及一个自带回测，建立薄分叉和同步规则。不写 EvoSport 功能。

### P1：体育数据与语义

接入一个数据源，建立不可变原始存储、CanonicalSportsEvent、CanonicalSportsContract 和 Golden Cases。此阶段开始替代手工整理。

### P2：Experiment Runner 与 Registry

实现 YAML、数据冻结、fingerprint、Homerun 回测调用、结果保存和报告。一条命令完成可复现实验，解决主要手工效率问题。

### P3：Evaluation Gate G1–G3

实现泄漏检查、walk-forward、试验预算、DSR/PBO/CPCV、成本/成交敏感性和基线对比。完成后系统可作为个人研究工作台。

### P4：简单基线

建立市场概率、固定规则和传统统计模型，作为 Agent 对照组。

### P5：Hidden OOS

实现独立权限、一次性 token、审计日志和 fail-closed 测试。

### P6：Shadow

实现不可变策略部署、backtest/shadow 对照、断流 kill switch 和偏差报告。

### P7：Agent Research

最后接入 Homerun autoresearch 或外部模型。Agent 只读取 train/dev、使用固定预算、输出不可变策略包并通过相同 Evaluation Gate。

## 13. MVP 验收标准

MVP 必须同时满足：

- 一条命令完成数据冻结、回测、评估和报告；
- 同一指纹重复运行得到相同结论；
- 故意泄漏未来数据的策略被拒绝；
- 随机和过度搜索策略无法通过；
- hidden OOS 对研究环境不可见；
- 未通过 OOS 的策略不能进入 shadow；
- shadow 断流不会产生虚假订单；
- 没有任何自动 live order 路径；
- EvoSport 对 Homerun 的核心修改保持为少量、可解释的接入 patch；
- Agent 研究必须能够与简单基线按相同预算比较。

## 14. 主要风险与控制

| 风险 | 控制 |
|---|---|
| Homerun 上游变化导致分叉失控 | 隔离 `backend/evosport`、固定四类接入点、自动兼容测试 |
| 体育和盘口数据缺失或权利不足 | 原始数据保留、manifest、单源 MVP、使用前核验存储与研究权利 |
| 样本太少却产生高收益结果 | 有效样本与不确定性门、`NEEDS_MORE_DATA`、等待未来数据 |
| Agent 通过大量试验制造假 Alpha | 全试验登记、固定预算、DSR/PBO/CPCV、一次性 OOS |
| 回测成交无法复现 | 悲观情景、shadow 校准、偏差超限自动退回 |
| OOS 被间接泄漏 | 独立账户和挂载、有限输出、一次性 token、fail-closed |
| 场所 API、规则或地域条件变化 | 官方接口适配、contract tests、人工启用实时连接 |
| 工具范围再次扩张 | 非目标清单、build-or-reuse 评审、Agent 和 UI 延后 |

## 15. 已冻结设计决策

- 项目是个人收益验证工具，不做商业化功能。
- Homerun 是主要技术底座，EvoSport 采用薄分叉。
- EvoSport 代码集中在独立包，Homerun 核心仅允许四类接入 patch。
- 第一项研究是赛前足球 O/U 2.5，暂不做滚球。
- MVP 单场所、单体育源、单市场源、CLI 优先。
- 先完成工具化数据、回测和评估，再接入 Agent。
- hidden OOS 必须显式消耗，不能进入默认一键流水线。
- MVP 只到 shadow，不包含自动实盘。

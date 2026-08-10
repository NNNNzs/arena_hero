# Arena Hero 行为树与指挥中心实施进度

更新时间：2026-08-10。本文记录工作区中已经存在的实现和本次实际验证结果；实施计划的目标与后续验收条件见 [行为树、任务调度与作战指挥中心实施计划](../.hermes/plans/2026-08-09_231236-arena-hero-behavior-tree-command-center.md)。本文不把纯逻辑状态机、shadow 或 canary 骨架表述为真实对战已上线。

## 项目目标和架构决策

目标是从每 Tick 重选全局 `StrategicMode` 的 legacy planner，逐步迁移到“全局 Goal/Task 调度器 + 每实体行为树 + 统一仲裁/校验”的分层结构。所有候选动作仍须由当前权威 `Turn` 校验、只映射当前 Turn controller、每对象至多一个动作，并由 `tactic.py` 一次性提交。

当前迁移采用可回退的并存方式：`LegacyPlannerAdapter` 只读地投影 legacy 决策为 trace；调度器可作为 shadow 观察；未来实体行为树和 objectives 应先经 feature flag/canary 接入，最后才替换 legacy 动作来源。持久化只发生在成功提交之后；trace/audit 使用脱敏别名和有界写入。

## 已完成

### Phase 1：领域模型、兼容 trace 与状态迁移

- 已有领域模型与生命周期记录：`arena_tactic/domain/{values,lifecycle,policy,commands,trace}.py`，测试为 `tests/test_domain_models.py`、`tests/test_decision_trace.py`。
- `arena_tactic/planning/legacy.py` 的 `LegacyPlannerAdapter` 将已经通过当前 Turn 校验的 legacy intents 投影为 schema-v1 `DecisionTrace`；它不改变 controller 分配或 replay-v1 记录。
- `arena_tactic/observability.py`、`arena_tactic/runtime.py` 与 `tactic.py` 已接入有界 trace sink 的提交后写入和关闭路径；`arena_tactic/memory.py` 当前写入 schema v3，并读取 v1/v2 的兼容状态。
- 实际验证：在项目 `.venv` 中，2026-08-10 运行完整主测试为 `148 passed in 2.00s`；这证明离线实现和测试夹具通过，不证明 API key、网络、服务器接收或真实策略收益。

### Phase 2：确定性调度器（shadow）

- 已有纯调度器 `arena_tactic/scheduler.py`，包括角色匹配、实体/目标容量锁、租约、阻塞与抢占记录；测试为 `tests/test_scheduler.py`。
- `arena_tactic/runtime.py` 在 `AgentConfig.scheduler_shadow=True` 时，仅从当前实体构建 `LEGACY_SHADOW` task 并记录 `ScheduleResult`/trace；该路径不排队 SDK 动作。
- 默认语义：`scheduler_shadow=False`。开启后 action source 仍是 `arena_tactic.strategy.propose_intents`，不是调度器；关闭即可停止 shadow 观察，不需数据回退。
- 实际验证：完整主测试通过；`test_shadow_scheduler_records_trace_without_changing_legacy_sdk_plan` 覆盖 shadow 不改变 legacy SDK plan。

### Phase 3：行为树基础与 canary 预留

- 已有最小同步行为树引擎 `arena_tactic/behavior_tree.py`，实现 Composite、Condition、Action、Selector、Sequence、Timeout、Retry、AbortIf、黑板、跨 Tick cursor、halt 与 trace；测试为 `tests/test_behavior_tree_engine.py`。
- `AgentConfig.worker_bt_canary` 存在且默认 `False`。当前 runtime 在 shadow trace 中明确记录：flag 为 `False` 时 `DISABLED`，为 `True` 时仍为 `NOT_IMPLEMENTED`。
- 因此，Worker canary 尚未接管动作；Vanguard、Ranger、Core 也没有行为树动作路径。当前所有实际动作仍来自 legacy strategy，尚不存在混合来源仲裁。
- 实际验证：完整主测试通过；行为树引擎测试已包含在该集合。计划中列出的 `tests/test_entity_trees.py` 当前不存在，说明四类实体树尚未实现。

### Phase 4/5/6：已实现的纯逻辑 objectives，尚未接管动作

| 阶段 | 已实现的纯逻辑 | 实际文件与测试 | 默认开关与边界 |
| --- | --- | --- | --- |
| Phase 4 Beacon Campaign | `ASSEMBLE`、`PICKUP`、`HOLD`、`RECOVER` 生命周期；只生成 Goal、Task 与候选种类。 | `arena_tactic/objectives/beacon.py`；`tests/test_beacon_campaign.py` | `beacon_campaign_v1=False`；不映射 `PICKUP_BEACON`/修盾等 SDK 动作，不主动 drop Beacon。 |
| Phase 5 Core 迁移 | cargo 召回、开始移动、移动等待、失败重规划、完成的计划状态。 | `arena_tactic/objectives/core_migration.py`；`tests/test_core_migration_plan.py` | `core_migration_v1=False`；不对 Core 调用或排队 `START_MOVE`，不会接管 legacy migration。 |
| Phase 6 敌 Core 攻击 | 集结、接战、失视重获、撤退、权威击杀确认；只生成任务和候选种类。 | `arena_tactic/objectives/core_attack.py`；`tests/test_core_attack_campaign.py` | `core_attack_campaign_v1=False`；不执行 `SHOOT_CELL` 或 `SWEEP`，不以历史目标发起攻击。 |

这些 modules 在 `arena_tactic/objectives/__init__.py` 中明确为 controller-free；`tests/test_scheduler.py` 还验证即使三个 objective flags 都显式设为 `True`，legacy SDK plan 仍不变。它们是受测纯逻辑骨架，不是实际对战功能上线。

## 未完成：Phase 7/8/9 与依赖

- Phase 7 Command API 未开始：尚无 command queue/applier/auth/web API，也没有 next-Tick 命令消费、CSRF、幂等、`If-Match`、TTL 或 emergency-stop 写路径。它依赖现有 domain command schema、trace/audit 基础以及后续 runtime pipeline 接入。
- Phase 8 指挥中心 UI 未开始：当前 `arena_tactic/dashboard.py` 仍是既有只读 dashboard；没有计划中的 static 模块、任务/行为树投影或命令面板。它依赖 Phase 7 的安全 command API，以及实体任务/BT 执行 trace。
- Phase 9 legacy 移除未开始：`arena_tactic/runtime.py` 仍直接调用 `strategy.propose_intents`，`StrategicMode` 仍在 `arena_tactic/models.py`。依赖四类实体树、统一仲裁、objectives 的真实受控接入、回放/shadow/canary 证据，且计划要求连续 500 Tick 后才可移除 legacy。

## 当前测试基线

以下命令于 2026-08-10 在项目 `.venv`（`arena-hero` 0.2.9）实际运行：

| 命令 | 原始结论 |
| --- | --- |
| `python3 -m compileall -q tactic.py arena_tactic tests` | 成功（退出码 0，无输出）。 |
| `python3 -m pip check` | `No broken requirements found.` |
| `python3 -m pytest -q` | `148 passed in 2.00s`。 |
| `PYTHONPATH=.agents/skills/arena-hero python3 -m pytest -q .agents/skills/arena-hero/tests` | `27 passed in 0.37s`。 |

未激活 `.venv` 的系统 Python 也实际检查过：找不到 `arena_hero`，主测试在收集阶段报 14 个 import errors，skill 测试报 2 个 import errors；该系统环境的 `pip check` 还报告 `mkdocs 1.4.2` 与 `markdown 3.4.1` 的版本冲突。因此测试基线以已激活项目 `.venv` 为准，Mac 上也必须先安装/激活项目虚拟环境。

## 当前未提交状态与 Mac 运行前事项

本次开始时工作区已经有未提交生产代码、测试和文档变更：`arena_tactic/{memory,models,observability,runtime}.py`、`tactic.py`、`tests/test_runtime_replay.py` 已修改；`arena_tactic/{behavior_tree,identity,scheduler}.py`、`arena_tactic/domain/`、`arena_tactic/objectives/`、`arena_tactic/planning/` 与相应 Phase 测试为未跟踪内容；`.hermes/` 也未跟踪。本次只新增 `README.md`、本文档，并最小同步 runtime 文档；没有 commit，也没有 restart。

在 Mac 上运行前：使用 Python 3.11+；从干净或明确了解的工作树创建 `.venv` 并执行 `pip install -e .`；激活后再运行测试；仅把 `ARENA_HERO_API_KEY` 放入未跟踪 `.env` 或环境变量，绝不提交或打印。运行 `python3 tactic.py` 会连接真实服务，必须在确认 API key 和实时提交意图后才执行。当前全量离线测试通过不等于已经做过真实对战验证。

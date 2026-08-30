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
- 实际验证见本文末尾的当前测试基线；这些结论只覆盖离线实现和测试夹具，不证明 API key、网络、服务器接收或真实策略收益。

### Phase 2：确定性调度器（shadow）

- 已有纯调度器 `arena_tactic/scheduler.py`，包括角色匹配、实体/目标容量锁、租约、阻塞与抢占记录；测试为 `tests/test_scheduler.py`。
- `arena_tactic/runtime.py` 在 `AgentConfig.scheduler_shadow=True` 时，仅从当前实体构建 `LEGACY_SHADOW` task 并记录 `ScheduleResult`/trace；该路径不排队 SDK 动作。
- 默认语义：`scheduler_shadow=False`。开启后 action source 仍是 `arena_tactic.strategy.propose_intents`，不是调度器；关闭即可停止 shadow 观察，不需数据回退。
- 实际验证：完整主测试通过；`test_shadow_scheduler_records_trace_without_changing_legacy_sdk_plan` 覆盖 shadow 不改变 legacy SDK plan。
- `AgentConfig.scheduler_canary=False` 默认也关闭。启用它（或 `planner_canary=True`）后，scheduler 从当前资源、Core 防御槽位、Beacon/攻击 lifecycle 和已接受的人工任务构造有角色/目标/租约的 assignment；下一状态只保存脱敏 alias、任务、目标、优先级与 lease。Worker、Vanguard、Ranger 行为树消费相应 assignment，trace 显示 `SCHEDULED` 状态、任务和租约。默认 legacy 路径仍不受影响。
- 在 `planner_canary=True` 的统一仲裁层，跨角色竞争同一友方容量槽会按既有分数/UUID 顺序保留先到者，并为后续移动选择确定性安全邻格；无可用邻格时写入 `arbitrator_capacity_wait`。这避免新管线把可预见的容量竞争计入 rejected intent，同时不改变 legacy 的拒绝观测语义。

### Phase 3：行为树基础与 Worker canary

- 已有最小同步行为树引擎 `arena_tactic/behavior_tree.py`，实现 Composite、Condition、Action、Selector、Sequence、Timeout、Retry、AbortIf、黑板、跨 Tick cursor、halt 与 trace；测试为 `tests/test_behavior_tree_engine.py`。
- `AgentConfig.worker_bt_canary` 默认仍为 `False`。启用后，`arena_tactic/behaviors/worker.py` 的 `WorkerCanaryPlanner` 只从当前 `DecisionContext` 和 controller-free Blackboard 构造 Worker 候选；它覆盖同一 Worker 的 legacy proposal，随后仍统一经过现有 `validate_intents` 和 `apply_intents`。不可达的返航/探索路线会产生对应的显式 `WAIT` 原因；若树仍意外没有给出意图，则以 `bt_worker_no_intent` 记录该异常，而非掩盖为验证器安全兜底。若 canary 发生本地异常，runtime 保留已经构造的 legacy Worker 提案，并在 trace 标记 `FALLBACK_LEGACY`，以免错过命令窗口。
- Worker 树已有硬威胁回撤/存入货物、带货返航、当前可见资源采集/路径、探索前沿和四 Tick 无进展后重规划分支。运行时按当前 Worker UUID 保留树 cursor；对象不再出现在当前 Turn 时立即丢弃，绝不保存 SDK controller。
- Vanguard、Ranger、Core 现均有默认关闭的最小 canary：Vanguard 低血量撤回/同格治疗、当前相邻敌人横扫（敌 Core 优先）和守卫；Ranger 仅向当前可见且射线合法的目标射击，低血量撤回；Core 处理移动等待、HP 治疗、低盾修复、当前同格地面 Beacon 拾取、保留资源后的 Worker 生产和安全等待。所有本地 canary 异常均回退 legacy。当前仍是有限混合来源，尚不是计划目标中的完整任务调度/仲裁系统。
- `AgentConfig.planner_canary=False` 默认关闭。启用时 runtime 不调用 `strategy.propose_intents`，而是以 scheduler assignment、四类行为树、objectives、人工 assignment 与统一校验构造完整计划；legacy adapter 仅复用为兼容 trace 投影。Core 树还会在当前 `unit_cost(WORKER, population)` 预览后、保持最小资源保留的前提下生产 Worker。
- `tactic.py` 的常规 worker 同样默认使用全部关闭的配置；只有相应 `ARENA_HERO_*` feature flag 精确为 `1` 时才启用单项 canary。`ARENA_HERO_FULL_CANARY=1` 是显式的全链路离线/前台 canary 开关，会同时开启 scheduler、四类实体树、三类 objective 和 planner canary；关闭或移除变量即可回到 legacy，且不需要状态回退。
- 实际验证：`tests/test_entity_trees.py` 覆盖 Worker 接管不影响 Vanguard、跨权威 Tick 的树执行，以及停滞路由超时重规划；完整主测试和官方 skill 测试均通过。

### Phase 4/5/6：持久化 objective 与受限动作接管

| 阶段 | 已实现的纯逻辑 | 实际文件与测试 | 默认开关与边界 |
| --- | --- | --- | --- |
| Phase 4 Beacon Campaign | `ASSEMBLE`、`PICKUP`、`EXFIL`、`SECURE`、`RECOVER` 生命周期。 | `arena_tactic/objectives/beacon.py`；`arena_tactic/squad_coordination.py`；`tests/test_beacon_campaign.py` | `beacon_campaign_v1=False`；只有全局 `BEACON` 模式可以启动集结、拾取和失落恢复，且只能使用正式 `EXPEDITION_BEACON` 成员，不再临时抽走其他编组。拾取后 Vanguard 优先携带，完整编队以携带者为锚、最慢成员为节奏向 Core 撤离；Ranger 接敌射击时非接敌成员继续撤离。携带者进入 `beacon_secure_radius` 后转为 `SECURE`：携带者留在基地防线，原远征护卫并入 `MINING_ESCORT` 扩大矿区安全覆盖，Core 只用超过最低储备的资源向 10 点护盾修复。绝不主动 drop，也不把公开坐标交给 Worker。 |
| Phase 5 Core 迁移 | cargo 召回、同格存入、开始移动、移动等待、失败重规划、完成的计划状态。 | `arena_tactic/objectives/core_migration.py`；`tests/test_core_migration_plan.py` | `core_migration_v1=False`；`RECALL` 先让所有有货 Worker 回到当前 Core，已同格者先显式 `DEPOSIT`；直到当前权威 Turn 确认所有 Worker cargo 已清空，`START` 才向 Beacon 锚点提交一条当前可见、非障碍/资源/占用的安全相邻 `START_MOVE`。同一腿只授权一次，下一权威 Turn 观察到移动结束后再从当前格重启下一腿。手动迁移命令也沿用此安全路径。 |
| Phase 6 敌 Core 攻击 | 集结、接战、失视重获、撤退、权威击杀确认。 | `arena_tactic/objectives/core_attack.py`；`tests/test_core_attack_campaign.py` | `core_attack_campaign_v1=False`；当前可见 target 但未达 quorum 时，combat roster 向该当前格集结；`ENGAGE` 仅替换拥有合法射线 Ranger 的 `SHOOT` 和相邻 Vanguard 的 `SWEEP`；`RETREAT` 向己 Core 回撤。失视不会按历史目标攻击；只有此前保存的脱敏目标别名与当前 `CORE_DESTROYED/ATTACK` 的 `target_id` 一致时才标为确认。 |

这些 modules 在 `arena_tactic/objectives/__init__.py` 中明确为 controller-free。`arena_tactic/runtime.py` 仅在相应 flag 启用时评估它们，把脱敏 lifecycle checkpoint 放入下一份 `AgentMemory.objective_states`，并向 trace 添加 `SHADOW` Goal；只在上表列出的窄条件下替换当前对象的一条候选动作，最终仍经过统一 `validate_intents` 与当前 controller 映射。只有 `commit()` 在成功提交后才持久化 checkpoint。它们默认关闭，尚未经过真实对战验收。

## Phase 7：安全 Command API（已实现最小闭环）

- `arena_tactic/command_center.py` 提供线程安全版本化队列、可见 ASCII 幂等键、同键异体冲突、TTL、取消、准备/accepted 后 finalize 以及脱敏 AuditEvent。HTTP handler 不读取 Turn、不持有 controller、不直接改变 planner memory。
- `arena_tactic/command_api.py` 实现 `POST /api/v1/session`、snapshot、commands、audit、policy、实体任务和 emergency-stop/resume-auto；写请求要求 session、CSRF、`If-Match` 与 `Idempotency-Key`，并有固定窗口的登录/写请求限流。Origin/Host 不再参与认证或写请求校验，CSRF 仍保留。`ARENA_HERO_COMMAND_WRITE=1` 且配置 `ARENA_HERO_COMMAND_PASSWORD` 才开启写；默认关闭。服务默认绑定 `127.0.0.1`，远程暴露请通过网络层访问控制或反向代理限制。
- `AgentRuntime` 在每个当前 Turn 预读命令，但只在调用方确认 SDK accepted 后由 `commit()` finalize。紧急停机仅在下一 Tick 为当前 Core/Units 生成显式 `WAIT`；不会停止连接或编造对象。`ASSIGN_TASK` 现支持 `HOLD_POSITION`、`RETREAT_TO_CORE`、`HARVEST_VISIBLE`、`MOVE_TO_CELL` 四种有界类型；安全优先级高于人工任务，任务及取消仅在 accepted 后写入 alias-keyed memory。`POST /api/v1/core/migrations` 同样在 accepted 后启动受限的多 Tick Core 迁移。
- `PATCH /api/v1/policy` 现接受 allowlist 中的 `BALANCED`、`DEFENSIVE`、`ECONOMY`、`AGGRESSIVE`。命令在 accepted Tick 前保持 `QUEUED`，成功后才更新 API 读模型和持久化的 `AgentMemory.policy_state`；空白重启队列会从该 accepted memory 恢复 policy 读模型，不会覆盖已有队列命令。当 scheduler canary/planner canary 生效时，策略分别提高 Core 防御、当前可见资源采集或敌 Core 集结的 task priority/utility。人工任务和紧急/撤退安全规则仍可抢占 policy。
- 尚缺：更多 policy 参数和可解释的组合规则、更多人工任务种类与编队任务，以及面向生产代理的更强身份体系。

## Phase 8：指挥中心 UI（最小可用读模型）

- `frontend/` 为 Vue 3/TypeScript Dashboard 源码；`/` 展示服务、Goal、实体/行为树路径与命令状态，认证后可排队紧急停机/恢复、四种受限实体任务、Core 迁移和四种 allowlist 策略姿态。策略面板只读取 session 保护的 `/api/v1/policy`，提交仍须通过下一次 accepted Tick。
- `DashboardDataStore` 从有界 decision-trace 尾部构造 allowlist 投影；不透传 UUID、原始 payload、Cookie、API key 或未批准字段。页面仍通过 `/api/dashboard` 只读刷新，写命令由 `/api/v1` 的 session 边界处理。
- 页面现含本地 SVG 战术地图：仅投影当前可见的己方、敌方、资源、障碍和 Beacon 坐标，最多各 100/200 项；它不读取状态文件原文，也不显示未批准字段。
- 任务区现同时投影 `Goal → Task → 目标锁 → 租约`，并从有界 recent trace 构造最多 100 条按 Tick 排列的只读时间线；不会暴露原始 UUID 或命令 payload。单位状态卡片按 Core、Worker、Vanguard、Ranger 分组，合并同 Tick 的位置/HP/护盾/货物与决策链（当前任务、动作、下一步、唤醒条件、阻塞和 ETA）；任务表单直接从当前脱敏实体列表选择目标，支持优先级选择、撤回 `QUEUED` 命令及为已生效人工任务排队取消。已完成本地桌面和 390px 移动端浏览器验收。
- Dashboard 已迁移到 `frontend/` 的 Vue 3/TypeScript 组件、`DashboardStore`、`dashboardApi` 和 `useTacticalMap`；旧的后端 HTML、Command Center 静态脚本、样式和地图静态副本不再作为运行入口，Vue bundle 为 Git 忽略的构建产物，PixiJS vendor 资源继续使用本地文件。

## Phase 9：legacy 移除与验收（未达到移除门槛）

- 新增 `arena_tactic/canary.py`、`arena_tactic/replay_loader.py` 和相应测试：500 个合成权威 Turn、以及仓库现有 348 个脱敏 replay-v1 Tick，均在离线状态下同时开启 scheduler shadow、四类实体 canary 与 objectives，验证零超时、零拒绝、p95/max 决策小于 500ms 与可重复动作签名；最近 100 个 replay Tick 的每个当前实体均有 task、动作和原因 trace。`python3 -m arena_tactic.canary <replay> --min-ticks 500` 会读取轮转历史、选择最新最长连续 Tick 区间后双跑完整管线并输出不含回放内容的 JSON gate；样本不足时保持失败（退出码 2），不会悄悄放宽门槛。loader 只以 alias 派生离线 UUID，绝不访问凭据或调用 `Turn.submit()`，不能被视为真实服验证。
- 默认 `arena_tactic/runtime.py` 仍直接调用 `strategy.propose_intents`，`StrategicMode` 仍在 `arena_tactic/models.py`；但 `planner_canary=True` 已证明可以在不调用该函数的情况下由 scheduler assignment 和行为树生成完整计划。统一实体任务调度/仲裁尚未成为默认动作来源，并且代表性连续 500 Tick replay 目前只有 348 条脱敏样本、其中最长连续区间为 149 Tick；因此不能安全删除 legacy planner。

## 当前测试基线

以下命令于 2026-08-10 在项目 `.venv`（`arena-hero` 0.2.9）实际运行：

| 命令 | 原始结论 |
| --- | --- |
| `python3 -m compileall -q tactic.py arena_tactic tests` | 成功（退出码 0，无输出）。 |
| `python3 -m pip check` | `No broken requirements found.` |
| `python3 -m pytest -q` | `199 passed in 9.91s`。 |
| `PYTHONPATH=.agents/skills/arena-hero python3 -m pytest -q .agents/skills/arena-hero/tests` | `27 passed in 0.10s`。 |

同日已做有限的前台实时 smoke：以 `ARENA_HERO_FULL_CANARY=1 python3 tactic.py` 连接后，服务 `/status` 报告 `connected=true`，首轮在 Tick 82424–82443 连续 accepted。该样本发现三个实际问题：同格 cargo Worker 与 `START_MOVE` 的竞态、Beacon 护航的容量竞争，以及跨角色移动的容量竞争；前者改为先显式 `DEPOSIT` 并以 `DEPOSIT_SUCCEEDED` 后的权威 Turn 才开始迁移，后者改为 scheduler roster 加相邻独立槽位，通用冲突则由 canary 仲裁改道或解释性等待。由于人工重启导致 Tick 区间断开，且仍远少于 500 Tick，这不是 Phase 9 验收。

未激活 `.venv` 的系统 Python 也实际检查过：找不到 `arena_hero`，主测试在收集阶段报 14 个 import errors，skill 测试报 2 个 import errors；该系统环境的 `pip check` 还报告 `mkdocs 1.4.2` 与 `markdown 3.4.1` 的版本冲突。因此测试基线以已激活项目 `.venv` 为准，Mac 上也必须先安装/激活项目虚拟环境。

## 当前未提交状态与 Mac 运行前事项

本次工作区包含尚未提交的行为树、objectives、command center、静态 UI、测试和本文档变更；没有 commit、restart 或启动服务。提交前应先确认用户已有未提交文件的归属并只 stage 本任务范围。

在 Mac 上运行前：使用 Python 3.11+；从干净或明确了解的工作树创建 `.venv` 并执行 `pip install -e .`；激活后再运行测试；仅把 `ARENA_HERO_API_KEY` 放入未跟踪 `.env` 或环境变量，绝不提交或打印。运行 `python3 tactic.py` 会连接真实服务，必须在确认 API key 和实时提交意图后才执行。当前全量离线测试通过不等于已经做过真实对战验证。

# Arena Hero 运行、记忆、回放与测试

## 1. 每 Tick 数据流

```mermaid
sequenceDiagram
    participant SDK as Current Turn
    participant R as AgentRuntime
    participant P as Command/Scheduler/BT
    participant V as Validation
    participant M as Persistence

    SDK->>R: complete authoritative state
    R->>R: DecisionContext + AgentMemory.advance
    R->>P: accepted commands, objectives, assignments and per-entity proposals
    P-->>R: controller-free ActionIntent proposals + trace
    R->>V: current-object legality and reservations
    V-->>R: final and rejected intents
    R->>SDK: map to current controllers
    SDK->>SDK: submit exactly once
    SDK-->>R: Accepted receipt
    R->>M: atomic memory + redacted JSONL replay + trace/audit
```

`tactic.py` 只负责 API Key 加载、官方同步 SDK 循环、一次提交、成功提交后的持久化和 `Ctrl-C` 退出。`choose_actions(turn)` 是离线兼容入口：它生成并映射计划，但不连接网络、不提交、不写磁盘。

## 2. 决策对象

- `DecisionContext` 是不可变的当前 Turn 快照，包含 Core、己方 Unit、当前可见敌人、资源、障碍、Beacon、占用索引、容量和上 Tick 事件。
- `ActionIntent` 不持有 SDK Controller，只保存当前对象 UUID、动作、目标、评分、原因、预计成本和预留格。
- `DecisionResult` 保存模式、最终意图、被拒意图、动作统计、等待原因、耗时、超时标志和下一版记忆。
- `AgentMemory` 不保存敌人 Controller 或可用于攻击的旧敌人事实。它仅以脱敏 alias 保存短期敌情轨迹（最后位置、最后可见 Tick、距 Core 距离、接近连续次数）以判断**当前仍可见**敌人是否接近；失视、Core 缺失或 `CORE_RESPAWNED` 会清空或使其不可用。

## 3. 记忆与事件

默认记忆文件是 `runtime/agent-state.json`，目录被 `.gitignore` 忽略。schema v4 状态包含版本号、最后 Tick、模式兼容标签、连续无资源 Tick、连续 Core 受击压力、永久障碍、带到期 Tick 的临时失败格、已探索格、资源观察、资源复查失败计数与冷却、legacy Unit 任务、人工任务、scheduler assignment、policy、objective lifecycle、已处理事件 ID 和脱敏计数。新增字段均可选，旧 v1/v2/v3/v4 文件缺失时使用安全默认值；Core 缺失或重生会清除受击压力。所有跨 Tick 实体引用都是不可逆 alias，绝不保存 controller。

事件按 `event_id` 去重。资源耗尽或成功采集会淘汰旧资源观察；没有事件但旧资源格连续 2 个权威 Turn 可见且为空时也会淘汰观察并冷却 8 Tick，重新可见的资源会立即恢复观察。移动失败会读取上一计划保存的实际下一格，普通失败将其冷却 4 Tick，地形阻挡将其记录为永久障碍，并让探索、资源、复查和侦察任务清除当前目标、轮换扇区；寻路若在预算内无法证明可达，直接等待并重新规划，不再使用可能导致左右振荡的贪心相邻格兜底；资源/探索目标分配也会直接排除不可达目标。存入失败会清理对应 Unit 任务；Core 重生会清空旧 Unit 任务和旧资源观察。生产、治疗、射击、移动和重生结果全部进入事件计数，下一 Turn 的完整状态仍是事实来源。

只有服务器返回 `accepted=true` 后，`AgentRuntime.commit()` 才通过同目录临时文件与 `os.replace()` 原子保存下一版记忆。进程在提交前退出不会提前推进持久状态。

## 4. 导航预算与降级

每 Tick 总决策预算默认 500ms。导航使用永久障碍、仍在冷却的失败格、当前敌人格和可选威胁格执行有界 A*，单次搜索最多展开 1500 个节点。资源目标按最短路径成本唯一分配；探索目标按稳定的东、南、西、北扇区和持久任务分配，并在每个扇区内先筛选最多 32 个几何候选再计算路径成本。

如果到达截止时间或搜索未找到路径，Agent 直接等待并重新规划，不再使用贪心相邻格兜底。资源和探索目标在分配前会排除 `bounded_path_cost()` 无法证明可达的目标；移动失败后相关任务清除当前目标并轮换扇区，避免重复追逐同一局部阻塞点。统一校验器再次检查障碍、敌方占用、当前攻击目标和每格最多两个己方实体；被拒动作变为带原因的安全 `WAIT`。

## 5. 脱敏观测与离线指标

标准输出每 Tick 只有一行 JSON 摘要：Tick、模式、资源、人口、动作计数、等待原因、决策耗时、超时和提交结果。

成功提交后追加 `runtime/replay.jsonl`。回放包含当前可见状态、事件白名单数值、最终/拒绝意图和耗时；用户名被省略，UUID 使用不可逆短哈希别名，API Key、Cookie 和 Authorization 不进入结构。`replay_metrics(path)` 可统计 Tick 数、接受率、超时数、模式/动作分布、资源变化、最大人口、Core 缺失/受损 Tick、Beacon 持有 Tick，以及平均和最大决策耗时。截断的最后一行会被安全跳过。

## 6. 离线验证

```bash
source .venv/bin/activate
python3 -m compileall -q tactic.py arena_tactic tests
python3 -m pip check
python3 -m pytest -q
PYTHONPATH=.agents/skills/arena-hero python3 -m pytest -q .agents/skills/arena-hero/tests
python3 -m arena_tactic.canary runtime/replay.jsonl --min-ticks 500
git diff --check
```

最后一条仅做离线验证：它读取 `replay.jsonl` 及轮转历史，选取最新的最长连续 Tick 区间后两次执行完整 scheduler/BT/objective canary，检查动作签名确定性、零超时、零被拒 intent、p95 小于 500ms、最大耗时小于 900ms，以及连续 replay Tick 不少于阈值。未达到 Tick 数时输出 JSON 证据并以退出码 `2` 结束；它不会连接、提交或读取凭据。

测试覆盖模式切换与迟滞、治疗与生产预算、Ranger/Vanguard 评分、近卫/巡逻/猎人不同槽位、接近 Core 的临时拦截与近卫保留、失视/远离不触发历史拦截、Core 缺失/重生清理旧任务和敌情轨迹、视野 integer-supercover、Ranger 中间射击格、A* 绕障、四扇区探索、目标持久化、失败格冷却、地形障碍学习、资源唯一分配、旧资源连续复查失效与重新发现、无可见资源时的积极探索、两实体容量、UUID 决胜、当前目标校验、事件去重、资源枯竭与补充、连续移动失败、敌 Core 突现、Core 受损、Beacon 掉落、原子记忆、脱敏回放、确定性属性和 20 Unit 性能。

若实时 replay 正在追加，canary 前必须复制一个固定快照并对该快照运行两次；不能把同一可变 Turn/文件复用于确定性比较，也不能为通过 canary 弱化零超时、零拒绝和性能强断言。这些结果仅证明本地语法、依赖和代表性官方 SDK 状态的行为。它们不证明 API Key 有效、网络可达、服务器接受计划或真实策略收益。

## 7. 实时运行边界

前台调试只有用户明确要求时才运行：

```bash
source .venv/bin/activate
python3 tactic.py
```

24 小时运行使用项目根目录的 Docker Compose 服务：

```bash
docker compose up -d --build
curl http://127.0.0.1:8787/livez
curl http://127.0.0.1:8787/healthz
docker compose logs -f --tail=100 arena-hero
```

容器负责进程重启，`tactic.py` 负责 SDK 流关闭后的重连，`/livez` 表示
进程仍在运行，`/healthz` 表示最近已收到 Arena Hero 的 Turn。实时验证应
单独报告安装的 SDK 版本、收到的首个 Turn、提交结果和非敏感事件；不得输出
密钥、Cookie 或 Authorization Header。

# 行为树、命令与 canary 迁移状态

`LegacyPlannerAdapter` 仍可将最终动作投影为兼容 trace；默认 action source 仍是 legacy strategy。启用 `planner_canary=True` 时，runtime 不调用 `strategy.propose_intents`，而由当前 Turn 的 command、scheduler assignment、四类行为树、Beacon/Core 迁移/敌 Core objective 和统一 validation 形成完整计划。各功能均有可单独关闭的 `AgentConfig` 开关；`tactic.py` 仅在对应 `ARENA_HERO_*` 环境变量精确为 `1` 时打开，`ARENA_HERO_FULL_CANARY=1` 会显式打开完整新管线。关闭变量立即回退 legacy，不要求数据回退。

`runtime/decision-trace.jsonl` 与 `runtime/audit.jsonl` 通过有界异步 sink 追加脱敏 allowlist 字段；trace/audit 分别按 32 MiB×4、16 MiB×8 轮转，replay 保持 schema-v1 并按 64 MiB×4 轮转。Command API 的人工任务、策略、Core 迁移、紧急停机在当前 Tick 只进入队列，只有服务端 accepted 后才持久化；空白重启队列会从已提交的 memory 恢复 policy 读模型。

当前仓库的脱敏 replay 有 348 条记录，最长连续区间为 149 Tick；完整离线 canary 的性能与确定性检查均通过，但不满足 Phase 9 的至少 500 个代表性连续 Tick 门槛。因此不得删除 legacy 默认路径，也不能把离线结果表述为真实服务验证。完整事实型进度见 [`arena-hero-implementation-progress.md`](arena-hero-implementation-progress.md)。

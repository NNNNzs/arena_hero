# Arena Hero 战术手册（Playbook）

> 由巡检/复盘 Agent 维护：每次异常处置后追加案例；验证过的战术结论沉淀到知识库。
> 格式：`## YYYY-MM-DD Tickxxxxx | 异常类型` + 根因 / 处置 / 效果。

## 处置案例

### 2026-08-23 Tick 157039~157350 | DEFENSE_DISENGAGED (防守单位脱离交战) 与关联误报排查处置
- **现象**：哨兵在 Tick 157039~157050 检测到 `DEFENSE_DISENGAGED` (防守单位脱离交战)，并在长窗口观察到 `EXPLORATION_STALL` (迷雾探索停滞) 与 `PRODUCTION_FREEZE` (兵营生产冻结)。
- **根因分析**：
  1. `DEFENSE_DISENGAGED`：在 Tick 157039~157050，远征先锋 `13272e4e5024` 在距核心 72 格的外围区域发现敌军并主动交火（`SWEEP` 扫荡敌人）；核心防守先锋 `c662bfdd181c` 与游侠 `862f1f17d84e` 恪尽职守保持在核心环防位置（`holding_defense_ring`）。检测脚本 `tactical_inspector.py` 判定脱离交战时仅判断 `nearest > 6 and action == 'WAIT'`，未设置战术交火有效半径上限（敌军在 90 格外依然被要求出击），导致误将驻守核心判定为脱离交战。
  2. `EXPLORATION_STALL`：采矿工人 `9ee929ad2dac` 在核心与矿点之间循环往返运矿，300 Tick 内往返多次（步数 293 步），首尾坐标净位移仅 27 格。检测器仅根据位移/步数比率判定，未排除正在执行采集（`HARVEST`）与回矿存款（`DEPOSIT`）的正常生产工人。
  3. `PRODUCTION_FREEZE`：当前人口为 5（初期编制 2 工人 + 2 先锋 + 1 游侠已满编）。根据核心生产策略 `core_plan.py`，进入成熟期后受和平期缓冲储备（`peacetime_resource_buffer=40`）机制约束，Core 在人口 5 时上限为 25 资源，需存满 25 资源后才会启动下一轮成熟期造兵（游侠成本 12 + 储备）。在此期间资源（14~16）处于正常蓄水期，非系统卡死。
- **处置动作**：
  1. 修复 `scripts/tactical_inspector.py` 中 `DEFENSE_DISENGAGED` 启发式规则：限制交火威胁判定距离为本地战术半径（`6 < nearest <= 16`），防止核心环防单位因远方侦察遭遇战而误报。
  2. 修复 `scripts/tactical_inspector.py` 中 `EXPLORATION_STALL` 规则：过滤在时间窗口内有采集（`HARVEST`）或成功存款（`DEPOSIT_SUCCEEDED`）记录的正常往返采矿工人。
  3. 在 `tests/test_tactical_inspector.py` 中新增 3 组单元测试（包含远距离敌军守家不误报、近距离脱战正确告警、采矿工人往返不误报）。
- **效果验证**：
  - 自动化测试套件 337 个测试全绿通过（`pytest -q`）。
  - `python3 scripts/tactical_inspector.py --ticks 300` 再次运行，`DEFENSE_DISENGAGED` 与 `EXPLORATION_STALL` 误报均已彻底消除。
  - 在线运行时服务健康状态正常（`accepted=304+`, `rejected=0`, `reconnects=0`）。

### 2026-08-24 Tick 157522~157622 | INEFFECTIVE_STATIONARY (对象长期无效静止) Command API 干预处置
- **现象**：小时复盘在 300/120 Tick 窗口检出 `INEFFECTIVE_STATIONARY` (对象长期无效静止)：先锋 `13272e4e5024` 停在 `[-901,1567]`（距核心约 6 格），`stationary_ticks=32`、`blocked_waits=55`，但 `failed_moves=0`（无移动失败记录，属地形围堵型原地等待）。同窗口另有一次性 `PRODUCTION_FREEZE` (兵营生产冻结，Tick 157506)，后续窗口自愈未复发。
- **根因分析**：先锋被核心周边障碍物半包围，导航避障进入等待循环；策略自身的失败目的地冷却机制未触发（无 MOVE_FAILED 记录），属检测规则覆盖的盲区组合。
- **处置动作**：
  1. 通过 Command API (指令接口) 下发一次 `ASSIGN_TASK / MOVE_TO_CELL` 手动脱困指令至 `[-903,1565]`（priority=900）。
  2. 指令调用格式要点（本次踩坑实录）：body 必须嵌套为 `{"type":"ASSIGN_TASK","payload":{...}}`；鉴权需三件套——session cookie + `X-CSRF-Token` + `Origin: http://127.0.0.1:8787`；并发控制头 `If-Match` 必须带引号写成 `"command-version-N"`（裸写会被拒）；每次请求需唯一 `Idempotency-Key`。
- **效果验证**：指令在 Tick 157615 状态变为 `APPLIED`；随后 40 Tick 窗口复检异常数为 **0**，服务健康（accepted/rejected 正常，reconnects=0）。

### 2026-08-24 Tick 158650~158968 | INEFFECTIVE_STATIONARY 复发（双先锋互堵）Command API 干预闭环
- **现象**：先锋 `c662bfdd181c`（[-899,1573]，stationary_ticks=158）与 `13272e4e5024`（[-897,1574]，stationary_ticks=152、blocked_waits=268）相邻格长期静止，疑似互相阻塞；同窗口 BEACON 模式窗口内切换 16 次。
- **处置**：按知识库第 3 条流程对两单位分别下发 `ASSIGN_TASK / MOVE_TO_CELL`（priority=900，目标 [-903,1570] 与 [-894,1577]），Tick 158959 双双 `APPLIED`。**格式补充坑**：`MOVE_TO_CELL` 的目标字段名是 `target`（不是 `cell`）；并发头 `If-Match` 的 version 每成功一条指令自增，连发多条需逐条刷新。
- **效果验证**：60 Tick 复检异常数 **0**，工人恢复 `carrying` 状态，存款 2/0，服务健康。

### 2026-08-24 Tick 159579~159692 | 核心格对换死锁（swap deadlock）Command API 三步破局闭环
- **现象**：载货工人 `e11ac102e4d7` 在 `[-899,1573]`（距核心 1 格，cargo=1）连续 60+ Tick 无法入库（CARGO_DELIVERY_STAGNATION）；工人 `9ee929ad2dac` 恰好站在核心格 `[-898,1573]` 上，其唯一出口正是载货工人的所在格——两者互为路障，双双 WAIT（blocked_waits=60，failed_moves=0）。障碍勘测确认 `[-897,1573]`、`[-898,1572]`、`[-898,1574]` 均为障碍，形成单通道咽喉。
- **处置**（三步，逐条刷新 If-Match version）：
  1. 先给占核心格的工人下发 `MOVE_TO_CELL [-905,1566]`（priority=800），但它被载货工人堵住无法动——**先挪"想进核心"的一方更有效**；
  2. 给载货工人 `e11ac102e4d7` 下发 `MOVE_TO_CELL [-902,1573]`（priority=900），腾出咽喉格；
  3. 给堵在障碍墙后的先锋 `13272e4e5024`（beacon_route_blocked）下发绕行点 `[-894,1577]`。
- **效果验证**：Tick 159691 复检——载货工人完成入库（存款成功 1 次，核心资源 11→12）并转入 RECON；工人恢复 RETURN 移动；先锋抵达绕行点且 blocker=null。CRITICAL 异常清零。
- **经验**：核心区单通道地形下，"占位者出不去 + 进货者进不来"的对换死锁无法靠策略自身冷却自愈；干预顺序应为**先移开阻塞方（挡路的载货/守卫单位），再放行被堵方**。另注意：核心卫兵（LEGACY_CORE_GUARD）长期驻守哨位会触发 INEFFECTIVE_STATIONARY 误报（如 `c662bfdd181c` 在 `[-899,1573]` stationary_ticks=15、blocker=null），复检时先查 current_task 再定性。

### 2026-08-24 Tick 160032~160113 | INEFFECTIVE_STATIONARY 三度复发（同单位同咽喉位）Command API 干预闭环
- **现象**：先锋 `13272e4e5024` 在 `[-897,1574]`（核心单通道咽喉）第三次卡死，`stationary_ticks=30+`、`blocked_waits=30`、`failed_moves=0`，任务为 beacon（信标远征）目标 `[-231,-306]`。同窗口 `c662bfdd181c` 为核心卫兵驻哨误报（知识库第 5 条），未处置。
- **处置**：按标准流程下发 `ASSIGN_TASK / MOVE_TO_CELL [-894,1577]`（priority=900），Tick 160109 `APPLIED`。
- **新坑实录**：payload 字段名是 `task_kind`（不是 `task`），写错返回 `INVALID_TASK: task_kind is not supported`；支持的手动任务集合见 `arena_tactic/command_center.py:_MANUAL_TASKS`（RETREAT_TO_CORE / HOLD_POSITION / HARVEST_VISIBLE / MOVE_TO_CELL）。
- **效果验证**：40 Tick 复检——该先锋脱离静止列表；期间一次瞬时 `CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 于 Tick 160113 自愈（工人恢复 carrying/MOVE）；仅剩卫兵驻哨已知误报。

## 战术知识库

1. **核心环防与远征接敌解耦准则**：
   - 侦察护卫单位在外围遭遇敌人时，核心驻守环防部队（`holding_defense_ring`）不应盲目弃守核心跨半图支援，避免核心空虚被偷家。
   - 告警检测器需以局部战术距离（16 格内）评估防守单位是否脱战。
2. **往返采矿与探索停滞区分准则**：
   - 工人往返采矿虽然全局净位移接近 0，但有持续的 `HARVEST`/`DEPOSIT_SUCCEEDED` 行为，必须与在迷雾中绕圈卡死的探索工人区分。
3. **Command API 应急干预标准流程**：
   - 适用场景：warning 级别且策略自身冷却机制无法自愈的原地卡死（有 `blocked_waits` 但 `failed_moves=0` 的地形围堵型静止）。
   - 流程：登录取 CSRF → POST `/api/v1/commands`（嵌套 payload + 引号版 If-Match + 幂等键）→ 等 2~5 Tick 后查 `/api/v1/entities/<alias>` 确认 `APPLIED` → 用 tactical_inspector 复检窗口清零才算闭环。

# Arena Hero 战术手册（Playbook）

> 由巡检/复盘 Agent 维护：每次异常处置后追加案例；验证过的战术结论沉淀到知识库。
> 格式：`## YYYY-MM-DD Tickxxxxx | 异常类型` + 根因 / 处置 / 效果。

## 处置案例

### 2026-09-05 Tick 225835~225841 | SQUAD_EXPEDITION_STALL (信标打击群协同停滞) 与工兵采矿路径阻塞修复
- **现象**：在 120 Tick 窗口巡检检出 `[CRITICAL] SQUAD_EXPEDITION_STALL (信标打击群协同停滞)` 与 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。16 名远征先锋在 2000 格外（如 `[-2800, 792]` 区域）长期停滞，reason 持续为 `mineral_tank_route_blocked`；同时基地 12 名工兵全员卡在 `resource_route_blocked` 或 `no_resource_or_frontier`。
- **根因分析**：
  1. `vanguards.py` 中的 `best_mineral_tank_cell`（基地矿区肉盾卡位）逻辑在检查 `vanguard.id in expedition_vanguards` 之前无条件执行，且没有距离守护。深入地图前线的远征先锋被错误判定接取基地矿区卡位，超远距离寻路失败后陷入 `mineral_tank_route_blocked` 原地永久 WAIT。
  2. 工兵在首选矿点路径暂时被挡时，缺少针对其他可见矿点的平滑降级，导致工兵全员原地等待。
- **处置动作**：
  1. 修改 `arena_tactic/strategy/vanguards.py`：为基地矿区肉盾卡位增加编制守护与距离门限（`vanguard.id not in expedition_vanguards and distance(vanguard.position, context.core.position) <= config.defense_exit_distance`），严禁远征军先锋与基地外先锋接取基地矿点卡位。
  2. 修改 `arena_tactic/strategy/workers.py`：工兵首选矿点寻路受阻时，尝试选择其他未被锁定的可见资源格（`resource_route_alt_cell`），打破路径阻塞僵局。
  3. 新增单元测试 `tests/test_expedition_mineral_tank_guard.py`（包含 6 个测试用例，全绿通过）。
- **效果验证**：
  - 单测全绿通过（`pytest tests/test_expedition_mineral_tank_guard.py` 6 passed）。
  - 热重启服务并验证 health endpoint 正常。

### 2026-09-04 Tick 219268~219277 | UNIT_OSCILLATION (单位严重振荡) Command API 干预处置闭环
- **现象**：在 120 Tick 窗口巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[CRITICAL] SQUAD_EXPEDITION_STALL (信标打击群协同停滞)`。工兵 `entity_4de5d0e9593a` 在核心门户 `[-900, 1570]` 与 `[-900, 1569]` 之间以周期 2 往复振荡 28 次（样本 30 次）。
- **根因分析**：工兵处于核心门口退让半径内（`cargo_delivery_yield_radius`），因基地外围被多名待命工兵（`[-900, 1568]`, `[-901, 1569]` 等）与障碍物阻隔，`_evacuate_doorstep_intent` 候选格子排序在内外两格之间来回翻转，引发周期性摆动。
- **处置动作**：
  1. 通过 Command API 登录获取 session 并提取 `csrf_token`。
  2. 下发 `ASSIGN_TASK` 指令：`entity_alias: "entity_4de5d0e9593a"`, `task_kind: "HOLD_POSITION"`, `priority: 800`，配合标头 `X-CSRF-Token`、`Origin: http://127.0.0.1:8787` 与 `If-Match: "command-version-0"`。
  3. 指令生成 ID `cmd_00000001_c27b1ec9`，在 Tick 219274 状态转为 `APPLIED`。
- **效果验证**：
  - 工兵在 Tick 219274 准时转入 `manual_hold_position` (等待)，摆荡立即停止。
  - Tick 219277 重新运行 `tactical_inspector.py --ticks 10`，`UNIT_OSCILLATION`、`SQUAD_EXPEDITION_STALL`、`DECISION_LATENCY_SPIKE` 全量消除，仅余常规防守静止。

### 2026-09-02 Tick 208280+ | CARGO_DELIVERY_STAGNATION (载货工人回矿停滞) / SQUAD_EXPEDITION_STALL (信标远征编队停滞) 工程修复
- **现象**：核心位于单通道口袋 `[-898, 1573]`；核心格被 CORE 与空载工人占满，唯一西向出口被两名载货工人占满，后方载货队列持续 `no_safe_route_with_cargo` (载货无安全回矿路径)。同时，Beacon expedition (信标远征打击群) 前锋与基地新兵相隔数千格，前锋被 `expedition_cohesion_hold` (远征编队凝聚等待) 长期冻结。
- **根因分析**：工人排序把距核心一格的载货工人排在核心格空载工人前，导致 Swap Deadlock (对换死锁)。预约表只会拒绝满格入口，旧逻辑没有为满载的单通道建立可解析的离开依赖。远征协调则把 extreme split (极端分裂，成员间距超过门限) 当作普通 regroup (重新集结)，错误地让前锋也进入等待。
- **处置动作**：
  1. 将 `vacate_core_cell_for_delivery` (为运矿腾退核心格) 提升为最高工人规划优先级；出口已有一名友军时，继续利用每格 2 个单位的合法容量预约。
  2. 新增有界的 `yield_delivery_corridor_congestion` (运矿走廊拥堵退让) 链：从最外层载货工开始向远离核心或侧向的安全格退避，逐层登记 departure (离开预约)，随后让核心格空载工进入刚释放的咽喉格；满格无可行出口时仍安全 WAIT (等待)，不虚构移动。
  3. extreme split (极端分裂) 时，前锋继续向 Beacon objective (信标目标) 推进或前沿警戒；后方成员以独立槽位全速向前锋集结，不再触发全队 `expedition_cohesion_hold` (远征编队凝聚等待)。
  4. 将 `EXPEDITION_BEACON` (信标远征打击群) 的专有先锋/游侠编制作为硬上限，reserve (预备队) 留在 `BASE_DEFENSE` (基地防御编队)，不再无上限灌入远征队。
- **回归验证**：新增/更新单通道口袋、门口满载退让和极端分裂前锋推进测试；执行 `pytest tests/ -q` 全量验证后提交。

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

### 2026-08-24 Tick 160291~160460 | 核心区多单位容量死锁（第 4 次咽喉位卡死，含 hp=1 安全抢占）Command API 分步干预闭环
- **现象**：核心格 `[-898,1573]` 被游侠 `1b1da070a55a` 占据（与 CORE 同格 2/2 满），其唯一西出口 `[-899,1573]` 也被游侠 `28f6d638896a`（hp=1，`critical_retreat_blocked` 想回核但核心格满）+ 卫兵 `c662bfdd181c` 占满（2/2）。载货工人 `d93eabd6cdc0`/`9ee929ad2dac` 双双 `no_safe_route_with_cargo`，触发 `CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` CRITICAL。
- **关键机理**：
  1. 引擎每格容量为 2（ReservationTable），诊断时必须逐格统计占用数，而不是只看"有没有人"；
  2. `_manual_safety_preempts` 对 hp≤1 单位直接抢占手动任务——hp=1 游侠无法用 Command API 移动，只能靠先腾出它的目标格让它自己走；
  3. 手动 `MOVE_TO_CELL` 到达后单位会停在目标格 WAIT（`manual_target_reached`），若目标格选在咽喉位会形成新的堵点。
- **处置**（顺序敏感）：① 移开唯一可动占位者卫兵 `c662bfdd181c` → `MOVE_TO_CELL [-900,1577]`（APPLIED Tick 160428）；② 游侠 `1b1da070a55a` 立即借空位脱出核心格；③ hp=1 游侠 `28f6d638896a` 自动回核进入 HEAL（`damaged_at_stationary_heal`）；④ 工人恢复入库（核心资源 6→8，`CARGO_DELIVERY_STAGNATION` 清零）；⑤ 收尾把手动停靠的单位取消任务/挪离咽喉位，避免二次堵塞。
- **遗留**：先锋 `13272e4e5024` 的 `beacon_route_blocked (信标路线受阻)` 第 4 次复发，目标 `[-282,-308]` 距离约 600 格，已于 Tick 161280 升级分段航点导航彻底解决（见下文案例）。

### 2026-08-24 Tick 161175~161281 | 远距离 A* 节点预算不足与超远距离信标分段航点导航闭环 (CARGO_DELIVERY_STAGNATION / INEFFECTIVE_STATIONARY)
- **现象**：
  1. 载货工人 `e945b3c756a7` 在 `[-846, 1618]`（距核心 97 格，cargo=1）连续 60+ Tick 无法规划回核路径（`no_safe_route_with_cargo`，触发 `CARGO_DELIVERY_STAGNATION` CRITICAL）；
  2. 先锋 `13272e4e5024` 在 `[-894, 1577]` 持续 60+ Tick `beacon_route_blocked`（信标距离 2239 格，触发 `INEFFECTIVE_STATIONARY` WARNING）。
- **根因分析**：
  1. `arena_tactic/navigation.py:plan_step` 中 `node_limit` 原公式为 `max(config.astar_node_limit, min(4 * distance(start, goal), 3000))`，当距离为 97 格时 `4 * 97 = 388` 节点，在大规模障碍物地图中严重不足（实测需 ~2500 节点），导致 A* 提前耗尽预算误判为无路。
  2. 对跨图超远距离目标（如 2200+ 格外的地面信标），单一全图 `bounded_astar` 因狭长包围盒（±12）与节点预算限制必然失败，导致远征单位永远停在核心咽喉处 `WAIT`。
- **处置动作**：
  1. 调整 A* 节点伸缩系数：`node_limit=max(config.astar_node_limit, min(40 * distance(start, goal), 4000))`，长途路径分配充足搜索预算（实测 97 格耗时仅 7ms）。
  2. 引入分段航点回退（waypoint fallback）：当目标距离 > 30 格且直达 A* 失败时，沿方向向量投影局部航点（`step_dist = min(25, max(10, dist // 4))`），实现局部避障流式推进。
  3. 新增单测 `test_plan_step_waypoint_fallback_for_distant_goal`（全量 351 测试绿灯）。
- **效果验证**：
  - 重启热加载后，载货工人 `e945b3c756a7` 立即恢复 `return_cargo_to_core` 移动（`[-846, 1618]` → `[-850, 1617]`）；
  - 先锋 `13272e4e5024` 立即摆脱 `beacon_route_blocked`，执行航点推进（`[-894, 1577]` → `[-893, 1573]`）；
  - `tactical_inspector` 60 Tick 窗口异常数归 **0**。

### 2026-08-24 Tick 161630~161688 | 先锋横扫技能 (SWEEP) 场景下 UNANSWERED_DAMAGE 误报排查与判定闭环
- **现象**：战术巡检器在 Tick 161631 触发 CRITICAL 级别 `UNANSWERED_DAMAGE` (单位受击后无反击或规避) 告警（先锋 `e7e2aa8f7976` HP 从 4 下降至 2）。
- **根因分析**：
  先锋在外围执行近战扫荡（`SWEEP`），连续受损时一直在使用 `SWEEP` 反击敌人，且在 HP 降至 1 时主动规避脱离射程并成功安全撤回核心。巡检器 `tactical_inspector.py` 的反击判定仅包含了 `{"ATTACK", "SHOOT"}`，遗漏了先锋的范围攻击动作 `SWEEP`，导致将正在近战反击的先锋误判为“受击未反击”。
- **处置动作**：
  1. 修复 `scripts/tactical_inspector.py` 中 `UNANSWERED_DAMAGE` 判定逻辑，将反击动作集扩充为 `{"ATTACK", "SHOOT", "SWEEP"}`。
  2. 在 `tests/test_tactical_inspector.py` 中新增 `test_vanguard_sweep_does_not_trigger_unanswered_damage` 单元测试。
- **效果验证**：
  - 352 个自动化测试全量通过（`pytest -q`）。
  - `scripts/tactical_inspector.py` 60 Tick 与 300 Tick 窗口复检异常数彻底清零（0 项）。
  - 实时战局中先锋成功完成交战并撤退至核心安全区，核心资源增长至 19/45，人口 9，运行正常。

### 2026-08-25 Tick 165380~165572 | 远征侦察敌巢纵深态势探明与前沿接敌动态转入 ATTACK 进攻协同
- **现象**：
  1. 远征先锋 `bdb20f4ff8ab` 在距核心 320 格外围区域 `[-1109, 1330]` 遭遇密集敌军单位（连续侦获 `ca680020d214`、`3b3995aee19e`、`b8701b413aa0`、`e85e1b0414a0`、`07d4491eb9e5`、`a2c6dad989e1`、`5dc3dc75ce19` 等 7+ 敌方单位），探明敌军主巢位置。该先锋在 Tick 165459~165461 遭遇集火攻击并于 Tick 165465 战损阵亡（触发一次性 `UNANSWERED_DAMAGE` (单位受击后无反击或规避) 告警，随后自然闭环）。
  2. 在 Tick 165570，前沿先锋 `589ba5257e5e` 于 `[-920, 1491]`（距核心 86 格）发现并使用 `SWEEP` (横扫攻击) 交战敌军；全局策略瞬间由 `BEACON (信标模式)` 动态切换为 `ATTACK (进攻模式)`。
  3. 基地内原本驻守防卫环的 6+ 名游侠（`862f1f17d84e`、`2b635beeb02b`、`f0bb9af72361`、`3e74a980cae0`、`f85683b782aa`、`1b1da070a55a`、`91153bb783b0`）迅速响应，执行 `ranger_seek_legal_firing_line` 前出向 `[-918, 1492]` 建立射击阵位，形成梯队协同集火。
- **根因与机制分析**：
  1. 远征侦察单位孤军深入敌巢深处，因寡不敌众战损属预期外围探路代价，成功换取了敌军主力据点的精确宏观坐标；
  2. 前沿单位遭遇敌情触发策略自适应模式切换（`BEACON` → `ATTACK`），战备游侠集群自动从环防等待状态转入追击协同（`INEFFECTIVE_STATIONARY` 静止单位数迅速减半），展现了良好的战场自愈与战术响应能力。
- **处置动作**：
  1. 战局处于良性自适应运作状态，核心 0 损伤、防线完整、经济与运矿持续运转，无需 Command API 强行人工插手。
  2. 记录敌巢坐标 `[-1109, 1330]` 作为后续战役推演情报。
- **效果验证**：
  - 核心状态极佳：HP 5/5, Shield 5/5, 资源 14/75, 人口 15/15 满编。
  - 运矿工人 `d93eabd6cdc0` 顺利返回核心，`CARGO_DELIVERY_STAGNATION` 瞬时告警自动清零。
  - 测试套件全量 357 项全部通过（`pytest -q`）。

### 2026-08-25 Tick 165790~165851 | 核心单通道咽喉群聚阻塞与入库对换死锁 (CARGO_DELIVERY_STAGNATION / INEFFECTIVE_STATIONARY) Command API 分层疏导闭环
- **现象**：
  1. 巡检器在 60 Tick 窗口触发 CRITICAL 级别 `CARGO_DELIVERY_STAGNATION` (载货工人回矿停滞) 告警（工人 `e945b3c756a7` 载货 1 格在 `[-901, 1573]` 连续 60+ Tick 无法入库，`no_safe_route_with_cargo`）；
  2. 同窗口检出 9~10 个战斗/后勤单位陷入 `INEFFECTIVE_STATIONARY` (对象长期无效静止)，核心周边多个单格容量饱和（2/2 满员）；
  3. 核心格 `[-898, 1573]` 内驻留工人 `0c76b77ebb1d`（后为新生工人 `ccb8f0cdccc0`）持续遭遇 `core_cell_vacate_blocked` (核心格腾退受阻)。
- **根因分析**：
  1. **地形咽喉约束**：核心 `[-898, 1573]` 北（`[-898, 1572]`）、东（`[-897, 1573]`）、南（`[-898, 1574]`）均为不可通行的障碍墙，全图通向核心的唯一出入口为西侧狭窄通道 `[-899, 1573]`。
  2. **容量与寻路耦合死锁**：西侧通道 `[-899, 1573]`、`[-900, 1573]` 被核心守卫（Guard）与巡逻/猎手先锋游侠占满（每格 2 单位容量饱和）。`bounded_astar` 寻路时仅将静态障碍和敌军计入 `blocked`，未预先考虑友军满载格；当规划出直线穿行路径后，第一步因 `ReservationTable` 满员而被拒（`plan_step` 返回 None，单位 WAIT），导致巡逻与猎手单位在咽喉处堆叠等待，进而将出核的工人与进核的载货工人两端堵死形成对换死锁（Swap Deadlock）。
- **处置动作（Command API 四步梯次疏导）**：
  1. **疏散走廊外围占位**：对卡在走廊内的 `entity_45082e9dab8f` (先锋)、`entity_cd010232459f` (先锋)、`entity_91153bb783b0` (游侠) 下发 `ASSIGN_TASK / MOVE_TO_CELL` 指令，就近移动 1 步至北侧开阔安全格 `[-899, 1572]` 与 `[-900, 1572]`；
  2. **移开咽喉守卫**：对驻守在核心唯一出入口 `[-899, 1573]` 的先锋守卫 `entity_31a0652ce1ae` 下发 `MOVE_TO_CELL [-899, 1572]`，彻底打通核心西向单行道；
  3. **腾挪入库顺位**：对抢入走廊的第二名载货工人 `entity_d93eabd6cdc0` 下发 `MOVE_TO_CELL [-900, 1573]` 稍作退避，使 Core 内新生工人 `entity_ccb8f0cdccc0` 得以成功出核（`vacate_core_cell_for_delivery`），载货工人 `entity_e945b3c756a7` 顺畅进核完成 `DEPOSIT` (资源入库)；
  4. **注销手动任务并全面放行**：下发 `CANCEL` 指令清除所有单位的手动任务，各战斗单位（游侠集群、先锋编队）与工人全线恢复自主探索与巡逻。
- **效果验证**：
  - 载货工人 `e945b3c756a7` 与 `d93eabd6cdc0` 连续完成 2 次入库（`deposit 2/0`），核心资源增长至 9/90，人口扩充至 18；
  - 探索工人 `0c76b77ebb1d` 顺利突进前沿并锁定新矿区（`move_to_unique_resource`）；
  - `tactical_inspector` 60 Tick 窗口异常数**彻底清零（0 项）**，300 Tick 窗口仅剩正常驻哨守卫，系统进入良性循环。

### 2026-08-25 Tick 165940~166030 | 西向外围前沿多游侠同步齐射火力网（Multi-Ranger Synchronized Salvo / 集火压制）与态势稳定
- **现象**：
  1. 巡检器在 300 Tick 窗口显示游侠 `17a19d5279e8` 存在短期静止记录（驻守在西翼前沿 `[-933, 1600]`，距核心 62 格），在 60/120 Tick 窗口已自愈（异常数 0）；
  2. 战局西侧外围前沿（`[-924..-937, 1592..1623]`）出现 5 名敌方散兵试探接近。战术引擎自适应在 `BEACON (信标模式)` 与 `ATTACK (进攻模式)` 间无缝切换；
  3. 部署在外围阵位的 4 名游侠（`f0bb9af72361`、`f85683b782aa`、`17a19d5279e8`、`91153bb783b0`）在 Tick 166004 同步发起集火齐射，窗口内达成 17 次有效命中（`SHOT_HIT`）。
- **根因与机制分析**：
  1. 游侠 `17a19d5279e8` 处于前沿伏击阵位（`seeking_legal_firing_line`），在敌军进入合法射程后即时启动交火，非系统卡死；
  2. 前沿游侠集群采用基于局部视距的最优目标打分机制（`highest_scoring_legal_ranger_target`），成功实现分散站位下的多目标同步齐射压制，将敌军逼退至核心 60+ 格以外。
- **处置动作**：
  1. 系统运行极其稳定，异常判定 0 项，核心 0 损耗（HP 5/5, Shield 5/5），运矿入库持续顺畅（5 工人 2 运 3 探，存款 5/0），无需人工 Command API 强行干预。
  2. 修复 `tests/test_strategy_regressions.py` 中和平期造兵单测中因 mid_workers 经济扩产特性引起的预期断言，确保全量测试 358 项全绿。
- **效果验证**：
  - 核心资源 14/95，人口规模达 19 满编；
  - 自动化测试套件全量 358 个测试 100% 通过（`pytest -q`）；
  - 实时战局 60/120 Tick 窗口异常数全为 0。

### 2026-08-26 口袋单通道回矿死锁与出核让位修复
- **现象**：核心位于 `(-898, 1573)`，北/东/南三面全为障碍物，西侧 `(-899, 1573)` 为唯一进出门禁。核心格被 CORE + 空载工占满（2/2），门口格被 2 名满载工占满（2/2）。系统持续 7+ 小时报 `CARGO_DELIVERY_STAGNATION`，基地资源停滞在 23~28/90。
- **根因分析**：
  1. `ReservationTable` 缺少出发格占用扣减（`departures`），导致流水线/对换移动被保守容量拒绝；
  2. `workers.py` 中 `vacate` 让位逻辑在局部用静态 `friendly_occupancy` 重新覆盖了 `reservations` 表，遮蔽了门口工人已决定的侧移避让；
  3. 远距离满载工人决策顺序早于核心空格上的空载工人，提前抢占了门禁格，导致核心内空载工永远被困在核心格。
- **处置与效果**：
  1. 在 `ReservationTable` 引入 `departures` 动态跟踪，支持端到端流水线容量判定（`reserve(dst, source=src)`）；
  2. 移除 `workers.py` 中 `vacate` 的局部覆盖，继承全局动态预定表；
  3. 优化工人决策顺序：门口就绪满载工 -> 核心空格空载工（最高让位优先级） -> 远距离满载工 -> 探索工；
  4. 369 项全量单测通过，离线回放验证空载工成功移出核心，门口满载工顺利接接入库。

### 2026-08-26 战术编组体系构建与信标远征编组重构
- **现象**：在 `BEACON` 模式下，原策略硬编码 `min(vanguards, key=distance)` 仅派遣单兵先锋跨越 2000+ 格长途奔袭，缺乏游侠射程火力掩护，单兵深入极易战损且大部队在基地闲置。
- **重构与落地**：
  1. 建立 Squad 编组模型 (`arena_tactic/squads.py`)：定义 `EXPEDITION_BEACON` (远征夺旗)、`BASE_DEFENSE` (基地防守)、`MINING_ESCORT` (采矿护航) 与 `SCOUT_RECON` (迷雾探索) 4 大编组；
  2. 重构先锋与游侠调度逻辑：`BEACON` 模式下由机动先锋梯队与机动游侠协同组成远征打击集群（Strike Team），游侠在先锋后方 2~3 格提供伴随火力支援与视野掩护，同时基地保留守备编制；
  3. 扩展巡检分析工具 (`scripts/tactical_inspector.py`)：在态势报告中增加【战术编制与编组态势】区块；
  4. 369 项全量测试全部通过。

### 2026-08-26 Tick 171809~171936 | CARGO_DELIVERY_STAGNATION 核心格游侠占用与 Command API 疏导干预
- **现象**：整点深度战术巡检，时间窗 120 Ticks（Tick 171809~171936）。系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，总人口 23，核心资源 48/115。检测到 `CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` [CRITICAL] 涉及 6 名载货工人，以及 2 名游侠 `INEFFECTIVE_STATIONARY (对象长期无效静止)`。
- **战况总结**：
  - 核心状态健康，HP 5/5，护盾 5/5，窗口内 0 次受击、0 次隐蔽受击、0 核心重生；
  - 战术编制：信标远征大队 (`EXPEDITION_BEACON`) 先锋与游侠协同向西南信标 `[-1141, -308]` 挺进（先头梯队抵达 `[-1111..-1120, 894..1087]`）；基地防御防线 (`BASE_DEFENSE`) 卡位环防哨位；
  - 根因分析：远征支援游侠 `f66ef66baafa` 恰好停留在核心所在格 `[-898, 1573]`，导致 6 名载货工人因回矿入库格与正向通道占用（`yield_doorstep_congestion` / `no_safe_route_with_cargo`）堵在核心门前 1~3 格内无法入库；
  - 处置：通过 Command API 下发 `ASSIGN_TASK / MOVE_TO_CELL` 将核心格游侠 `entity_f66ef66baafa` 调离至侧翼 `[-894, 1577]`，指令成功于 Tick 171943 生效执行（`status: APPLIED`），释放核心入库格。同时生成全景 Markdown 战报并成功发送 HTML 邮件至 709934831@qq.com 归档。
### 2026-09-02 | Hot Tier (热数据层) 7-day retention and Cold Tier (冷数据层) deletion guard
- Replay and decision trace now retain **current + 11 history volumes** by default (12 volumes; replay budget about 768 MiB). Set `ARENA_HERO_HISTORY_FILES` only when an operator intentionally changes that window.
- Before a rotated volume is discarded, `AsyncSupabaseWriter` runs a background JSONL backfill and confirms every Tick has been accepted by Supabase. While that is pending or unavailable, rotation retains the oldest volume and appends locally; the Tick loop never waits for the network.
- Run `python scripts/prune_and_sync_history.py --dry-run --days 7` for the lifecycle audit, then omit `--dry-run` to reconcile missing replay/trace Ticks and delete only safe, expired rotated volumes. `protected-unsynced` means no deletion occurred / `未同步受保护` 表示未删除。

### 2026-09-03 | Tick 216998 载货工人在核心门前让路与侧滑回矿死锁振荡修复
- **现象**：系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 91/200。巡检检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及 5 名工人，`[WARNING] UNIT_OSCILLATION (单位往返振荡)` 涉及 4 个对象。入库成功数持续为 0。
- **根因分析**：
  在 `arena_tactic/strategy/workers.py` 中，当工人持有 cargo 到达核心门前（distance == 1）时，若该格有 2 名友军重叠，旧逻辑无条件强制排序在前的工人执行 `yield_doorstep_congestion` 向外侧避让 1 格（距离变为 2）。而在下一 tick，由于距离变为 2，触发了 `_return_to_core_sidestep` 重新向核心靠拢前进 1 格回到门口。两套逻辑形成 2 格高频往返振荡（Ping-Pong Oscillation）死锁，导致满载工人即便在核心格为空的情况下也反复在门前往返振荡，无法踏入核心格完成 `DEPOSIT`。
- **处置动作**：
  1. 移除 `workers.py` 中载货工人在门前（distance == 1）因同格重叠而盲目向外让路的 `yield_doorstep_congestion` 侧移分支；载货工人在核心门口优先预约核心格入库，若核心格暂时不可用则保持就地等待（`cargo_doorstep_wait_for_entry`），彻底根除往返振荡死锁。
  2. 在 `tests/test_core_congestion.py` 中增加 `test_doorstep_cargo_workers_enter_empty_core_without_sidestep_oscillation` 回归测试用例。
  3. 全量单元测试 421 项 100% 通过（`pytest tests/ -q`），按 `auto-commit` 规范提交。
  4. 重启 Docker 容器加载最新战术逻辑生效。

### 2026-09-05 | Tick 225160 游侠远距离撤退多格环形死循环振荡（UNIT_OSCILLATION）与门前避障重构
- **现象**：巡检时间窗 Tick 225035..225155，系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 97/200。检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`，游侠 `5c604f8a2a33` 在 `[-2598, 1176]` 附近陷入 4-Tick 极度规则的周期性环形死锁振荡（60 步内反向 29 次）：`[-2597, 1176] -> LEFT -> [-2598, 1176] -> DOWN -> [-2598, 1177] -> UP -> [-2598, 1176] -> RIGHT -> [-2597, 1176]`，任务类型为 `critical_ranger_retreat_distant_fallback`。\n- **根因分析**：
  在 `arena_tactic/strategy/common.py` 中，`_distant_retreat_fallback_intent` 原仅记录单步 `prev_cell` 进行防回退排序（`1 if previous_cell is not None and item[0] == previous_cell else 0`）。当单位在遇到复杂障碍物凹陷时，在 3~4 格形成的局部环路中循环往复，因为当其踏入第 3 格时，第 1 格已不在 `prev_cell` 中，贪心距离评估与防回退机制失效，导致反复在 3~4 格形成的环路上无限往返。
- **处置动作**：
  1. 将单步 `prev_cell` 升级为记录最近走过的坐标序列 `recent_cells`（保留最近 5 格），引入多层级 Taboo 禁忌惩罚（越近访问的坐标惩罚权重越高，最高 600 并逐级减半），彻底打破 3~4 格的局部环形振荡；
  2. 重构核心门前避让 `_evacuate_doorstep_intent` 与受损回撤 `_critical_retreat_sidestep`，加入防反向回溯禁忌门限；
  3. 新增 `tests/test_distant_retreat_oscillation.py` 覆盖 9 项多格回环振荡与禁忌队列回归用例，扩充 `tests/test_core_congestion.py` 与 `tests/test_tactical_inspector.py`；
  4. 全量单元测试 437 项 100% 通过（`pytest tests/ -q`），按 auto-commit 规范提交；
  5. 报警 HTML 邮件成功发送至 709934831@qq.com 归档；
  6. 重启 Docker 容器加载最新战术逻辑生效。

### 2026-09-05 | Tick 226521 超远距离守备单位 guard_route_blocked 路径死锁与增量回撤修复
- **现象**：巡检时间窗 Tick 226398..226521，系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 97/200。巡检检出 `[CRITICAL] SQUAD_EXPEDITION_STALL (信标打击群协同停滞)` 涉及 12 名远征队成员，`[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)` 涉及 24 个对象（占全军 60%）。
- **根因分析**：
  在 `arena_tactic/strategy/vanguards.py` 与 `arena_tactic/strategy/rangers.py` 中，约 10 名被分配为核心守备（`LEGACY_CORE_GUARD`）的先锋和游侠（如 `28f6d638896a`, `986c96c1179e`, `3ac3cba7cab8`, `c662bfdd181c`, `45082e9dab8f`, `7c7c18221303` 等），实际坐标分散在远达 2000 多格之外的旧战区（`[-2800, 700]` 区域）。策略层直接对其调用 `_move(unit, guard_target, "hold_core_defense_ring", 300, ...)`。由于跨越 2000+ 格的迷雾和复杂障碍物，单次 A* 寻路失败返回 None，且单位不处于 doorstep 区域，导致每回合全部 fallback 到 `_wait(unit, "guard_route_blocked")`。这导致 10 名战斗主力在远方永久死锁卡死，且长距离无效寻路计算导致决策耗时激增至 1220ms。
- **处置动作**：
  1. 在 `vanguards.py` 与 `rangers.py` 中引入超远距离守备单位增量回撤降级（Long-distance Guard Fallback）：当守备单位因路径阻断或距离过远寻路失败且距 `guard_target` 超过长途门限时，调用 `_distant_retreat_fallback_intent` 进行单步增量贪心逼近，穿越迷雾向核心方向移动，彻底打破原地 WAIT 死锁；
  2. 新增 `tests/test_guard_route_fallback.py` 覆盖 6 项超远距离守备先锋与游侠解卡单测用例；
  3. 全量测试通过，按 auto-commit 规范提交；
  4. 报警 HTML 邮件成功发送至 709934831@qq.com 归档；
  5. 重启 Docker 容器加载最新战术逻辑生效。


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

## 战术知识库

1. **核心环防与远征接敌解耦准则**：
   - 侦察护卫单位在外围遭遇敌人时，核心驻守环防部队（`holding_defense_ring`）不应盲目弃守核心跨半图支援，避免核心空虚被偷家。
   - 告警检测器需以局部战术距离（16 格内）评估防守单位是否脱战。
2. **往返采矿与探索停滞区分准则**：
   - 工人往返采矿虽然全局净位移接近 0，但有持续的 `HARVEST`/`DEPOSIT_SUCCEEDED` 行为，必须与在迷雾中绕圈卡死的探索工人区分。
3. **Command API 应急干预标准流程**：
   - 适用场景：warning 级别且策略自身冷却机制无法自愈的原地卡死（有 `blocked_waits` 但 `failed_moves=0` 的地形围堵型静止）。
   - 流程：登录取 CSRF → POST `/api/v1/commands`（嵌套 payload + 引号版 If-Match + 幂等键）→ 等 2~5 Tick 后查 `/api/v1/entities/<alias>` 确认 `APPLIED` → 用 tactical_inspector 复检窗口清零才算闭环。

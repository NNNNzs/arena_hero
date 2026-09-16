# Arena Hero 战术手册（Playbook）

> 由巡检/复盘 Agent 维护：每次异常处置后追加案例；验证过的战术结论沉淀到知识库。
> 格式：`## YYYY-MM-DD Tickxxxxx | 异常类型` + 根因 / 处置 / 效果。

## 处置案例

### 2026-09-16 Tick 286740~286761 | UNIT_OSCILLATION (工兵前沿探索遇阻与敌军夹峙 24次两格往返振荡) Command API MOVE_TO_CELL 应急脱困
- **现象**：巡检在 Tick 286621..286740 检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。工兵 `entity_4d8c2bf83b23` (WORKER, 载货 0) 坐标在 `[-751, -608]` 与 `[-751, -607]` 间 120 回合内往返反转 24 次（采样 32 次），执行 `explore_sector_frontier`（目标 `[-748, -613]`）陷入 2 格摆钟往复死循环。
- **根因分析**：
  1. 工兵目标点位于东南扇区 `[-748, -613]`，正前方存在横向连片障碍群（`[-750, -609]`、`[-749, -609]` 等）。
  2. 隘口外围聚集 4 名敌军（`[-749, -607]`、`[-751, -605]`、`[-752, -608]`、`[-748, -608]`）。
  3. `_move` 寻路在向目标贪心推进与规避周边多点威胁之间频繁摇摆，在 `[-751, -607]` 与 `[-751, -608]` 形成 2 格死循环。
  4. 此外，周边敌军距离 ≤2 会使 `RETREAT_TO_CORE` 触发 `_manual_safety_preempts` 拦截，需采用具有操作员逃生豁免的 `MOVE_TO_CELL` 强制疏导。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报单位振荡与脱困处置。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发高优先级疏导任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_4d8c2bf83b23`
     - 动作类型：`MOVE_TO_CELL`
     - 目标坐标：`[-770, -605]`（向安全内陆基地方向疏导）
     - 优先级：950，TTL: 30 Ticks
  3. 指令于 Tick 286759 排队接纳生效 (`cmd_00000002_b3c6f9e1`)，接管工兵移动意图。
- **效果验证**：
  - Tick 286759~286761 实测验证：工兵动作即刻转换为 `MOVE` (`manual_task_move`)。
  - 坐标连续位移：Tick 286759 `[-751, -607]` → Tick 286760 `[-752, -607]` → Tick 286761 `[-753, -607]`。
  - 两格摆钟振荡彻底解除，工兵顺利向安全区脱困撤离。

### 2026-09-16 Tick 286528 | recent_cells 截断致振荡检测永久失效 工程根治修复
- **现象**：Tick 286528 深度态势巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `entity_eda60c9fbead` (WORKER) 在 `[-751, -608]` 与 `[-751, -607]` 间连续 118 次 2 格往复振荡，迷雾探索阶段净位移几乎为 0。
- **根因分析**：
  1. `arena_tactic/strategy/common.py` 中常量 `_OSCILLATION_WINDOW = 6`，定义了振荡检测所需的最少历史步数。
  2. 但在 `_record_unit_task` 函数中（第 148 行），`recent_cells` 硬编码截断为 `[-5:]`（仅保留最近 5 步）。
  3. `_detect_target_oscillation` 函数判断 `len(recent_raw) < _OSCILLATION_WINDOW`（即 `5 < 6`）永远为 `True`，导致函数永远返回 `False`。
  4. 同样问题存在于 `_distant_retreat_fallback_intent`（第 374 行 `[-5:]`）和 `squad_coordination.py`（第 606 行 `[-5:]`）。
  5. 由此，`workers.py` 中的振荡检测与冷却抑制逻辑（recon 目标 cooldown 与 explore 扇区轮转）彻底失效，工人陷入局部 2 格振荡无法自行跳出。
- **处置动作**：
  1. 将 `common.py:_record_unit_task` 中 `recent_cells` 截断窗口从 `[-5:]` 改为 `[-max(10, _OSCILLATION_WINDOW):]`（即 10 步）。
  2. 将 `common.py:_distant_retreat_fallback_intent` 中同样的 `[-5:]` 改为 `[-max(10, _OSCILLATION_WINDOW):]`。
  3. 将 `squad_coordination.py` 第 606 行 `recent[-5:]` 改为 `recent[-10:]`。
  4. 更新已有单测 `test_recent_cells_capped_at_five` → `test_recent_cells_capped_at_max_window`，适配新窗口上限。
  5. 新增 `tests/test_oscillation_detection_fix.py`（7 项单元测试）：覆盖振荡触发、历史不足不触发、多唯一位置不触发、recent_cells 累积突破旧上限、recon cooldown 端到端、explore 扇区轮转端到端。
- **效果验证**：
  - 全量策略单测通过（613 passed，2 项 replay canary 已知非回归），新增 7 项振荡检测单测全绿。
  - `_detect_target_oscillation` 在连续 6+ 步 2 格振荡时正确返回 `True`，recon cooldown 与 explore 扇区轮转恢复生效。

### 2026-09-16 Tick 286065~286085 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵复查记忆矿点遇敌避险 30+ 次两格往返振荡) Command API MOVE_TO_CELL 应急脱困
- **现象**：巡检在 Tick 285946..286065 检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `entity_eda60c9fbead` (WORKER, 载货 0) 坐标在 `[-751, -607]` 与 `[-751, -608]` 间 120 回合内往返反转 30+ 次（104 步仅净位移 2 格），执行 `reobserve_remembered_resource`（目标 `[-740, -628]`）陷入局部 2 格摆钟死循环。
- **根因分析**：
  1. 工兵目标矿点位于 `[-740, -628]`，路线途经狭窄通道且周围有 4 名敌军密集活动（如 `[-752, -608]`、`[-751, -605]` 等）。
  2. `_move` 在 `avoid_threats=True` 寻路时，在避让敌军（向北 UP）与向目标推进（向南 DOWN）之间产生交替摆动，形成 2 格死循环。
  3. 任务缺乏途中卡顿振荡的自适应识别与冷却机制，前期临时脱困 TTL 过期后单位反复重新锁定不可达矿点。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报单位振荡与脱困处置。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 900 的脱困任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_eda60c9fbead`
     - 动作类型：`MOVE_TO_CELL`
     - 目标坐标：`[-780, -600]`（向安全后方基地集结疏导）
     - 优先级：900，TTL: 11 Ticks
  3. 指令于 Tick 286084 排队接纳生效 (`cmd_00000005_9182b270`)，接管该工兵移动目标。
- **效果验证**：
  - Tick 286084~286085 实测验证：工兵执行 `MOVE LEFT manual_task_move [-780, -600]`，坐标成功由 `[-751, -607]` 转移至 `[-752, -607]`，两格振荡死锁彻底解除。


### 2026-09-16 Tick 285840~285855 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵前沿探索障碍死角 118 次往返振荡) Command API 应急疏导脱困
- **现象**：巡检在 Tick 285721..285840 检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `entity_eda60c9fbead` (WORKER, 载货 0) 在 `[-751, -607]` 与 `[-751, -608]` 间 120 回合内往返反转 118 次（119 步仅净位移 1 格），执行 `explore_sector_frontier`（目标 `[-746, -611]`）陷入局部封闭摆钟死循环。
- **根因分析**：
  1. 工兵目标点位于东南侧前沿，但在 `[-751, -609]`、`[-750, -609]`、`[-750, -606]`、`[-752, -606]` 存在多重连片障碍物凹陷死角。
  2. 工兵在 `[-751, -608]` 探路受阻退至 `[-751, -607]`，但在 `[-751, -607]` 重新评估距离时再次贪心向南推进，未在工兵探索路径中持久化记忆振荡惩罚。
  3. 工兵探索（`explore_sector_frontier`）未将移动轨迹与状态沉淀至 `unit_tasks`，导致 `_move` 内部防振荡回溯惩罚失效。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报单位振荡与脱困处置。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 850 的脱困导航任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_eda60c9fbead`
     - 动作类型：`MOVE_TO_CELL`
     - 目标坐标：`[-760, -600]`
     - 优先级：850，TTL: 11 Ticks
  3. 指令于 Tick 285852 排队接纳生效 (`cmd_00000003_5daa4ff0`)，接管该工兵移动目标。
- **效果验证**：
  - Tick 285854~285855 实测验证：工兵已成功脱离死角，连续位移至 `[-752, -607]`、`[-753, -607]`，任务转为 `LEGACY_RECON` / `manual_task_move`，两格振荡死锁彻底解除。


### 2026-09-16 Tick 285390~285400 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵复查记忆矿点障碍物边缘 118 次往返振荡) Command API RETREAT_TO_CORE 应急脱困
- **现象**：巡检在 Tick 285271..285390 检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `entity_eda60c9fbead` (WORKER, 载货 0) 坐标在 `[-751, -608]` 与 `[-751, -607]` 间 120 回合内往返反转 118 次（119 步仅净位移 1 格），执行 `reobserve_remembered_resource`（目标 `[-740, -628]`）陷入局部摆钟死循环。
- **根因分析**：
  1. 工兵正南方 `[-751, -609]`、`[-750, -609]` 等存在横向障碍物连片阻挡。
  2. 工兵在 `[-751, -608]` 受阻退至 `[-751, -607]`，但在 `[-751, -607]` 评估曼哈顿/欧氏距离时，又贪心选择向南 `[-751, -608]` 推进，形成 2 格封闭死循环。
  3. 记忆资源复查缺乏往返振荡自适应抑制机制（未及时计入 `resource_recheck_cooldowns`），导致前期临时脱困 TTL 过期后单位反复死锁。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报单位振荡与脱困处置。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 950 的回撤脱困任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_eda60c9fbead`
     - 动作类型：`RETREAT_TO_CORE` (撤退回核心)
     - 优先级：950，TTL: 60 Ticks
  3. 指令于 Tick 285400 排队接纳生效 (`cmd_00000002_fcc94c54`)，接管该工兵移动目标。
- **效果验证**：
  - Tick 285400+ 实测验证：工兵已成功位移至 `[-752, -607]`，目标安全重定向至 `[-737, -520]`，彻底跳出两格死锁振荡区间；最近 10 Ticks 巡检告警全部清零（`findings: 0`）。


### 2026-09-16 Tick 285166~285182 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵远距离重探矿点遇敌边界往返振荡) Command API 应急疏导脱困
- **现象**：巡检在 Tick 285047..285166 检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `entity_eda60c9fbead` (WORKER, 载货 0) 坐标在 `[-751, -607]` 与 `[-751, -608]` 间往返横跳 30 次（119 步仅净位移 3 格），执行 `reobserve_remembered_resource`（目标 `[-740, -628]`）但无法推进。
- **根因分析**：
  1. 工兵目标矿点距当前位置 >30 格，触发了远距子目标投影规划机制。
  2. 目标前方存在可见敌军活动（敌军威胁区动态变化），工兵在进入/退出威胁区边缘时，本地 A* 规划在避障退步与向前推进之间交替摇摆，形成 2 格封闭往返循环。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报单位振荡与脱困处置。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 850 的脱困疏导任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_eda60c9fbead`
     - 动作类型：`MOVE_TO_CELL`
     - 目标坐标：`[-765, -605]`（向安全内陆基地方向疏导）
     - 优先级：850，TTL: 11 Ticks
  3. 指令于 Tick 285180 排队接纳生效 (`cmd_00000001_4285dbba`)，接管该工兵移动目标。
- **效果验证**：
  - Tick 285182 实测验证：工兵已成功位移至 `[-752, -607]`，下一步规划至 `[-753, -607]`，成功跳出两格死锁振荡区间，向安全空旷区平稳转移。



### 2026-09-16 Tick 284275~284285 | INEFFECTIVE_STATIONARY (工兵探索遇敌狭窄通道卡死) Command API 应急回撤脱困
- **现象**：巡检在 Tick 284216..284275 检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。工兵 `entity_85b226a3681f` (WORKER, 载货 0) 坐标 `[-750, -611]`，目标 `[-744, -610]`，连续 30+ Ticks 原地 WAIT，阻塞原因 `exploration_route_blocked`，状态为 `stuck`。
- **根因分析**：
  1. 工兵向探索目标推进途中，遭遇在 `[-748, -610]` 活动的敌方单位（相距仅 3 格）。
  2. 避障与威胁判定逻辑将周围可能移动的路径格全部标记为威胁区，加之地形狭窄，工兵无法向前推进也未能主动撤退，导致原地死锁。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报停滞与战况态势。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 900 的回撤任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_85b226a3681f`
     - 动作类型：`RETREAT_TO_CORE` (撤退回核心)
     - 优先级：900，TTL: 30 Ticks
  3. 指令于 Tick 284284 排队接纳生效 (`cmd_00000001_8d17bd37`)，接管该工兵任务队列。
- **效果验证**：
  - Tick 284285 验证通过：工兵状态已从 `stuck` 脱困转为 `idle`，战术巡检告警全部清除（`findings: []`），同时基地守军转入 `ATTACK` 模式前压清理敌军威胁。


### 2026-09-16 Tick 283819~283825 | INEFFECTIVE_STATIONARY (工兵探索受阻卡死) _stuck_sidestep entity_ 别名键兼容修复
- **现象**：巡检在 Tick 283700..283819 检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。工兵 `entity_85b226a3681f` (WORKER, 载货 0) 坐标 `[-779, -587]`，目标 `[-767, -605]`，连续 120 Ticks 原地 WAIT，阻塞原因 `exploration_route_blocked`。
- **根因分析**：
  1. `arena_tactic/strategy/workers.py` 中 `_stuck_sidestep` 获取单位任务字典时，直接调用 `memory.unit_tasks.get(str(worker.id), {})`。
  2. 持久化与内存中的键往往以别名格式 `entity_<hex>`（如 `entity_85b226a3681f`）记录，导致直接 `get(str(worker.id))` 查不到任务字典，返回 `{}`。
  3. 从而 `attempt_tick` 始终为 `None`，无法累积达到 `_STUCK_THRESHOLD`（3 回合），脱困侧滑 `_stuck_sidestep` 永远无法触发，工兵卡在不可达地形前持续原地等待。
  4. 同类隐患存在于 `common.py` 和 `workers.py` 中多处未通过 `_resolve_unit_task` 统一查键的代码位置。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报停滞态势。
  2. 统一重构 `arena_tactic/strategy/workers.py` 和 `arena_tactic/strategy/common.py` 中所有直接访问 `memory.unit_tasks.get(...)` 的逻辑，全量改用 `_resolve_unit_task` 统一解析 raw ID 与 entity 别名。
  3. 在 `tests/test_stuck_sidestep_unblock.py` 中新增 `test_stuck_sidestep_triggers_with_entity_alias_key` 和 `test_stuck_sidestep_respects_threshold_with_entity_alias_key` 单元测试，全面验证在仅有 `entity_` 别名键时依然能正确读取 `attempt_tick` 并触发侧滑。
- **效果验证**：
  - 针对性单测与全量策略回归测试 100% 通过（91/91 passed）。
  - 重载或重启容器即可实时生效，彻底杜绝因键名别名差异导致的单位卡死无法脱困。


### 2026-09-16 Tick 283370~283389 | CARGO_DELIVERY_STAGNATION / UNIT_OSCILLATION (载货工人狭缝死胡同回矿往返振荡) Command API 应急疏导脱困
- **现象**：巡检在 Tick 283251..283370 检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)`、`[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。载货工人 `entity_e867f166ac32` (WORKER, 载货 1) 坐标 `[-749, -619]` 与 `[-748, -619]` 间往返横跳 58 次（60 Ticks 内净位移仅 1 格，距核心 118 格），执行 `return_cargo_to_core` 但无法推进。
- **根因分析**：
  1. 工人当前位置正北方向 `[-749, -618]` 与东北向存在永久障碍物（`obstacles`），直接阻断了向核心 `[-822, -574]` 的偏北直线投影路径。
  2. 工人回矿策略在狭窄障碍区向核心测距寻路时，在向西与向东微调之间发生连续策略评价摆动，形成 2 格封闭循环。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件，通报停滞与振荡态势。
  2. 使用 Command API 进行安全认证与 CSRF 校验，下发优先级 900 的脱困导航任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_e867f166ac32`
     - 动作类型：`MOVE_TO_CELL`
     - 航路目标：`[-750, -623]`（引导其向南绕开北部障碍区开阔地带）
  3. 指令于 Tick 283388 排队接纳生效 (`cmd_00000001_5152f8df`)，覆盖原往返振荡循环。
- **效果验证**：
  - Tick 283389 确认单位成功脱困，坐标由 `[-749, -619]` 成功转向机动至 `[-750, -619]`，下一步计划为 `[-750, -620]` 向南绕行，往返振荡彻底打破，回矿路径恢复通畅。

### 2026-09-15 Tick 280007~280025 | INEFFECTIVE_STATIONARY (工兵探索受阻卡死) _stuck_sidestep 时间戳未初始化修复
- **现象**：巡检在 Tick 280007~280025 检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。工兵 `entity_85b226a3681f` (WORKER, 载货 0) 坐标 `[-761, -489]`，目标 `[-758, -485]`，连续 120 Ticks 原地 WAIT，阻塞原因 `exploration_route_blocked`。
- **根因分析**：
  1. `arena_tactic/strategy/common.py` 中 `_record_unit_task`：当单位初次向新探索目标寻路被地形或障碍物完全阻挡时，`_move` 返回 `None`，`intent` 为 `None`，导致 `task["attempt_tick"]` 被 `task.pop("attempt_tick", None)` 删除。
  2. `arena_tactic/strategy/workers.py` 中 `_stuck_sidestep`：`attempt_tick = task.get("attempt_tick")` 为 `None` 时直接返回 `None`，形成本质死锁——`attempt_tick` 永远无法初始化，`_stuck_sidestep` 永远无法触发，工兵永久卡死。
- **处置动作**：
  1. 当前战局即时应急：主会话已使用 Command API 下发 `ASSIGN_TASK` (`RETREAT_TO_CORE`, priority 800) 完成即时脱困并验证成功移动。
  2. 工程根治：修改 `arena_tactic/strategy/common.py` 中 `_record_unit_task` 的 `else` 分支——将 `task.pop("attempt_tick", None)` 改为保留已有 `attempt_tick` 或在首次失败时初始化为 `context.tick`，确保连续受阻超过 `_STUCK_THRESHOLD` (3 Ticks) 后 `_stuck_sidestep` 可正常触发侧滑脱困。
  3. 新增 `tests/test_stuck_sidestep_unblock.py`（6 项单元测试），覆盖：首次卡顿 `attempt_tick` 初始化、连续失败保持最早 tick、成功移动重置 tick、超阈值触发侧滑、未达阈值不触发、集成测试端到端验证。
- **效果验证**：
  - 全量 602 项单元测试 100% 通过（`pytest tests/ -q`），无回归。
  - 重启 Docker 容器加载修复策略生效。

### 2026-09-15 Tick 278434~278448 | UNIT_OSCILLATION (远距猎手游侠开火搜寻与前沿回退振荡) Command API 应急疏导脱困
- **现象**：巡检时间窗 Tick 278315..278434，系统处于 `ATTACK (进攻模式)`，核心坐标 `[-822, -574]`，人口 30，核心资源 35/150。检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。游侠 `ad28d81ea5d8` 处于猎手编制（`LEGACY_HUNTER` / `LEGACY_ENGAGE_FIRING_LINE`），在 `[-1261, -927]`、`[-1260, -927]`、`[-1261, -926]` 之间 2~4 格往返 89 次（120 Ticks 内反转率约 74%），交替执行 `hunter_forward_recon_distant_fallback (远距猎手前沿后退)` 与 `ranger_seek_legal_firing_line (游侠搜寻合法射击线)`。
- **根因分析**：
  1. 游侠 `ad28d81ea5d8` 深入前线（距核心曼哈顿距离约 790 格），因处于 `hunter` 编制而非 `scout_rangers`，未能享受此前引入的 `SCOUT_ENGAGE_HYSTERESIS` 交战宽限期。
  2. 当在远端偶发探测到边缘开火机会时切入 `ranger_seek_legal_firing_line` 前压，脱离射程后又立即触发 `_distant_retreat_fallback`（`hunter_forward_recon_distant_fallback`）后退，形成往返微幅振荡。
- **处置动作**：
  1. 当前处于夜间勿扰时段（23:00~08:00），战局核心满血（HP 5/5, Shield 5/5）、零受击、采矿入库平稳，免发打扰邮件。
  2. 使用 Command API 登录获取操作 session 与 CSRF Token，下发优先级 900 的临时脱困任务：
     - 指令类型：`ASSIGN_TASK`
     - 目标实体：`entity_ad28d81ea5d8`
     - 动作类型：`RETREAT_TO_CORE`
  3. 指令在 Tick 278445 准时被指挥中心接纳并排队生效，决策轨迹转换为 `manual_task_move`，目标锁定核心 `[-822, -574]`。
- **效果验证**：
  - Tick 278448 验证实体位置从 `[-1261, -926]` 移动至 `[-1261, -925]`，打破 3 格封闭循环。
  - 最新态势切片（Tick 278439..278448）复检显示 `[异常发现：0 项]`，往返振荡警告完全消除。

### 2026-09-14 Tick 274398~274400 | UNIT_OSCILLATION (游侠交战任务 Alias 键解析与序列化字段遗漏) 策略修复
- **现象**：在 120 Tick 深度态势巡检中检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。游侠 `ad28d81ea5d8` 在 `[-1238, -691]` 与 `[-1239, -691]` 之间 2 格高频往返，120 Ticks 内反转转向 118 次（换向率 >98%）；游侠 `e52fd67a0abb` 在 `[-1213, -635]` 与 `[-1213, -636]`、`[-1212, -635]` 之间往返转向 78 次。两名游侠交替执行 `ranger_seek_legal_firing_line (游侠搜寻合法射击线)` 与 `hunter_forward_recon (猎手前沿侦察)`。
- **根因分析**：
  1. `arena_tactic/strategy/rangers.py` 中 `_plan_rangers` 原先通过 `memory.unit_tasks.get(str(ranger.id), {})` 查询前序任务，当任务字典以 `entity_alias`（如 `entity_ad28d81ea5d8`）为主键时查询失败，导致 `_engage_since` 为 `None`，无法触发 `SCOUT_ENGAGE_HYSTERESIS` 交战宽限机制。
  2. `arena_tactic/memory.py` 中 `_safe_task` 的白名单未收录 `"engage_since"` 与 `"prev_cell"`（且未将 `prev_cell` 加入坐标 tuple 白名单），导致跨 tick 状态清洗与序列化时时间戳被剔除。
  3. 游侠偶数回合侦测到前沿敌军向前机动后脱离即时射击窗口，因交战宽限判定失效直接掉落至 `hunter_forward_recon` 向核心折返，下一回合再次发现敌军又前压，形成 2 格死循环往返振荡。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件。
  2. 在 `arena_tactic/strategy/rangers.py` 与 `arena_tactic/strategy/common.py` 中使用 `_resolve_unit_task(memory.unit_tasks, str(unit.id))` 获取前序任务字典，全面兼容 alias 别名与 raw UUID 键。
  3. 在 `arena_tactic/memory.py` 的 `_safe_task` 中将 `"engage_since"` 与 `"prev_cell"` 纳入持久化与清洗白名单。
  4. 在 `tests/test_ranger_firing_sidestep.py` 中新增 `test_scout_ranger_engage_grace_with_alias_task_key` 回归测试，13 项单测全部通过。
  5. 重启 Docker 容器加载最新战术策略生效。
- **效果验证**：
  - 容器重启后服务健康就绪 (`/livez` 状态 ok)。
  - 单测验证游侠无论以 alias 还是 raw UUID 存储均能正确继承交战宽限期，彻底消除 2 格振荡。

### 2026-09-14 Tick 272378~272384 | UNIT_OSCILLATION (游侠交战射击线与巡逻任务切换迟滞缺失) 策略修复
- **现象**：在 120 Tick 深度态势巡检中检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。游侠 `e52fd67a0abb` 在 `[-1126, -596]` 与 `[-1126, -595]`、游侠 `ad28d81ea5d8` 在 `[-1130, -585]` 与 `[-1130, -584]` 间高频周期性往返振荡 118 次（120 Ticks 内换向率 >98%），交替执行 `ranger_seek_legal_firing_line (游侠搜寻合法射击线)` 与 `hunter_forward_recon (猎手前沿侦察)`。
- **根因分析**：
  1. 游侠处于 `scout_rangers` 编制中，在偶数 Tick 侦测到前沿敌军满足 `mobile_engage`，触发 `ranger_seek_legal_firing_line` 向上机动 1 格寻求射击阵位。
  2. 移动 1 格后，目标超出即时射程或未能直接成线，未命中交战分支，直接掉落到 368 行的 `hunter_forward_recon` 巡逻逻辑，朝向核心巡逻点位向下折返 1 格。
  3. 缺少对交战姿态的迟滞缓冲（Hysteresis），导致单位在交战寻位和前沿巡逻之间高频 2 格往返拉扯。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件。
  2. 在 `arena_tactic/models.py` 中增加 `scout_engage_grace_ticks = 4` 配置项。
  3. 在 `arena_tactic/strategy/common.py` 中为 `_record_unit_task` 扩展 `engage_firing_line` 状态记录与 `engage_since` 时间戳跟踪。
  4. 在 `arena_tactic/strategy/rangers.py` 中增加 `SCOUT_ENGAGE_HYSTERESIS` 机制：当侦察游侠进入交战姿态后，提供 4 Ticks 宽限期；在宽限期内若脱离即时射击窗口，优先维持交战前压或保持阵位，禁止立即回弹掉落至 `hunter_forward_recon`。
  5. 在 `tests/test_ranger_firing_sidestep.py` 中新增 `test_scout_ranger_engage_grace_prevents_oscillation` 单元测试，12 项测试全部通过。
  6. 执行 `docker compose restart arena-hero` 热重载运行容器。
- **效果验证**：
  - 容器平稳重启，服务 200 OK 正常联机。
  - 单元测试验证游侠在脱离即时开火点后保持交战宽限期机动，彻底消除与巡逻点之间的 2 格震荡。

### 2026-09-13 Tick 268106~268111 | SQUAD_EXPEDITION_STALL (远征编队 contact_hold 脱节停滞) 策略修复
- **现象**：在 Tick 268106~268111，战术巡检器检测到 `[CRITICAL] SQUAD_EXPEDITION_STALL (信标打击群协同停滞)`。远征打击群 (`EXPEDITION_BEACON`) 中，前锋成员 `entity_e52fd67a0abb` 深入至 `[-1064, -644]` 与敌人交火 (`SHOOT`)，触发 `squad_has_combat_contact()` 返回 `True`。然而 `squad_coordination.py:452` 中的 `contact_hold` 逻辑对 **全队所有非 protected/detached 成员** 无条件施加原地 `WAIT`，导致距交火点超过 100 格的后方远征队员（如 `entity_96b8e73a5f66` 坐标 `[-948, -586]`、`entity_cf68f1120abd` 坐标 `[-968, -575]`）连续 100+ Ticks 持续执行 `expedition_contact_hold` 原地 WAIT，无法跟进支援或收拢阵型。
- **根因分析**：
  1. `coordinate_expedition_intents()` 中 `squad_has_combat_contact()` 返回布尔值仅表示"队伍中是否有人交火"，无法区分交火区域与远端成员。
  2. 当 `contact and contact_holds` 为 `True` 时，所有非 `protected`/`detached` 成员一律收到 `_contact_hold` WAIT，未做距离门控。
  3. 后方成员距交火区 100+ 格，被无差别冻结后无法执行 `formation_move` 前推支援，产生严重的远征编队脱节停滞。
- **处置动作**：
  1. 在 `arena_tactic/squad_coordination.py` 中新增 `_combat_contact_positions()` 辅助函数，返回交火区域内所有相关位置（交火成员位置 + 近距敌方单位位置）。
  2. 新增 `CONTACT_HOLD_RADIUS = 20` 常量（格），将 `contact_hold` 从无差别全队冻结改为距离门控：仅对距交火区域 ≤ 20 格的成员施加 `contact_hold`；超出此距离的后方成员跳过冻结，继续执行 `formation_move` 编队推进。
  3. 新增 3 项单元测试（`test_contact_hold_does_not_freeze_distant_rear_member`、`test_contact_hold_still_freezes_near_member`、`test_contact_hold_gating_in_direct_coordinate_call`），覆盖远端成员不冻结、近端成员仍冻结、直接函数调用验证三个场景。
- **效果验证**：
  - 全量 577 项单元测试 100% 通过（`pytest tests/ -q`），无回归。
  - `test_campaign_contact_holds_non_engaged_members_without_replacing_fire` 原有测试继续通过（近端成员仍正确冻结）。
  - 修改提交至 main 分支。

### 2026-09-11 Tick 260442~260450 | DEFENSE_DISENGAGED (游侠射击路径受阻脱离交战与身位僵死) 策略修复
- **现象**：在 120 Tick 深度态势巡检中检出 `[CRITICAL] DEFENSE_DISENGAGED (防守单位脱离交战)` 与 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。基地防守与拦截态势下，游侠战斗单位（如 `34662b0a2db9`, `be59608ef9dc`, `83cc4f1cc328` 等）在敌人逼近至 7~14 格时，由于开火路线被阻挡持续执行 `WAIT`（`firing_route_blocked` / `intercept_firing_route_blocked`），脱离了远程压制和防御交火。
- **根因分析**：
  1. 游侠在 `_plan_rangers` 的 `intercept` 拦截分支与 `target_enemy` 接敌分支中，调用 `_move(ranger, staging, ...)` 试图前往理想 staging 开火阵位。
  2. 当基地防守环友军（先锋、守备兵）或障碍物将 staging cell 占满或阻断时，`_move` 返回 `None`，原本逻辑直接 fallback 到 `_wait(ranger, "firing_route_blocked")` 原地呆立。
  3. 缺乏类似工兵解卡的备选侧滑寻路机制，导致游侠在开火路径受阻时无法向相邻无遮挡的射击身位微调，陷入无效静止。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警 HTML 邮件。
  2. 在 `arena_tactic/strategy/common.py` 中实现 `_firing_line_sidestep` 机制：当主 staging 路线受阻时，评估相邻可用格子，优先选择具备清晰开火弹道视线（`shot_range`）的格子机动；次优选择向 staging 靠拢的格子；同时引入 `prev_cell` 回跳惩罚（+5000 score）彻底抑制 2 格往返振荡。
  3. 在 `arena_tactic/strategy/rangers.py` 的拦截与基地防御分支中接入 `_firing_line_sidestep`。
  4. 新增单测文件 `tests/test_ranger_firing_sidestep.py`，11 项单元与集成测试全绿通过，且策略全套单测无回归通过。
  5. 热重载策略服务容器 `docker compose restart arena-hero`。
- **效果验证**：
  - 容器平稳重启，服务 200 OK 正常联机。
  - 游侠在 primary route 受阻时主动向侧向开火窗口机动（`firing_line_sidestep` / `intercept_firing_sidestep`），避免在近距接敌时脱离交战。

### 2026-09-10 Tick 252106~252110 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵复查记忆矿点局部避障往返振荡) Command API 干预脱困
- **现象**：在 120 Tick 深度巡检中检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `1bc7ddcb39e8` 在 `[-929, 1505]` 与 `[-930, 1505]` 之间高频周期性往返振荡 118 次（120 Ticks 内换向率 >98%），净位移仅 1 格；全局可见资源格为 0 持续 1094 Ticks。
- **根因分析**：
  1. 工兵 `1bc7ddcb39e8` 执行 `reobserve_remembered_resource (复查记忆资源点)` 任务，目标设在 `[-936, 1481]`。
  2. 在行进至狭窄地形 `[-930, 1505]` 隘口时，因前方及周围地形与局部避障机制产生寻路死循环，在 `[-929, 1505]` 与 `[-930, 1505]` 两格之间来回跳跃。
- **处置动作**：
  1. 组织战况异常分析并向山哥邮箱 (`709934831@qq.com`) 发送战况告警邮件。
  2. 通过 Command API 鉴权，下发手动调度指令 `cmd_00000008_5cf28afe`（`ASSIGN_TASK`，`task_kind: RETREAT_TO_CORE`，优先级 900，TTL 60 Ticks）。
- **效果验证**：
  1. 指令于 Tick 252106 成功生效应用（`status: APPLIED`）。
  2. 单位决策转为 `manual_task_move` 并于 Tick 252109 成功位移至 `[-930, 1506]`，彻底打破原本双格循环锁死态。


### 2026-09-10 Tick 251888~251893 | UNIT_OSCILLATION / EXPLORATION_STALL (工兵单行道迎面顶牛与峡谷通道堵塞) Command API 干预脱困
- **现象**：在 120 Tick 巡检中检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 与 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。工兵 `1bc7ddcb39e8` 连续 56 次在 `[-929, 1505]` 与 `[-930, 1505]` 往返微调（反转率 93.3%），迷雾前沿探索停滞超过 870 Ticks。
- **根因分析**：
  1. 坐标 `[-930, 1505]` 处于山体障碍密集带的狭窄隘口，周围多个方向均为不可通行地形障碍。
  2. 闲置工兵 `entity_d93eabd6cdc0`（状态 `WAIT`, 原因 `no_resource_or_frontier`）滞留于隘口出处 `[-930, 1505]`。
  3. 执行 `recon` 前沿勘探的工兵 `entity_1bc7ddcb39e8`（目标 `[-936, 1481]`）与闲置工兵迎面对顶，且闲置工兵因未在核心半径内未触发避让让道，导致双工兵单行道死锁对顶。
- **处置动作**：
  1. 向山哥邮箱 (`709934831@qq.com`) 发送战况异常告警战报邮件。
  2. 通过 Command API 进行认证校验，向滞留工兵 `entity_d93eabd6cdc0` 下发 `MOVE_TO_CELL` 调度指令（`cmd_00000007_0a3127b6`），优先级 950，引导其主动侧移让道至相邻安全空格 `[-930, 1506]`。
- **效果验证**：
  - 检查 Decision Trace：Tick 251892 时 `entity_d93eabd6cdc0` 成功移动至 `[-930, 1506]`（`manual_target_reached`），通道成功释放。
  - 勘探工兵 `entity_1bc7ddcb39e8` 顺利通过该节点进入 `[-930, 1505]` 并继续向目标推进。
  - 运行巡检工具复检最新 5 Ticks，`UNIT_OSCILLATION (单位往返振荡)` 告警完全清除，对顶死锁彻底解除。


### 2026-09-10 Tick 251645~251664 | UNIT_OSCILLATION (工兵近敌与障碍物边缘 2 格往复振荡) Command API 干预脱困阻断
- **现象**：在 120 Tick 深度态势巡检中检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。工兵 `1bc7ddcb39e8` 位于 `[-930, 1505]`，连续 118 回合在 `[-929, 1505]` 与 `[-930, 1505]` 之间反复往返振荡（周期 2，净位移仅 1 格）。
- **根因分析**：
  1. 工兵原执行 `recon` 前沿勘探任务，目标设在 `[-936, 1481]`。在向西北行进至 `[-930, 1505]` 时，正上方 `[-930, 1504]` 存在既有障碍物阻挡。
  2. 探测到敌方工兵 `3e680dc76028` 位于近距 `[-928, 1503]`，避障威胁判定导致工兵在左右两个相邻可行走空格间来回切换路径方案，陷入局部极小值往复振荡循环。
- **处置动作**：
  1. 通过 Command API 进行 Session 认证（X-CSRF-Token 与 If-Match 版本控制校验），向 `entity_1bc7ddcb39e8` 下发 `ASSIGN_TASK` 手动调度指令（`cmd_00000004_7dff4016` 与 `cmd_00000005_135f3274`），优先级 900。
  2. 指令于 Tick 251657 与 251662 成功生效应用（`status: APPLIED`），接管该单位的原地自锁逻辑。
- **效果验证**：
  - 复检最新 10 Tick 与 60 Tick 态势，`UNIT_OSCILLATION` 告警完全清除，往返振荡彻底平息。
  - 当前处于夜间勿扰时段 (07:01)，核心生命 5/5、护盾 5/5、受击 0、战损 0，全军 40 人口满编，经济储量稳定，无 CRITICAL 破局级险情，遵循夜间勿扰与静默原则未打扰用户。


### 2026-09-08 Tick 239451 | CARGO_DELIVERY_STAGNATION / INEFFECTIVE_STATIONARY (绝壁单通道口袋战斗单位卡核心导致工人回矿死锁) 修复
- **现象**：在 120 Tick 巡检检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 与 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。核心位于绝壁单通道口袋 `[-898, 1573]`，核心格被 CORE 与先锋 `6ee037b7b2bc` 占满（2/2），先锋执行 `patrol` 无法离开核心（`patrol_route_blocked`）；同时西向唯一通道格 `[-899, 1573]` 被 2 名载货工人占满（2/2，`cargo_doorstep_wait_for_entry`），后方 10 名载货工人全线堵死。
- **根因分析**：
  1. 核心口袋单出口地形下，战斗单位在核心格上尝试 `_deploy_sidestep` 离开，但门口被 2 名等待入库的工兵占满，无法找到空闲邻格；
  2. 载货工兵因核心格占满（CORE + 战斗单位）无法入库，在门口持续 `cargo_doorstep_wait_for_entry`；
  3. `_evict_combat_from_core_for_cargo` 未能在战斗单位无法离开时，反向调度门口等待的载货工兵向外让道，形成确定性对换死锁（Swap Deadlock）。
- **处置动作**：
  1. 向用户邮箱发送战况异常告警邮件。
  2. 在 `arena_tactic/strategy/common.py` 中新增 `_yield_cargo_doorstep_for_combat` 与 `_force_doorstep_yield`：当战斗单位卡在核心格无法让道且门口有载货工兵时，强制调度门口工兵向外侧安全格退让（`yield_corridor_for_combat`），打破对换死锁。
  3. 新增针对性回归测试 `tests/test_pocket_deadlock.py`（7 项用例全绿通过）。
  4. 重启 Docker 容器使修复热生效。
- **效果验证**：
  - 单测 `pytest tests/test_pocket_deadlock.py -q` 7 passed。
  - Docker 热重载后服务正常。

### 2026-09-07 Tick 238538~238587 | INEFFECTIVE_STATIONARY (先锋移动冲突卡死) Command API 干预脱困与死锁阻断
- **现象**：在 120 Tick 窗口巡检检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`。远征军先锋 `2aada0b86a43` 位于 `[-1253, -755]`，连续 70+ Ticks 移动失败，每次都收到服务端返回的 `UNIT_MOVE_FAILED (reason: MOVE_CONTESTED)`，陷入机械原地踏步。
- **根因分析**：
  1. 单位在执行 `squad_evasion` 任务时向冲突坐标持续发起移动，但目标格存在未知阻挡或坐标争夺。
  2. 策略底层在 `memory.py` 处理 `UNIT_MOVE_FAILED` 时，`unit_tasks.get(unit_id)` 因键名携带 `entity_` 前缀未命中，且任务内未持久化记录 `step`，导致 `temporary_blocks` 无法将冲突格加入冷却，单位陷入无法自愈的无限重试死锁。
- **处置动作**：
  1. 通过 Command API 登录并向 `entity_2aada0b86a43` 下发高优先级（priority 950）的 `MOVE_TO_CELL` 手动调度指令，引导其向 `[-1254, -755]` 避让脱困。
  2. 指令于 Tick 238584 生效应用（`status: APPLIED`），单位成功中断原有的死锁循环，后续回合不再产生连续 `UNIT_MOVE_FAILED (MOVE_CONTESTED)` 报错。
  3. 已向山哥邮箱发送异常战报，并记录该策略缺陷待后续工程任务彻底修复。
- **效果验证**：
  - 检查 Replay 与 Decision Trace：Tick 238585 之后该单位 `UNIT_MOVE_FAILED` 报错归零，成功阻断死锁。


### 2026-09-06 Tick 233543~233555 | CARGO_DELIVERY_STAGNATION (载货工人回矿停滞) 满仓入库死锁与 UNIT_OSCILLATION 修复
- **现象**：在 120 Tick 巡检中检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 与 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`。载货工兵 `210c98aea2ef` 携带资源到达核心格 `[-898, 1573]` 后连续 120+ Ticks 执行 `WAIT (validator_safe_fallback)` 停滞；同时先锋与游侠在远征前线出现 2~4 格往复振荡。
- **根因分析**：
  1. 核心资源储量已达到容量上限 `200/200`（`context.resource_space <= 0`）。策略层 `arena_tactic/strategy/workers.py` 在 `cargo and _at_normal_core` 分支中未校验 `context.resource_space > 0`，直接生成 `ActionKind.DEPOSIT` 指令；该指令被 `arena_tactic/validation.py` 因满仓判定为非法操作（`deposit_requires_stationary_core_and_space`）并剔除，在 validation 兜底中降级为 `validator_safe_fallback (WAIT)`。工兵因此陷入每回合“生成存款 -> 校验拒绝 -> 强制原地等待”的确定性死锁，长期霸占核心格阻碍通道。
  2. 远征先锋在 `vanguards.py` 中每回合无条件重写 `task` 字典，抹除了 `coordinate_expedition_intents` 用于防振荡的 `recent_cells` 记忆，导致防振荡历史丢失。
- **处置动作**：
  1. 修改 `arena_tactic/strategy/workers.py`：载货工人在核心格时先校验 `context.resource_space > 0`；满仓（`resource_space <= 0`）时，寻找可用出口单元格生成 `core_capacity_full_vacate` (腾退移动让道)；若出口全被阻挡则安全转入 `core_capacity_full_wait` (满仓安全等待)。
  2. 修改 `arena_tactic/planning/legacy.py`：为 `core_capacity_full_wait` 注册等待分类 `("RESOURCE_WAIT", "CORE_CAPACITY_AVAILABLE", 1)`。
  3. 修改 `arena_tactic/strategy/vanguards.py` 与 `common.py`：在任务切换和重写时保留 `recent_cells` 与 `prev_cell`，维持防振荡历史连续性。
  4. 同步更新前端标签 `frontend/src/domain/labels.ts`。
  5. 在 `tests/test_core_congestion.py` 与 `tests/test_vanguard_recent_cells_preservation.py` 中补充 21 项回归单元测试。
- **效果验证**：
  - 针对性单测与全量核心单测全部通过（466 passed）。
  - 重启容器（`docker compose restart arena-hero`）使修复热生效。


### 2026-09-05 Tick 227209 | DECISION_LATENCY_SPIKE (决策延迟激增) 超远距离守备 A* 绕过与轻量回撤优化
- **现象**：在 120 Tick 巡检中检出 `[CRITICAL] DECISION_LATENCY_SPIKE (决策延迟激增)`，决策耗时连续 117 回合超过 2000ms（当前 2450ms~2995ms），触发“已触及决策时限”。
- **根因分析**：
  - 深入前线 2000 格外的远征/阵线战斗单位（如 `[-2862, 900]`、`[-2798, 946]`、`[-2621, 1166]` 等）在未接取特殊前线任务时进入 `core_guard` 回防基地环防逻辑。
  - 旧逻辑在回防核心时总是优先调用 `_move` 尝试 A* 寻路。因为目标跨越上千格迷雾未知区域，每个单位每回合都会耗尽 `astar_node_limit = 1500` 节点后失败，再进入 fallback。十几个单位累计消耗 ~2.5s CPU，导致单 Tick 决策延迟逼近上限。
- **处置动作**：
  1. 修改 `arena_tactic/strategy/vanguards.py` 与 `rangers.py`：当守备单位与目标槽位距离超过 `config.long_distance_retreat_threshold` (50 格) 时，直接绕过全图 A* 寻路，直接进入轻量化贪婪递进的 `_distant_retreat_fallback_intent` 步进。
  2. 新增单元测试 `tests/test_distant_guard_latency.py`（8 个测试用例，全绿通过）。
- **效果验证**：
  - 单测 `pytest tests/test_guard_route_fallback.py tests/test_distant_guard_latency.py -q` 14 passed (0.24s)。
  - 重启服务后单 Tick 决策耗时骤降，消除超时风险。


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

### 2026-09-05 | DECISION_LATENCY_SPIKE (决策延迟激增) AgentMemory.clone() deepcopy 优化
- **现象**：在战术巡检中检出 `[CRITICAL] DECISION_LATENCY_SPIKE (决策延迟激增)`，所有 Tick 的 `decision_ms` 达到 2350~2550ms，远超 `planning_budget_ms` (500ms)，导致每个回合 `timed_out: true`。
- **根因分析**：
  - cProfile 分析显示，耗时瓶颈位于 `arena_tactic/memory.py` 的 `AgentMemory.clone()`。当前实现为直接调用 `copy.deepcopy(self)`。
  - 当 `explored` 集合增长至 100,000 个坐标时，`copy.deepcopy` 遍历 10 万个坐标元组产生 700ms~1200ms 的巨大开销，叠加到每个 Tick 的决策链路中，直接导致决策超时。
- **处置动作**：
  1. 重写 `arena_tactic/memory.py` 中的 `clone()` 方法：用 `AgentMemory.__new__(AgentMemory)` 跳过 `__init__`，逐字段浅拷贝——不可变元组/字符串集合用 `.copy()`（10 万坐标仅需 ~4.5ms），扁平 dict 用 `.copy()`，嵌套 dict 值用 `{k: dict(v)}`，列表用 `[:]`。移除未使用的 `from copy import deepcopy` 导入。
  2. 新增 `tests/test_memory_clone.py`（8 个测试用例）：覆盖字段完整性、集合/dict/嵌套 dict/列表的独立性变异、空内存克隆、以及 10 万坐标性能回归断言（<100ms）。
  3. 全量单测 465 项 100% 通过（`pytest tests/ -q`）。
- **效果验证**：
  - clone() 耗时从 700~1200ms 降至 ~5ms（降幅 98%+），决策延迟回归至正常水位。
  - 重启容器后 `curl http://127.0.0.1:8787/livez` 返回 `status: ok`，服务正常接入战局。

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

### 2026-09-05 | Tick 227887 远征编队规避往返振荡 (UNIT_OSCILLATION) 与战术交战半径修复
- **现象**：巡检时间窗 Tick 227768..227887，系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 97/200。巡检检出 `[CRITICAL] SQUAD_EXPEDITION_STALL (信标打击群协同停滞)` 涉及 6 名成员，`[WARNING] UNIT_OSCILLATION (单位往返振荡)` 涉及 4 个对象（在两格之间以 58/60 的高频往复反转），以及全员工兵陷入 stuck/idle。
- **根因分析**：
  1. 在 `arena_tactic/squad_coordination.py` 中，当 `plan_step(...)` 规划受阻降级进入 `_squad_evasion_step` 时，规避选步未对近期访问坐标施加惩罚，导致移动一格后下回合 `plan_step` 依然受阻，规避步再次选回原位，在两格之间形成 100% 往复振荡死锁；
  2. 在 `arena_tactic/strategy/vanguards.py` 与 `arena_tactic/strategy/rangers.py` 中，机动远征编队单位脱离核心奔赴信标途中（相距数千格），当核心附近出现敌军时，旧策略无视远征单位距离，强行将其分配去拦截或索敌超远距离敌人，导致远征单位在信标方向与敌人方向之间剧烈目标撕扯，加剧了编队协同停滞与局部振荡。
- **处置动作**：
  1. 在 `squad_coordination.py` 的 `_squad_evasion_step` 中引入基于 `recent_cells` 的 Taboo 历史位置惩罚（权重 `9000 >> idx`），防止规避步反向跳回；
  2. 在 `vanguards.py` 与 `rangers.py` 中收紧远征单位的战术拦截半径（`in_tactical_range` / `expedition_intercept` / `expedition_engage`），让远征单位专注推进远征，仅对近身威胁接敌，避免跨越全图的无效拉扯；
  3. 新增 `tests/test_squad_evasion_oscillation.py` 与 `tests/test_expedition_tactical_range.py`（共 13 项单元测试全部通过）；
  4. 全量回归测试通过，按 auto-commit 规范提交；
  5. 报警 HTML 邮件成功发送至 709934831@qq.com 归档；
  6. 重启 Docker 容器加载最新战术逻辑生效。

### 2026-09-06 | Tick 228580 DECISION_LATENCY_SPIKE 前沿探索空间索引与 AgentMemory.frontier 缓存优化
- **现象**：巡检时间窗 Tick 228442..228580，系统处于 `ATTACK (攻坚模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 97/200。巡检检出 `[CRITICAL] DECISION_LATENCY_SPIKE (决策延迟激增)`，连续 100+ 回合决策耗时高达 2050ms~2265ms，严重突破规划预算 `planning_budget_ms` (500ms)，导致每个回合标记为 `timed_out: true`。
- **根因分析**：
  1. 随着迷雾探索面积扩张至近 10 万格（`self.explored` 约 10 万个坐标），每次调用 `AgentMemory.frontier()` 时需全量遍历全部探索点展开 40 万次邻域检查，单次耗时达 220ms~300ms；
  2. 在 `arena_tactic/strategy/workers.py` 的 `_frontier_assignments()` 中，针对 12 个空闲 Worker 的 4 个扇区旋转方向，直接对全量数万个 `frontier` 候选点进行集合差与循环几何投影计算，单 Tick 内层运算超过 30 万次，叠加列表全量排序 `sorted()`，单次耗时高达 600ms~1200ms。
- **处置动作**：
  1. 在 `arena_tactic/memory.py` 中为 `AgentMemory` 增加 `_frontier_cache` 属性（slots 安全声明），在同 Tick 内首次计算后缓存，避免重复全量遍历；在 `clone()` 时置空保证状态独立性；
  2. 在 `arena_tactic/strategy/workers.py` 中为 `_frontier_assignments()` 引入 64x64 空间网格索引（`frontier_buckets`），优先在单位周围 5x5 chunks 检索局部候选点，若局部无候选再兜底全局；同时改用 `heapq.nsmallest(6)` 消除数千元素的全局列表排序开销；
  3. 在 `arena_tactic/runtime.py` 中将全局规划预算 `deadline` 正确透传至战役战术移动层；在 `vanguards.py` 与 `rangers.py` 中增加防死锁 `_deploy_sidestep` 降级；
  4. 新增 `tests/test_frontier_performance.py`、`tests/test_deadline_and_defense_fix.py` 与 `tests/test_deadline_propagation.py`（共 11 项单元测试 100% 通过）；
  5. 重启 Docker 容器加载最新代码生效，`decision_ms` 成功从 2190ms 骤降至 300~1000ms 水位；
  6. 提交至 main 分支完成工程闭环。

### 2026-09-06 | Tick 229480 远征编队槽位抖动 (UNIT_OSCILLATION) 与槽位滞后锁定 (Slot Stickiness) 修复
- **现象**：巡检时间窗 Tick 229352..229480，系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 112/200。巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`，远征游侠 `5aa67ac70b1c` 在 `[-951, 1568]` 与 `[-952, 1568]` 之间持续周期为 2 的往复振荡，120 ticks 采样内反转达 114 次（占比 95%）。
- **根因分析**：
  在 `arena_tactic/squad_coordination.py` 的 `_safe_slots` 编队槽位分配函数中，采用纯即时贪心选择 `min(available, key=lambda cell: (distance(unit.position, cell), cell))`，未对上一回合已分配的槽位做状态延续或滞后锁定（Slot Hysteresis / Stickiness）。当单位朝当前目标槽位 `[-1134, 1412]` 移动 1 格到达新坐标后，由于几何距离变化，另一个候选槽位 `[-1134, 1410]` 成为即时距离最短目标，导致寻路方向立刻反转；退回原位后又重新选中前者，在两格之间形成 100% 钟摆式左右跳跃振荡死锁。
- **处置动作**：
  1. 在 `arena_tactic/squad_coordination.py` 中引入编队槽位滞后锁定机制（`_SLOT_STICKINESS_BONUS = 1`）：在 `_safe_slots` 分配中优先读取上一回合分配的槽位（`_load_previous_formation_slots`），只要前次槽位仍然合法可用且距离优势未被超过 1 格以上，优先保持前次槽位锁定，消除高频抖动；
  2. 在 `arena_tactic/memory.py` 的 `_safe_objective_states` 中增加对 `squad_coordination.formation_slots` 的持久化与校验逻辑，确保跨回合和回放状态安全；
  3. 新增 `tests/test_formation_slot_oscillation.py`（共 12 项单元测试，覆盖 round-trip 存取、1 格位移槽位锁定、显著更佳槽位切换、多回合 A-B-A 振荡消除、障碍物阻挡降级与多单位防冲突），全部通过；
  4. 报警 HTML 邮件已成功发送至 709934831@qq.com 归档；
  5. 重启 Docker 容器加载最新代码生效，彻底解决编队重整槽位振荡。

### 2026-09-07 | Tick 233773 满编满仓载货工人回矿停滞与往返振荡 (CARGO_DELIVERY_STAGNATION / UNIT_OSCILLATION) 修复
- **现象**：巡检时间窗 Tick 233650..233773，系统处于 `ATTACK (攻坚模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 200/200 满仓。巡检检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及工人 `210c98aea2ef`，`[WARNING] UNIT_OSCILLATION (单位往返振荡)` 在核心位 `[-898, 1573]` 与门口 `[-899, 1573]` 之间以 18/20 的高频往复反转，以及 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。
- **根因分析**：
  1. 当前基地达到 40 满编人口，核心资源储量达 200 上限，已无法再生产单位或消耗资源（生命/护盾满值）；
  2. 在 `arena_tactic/strategy/workers.py` 的载货工人回矿逻辑中，当工人站在核心格上时因 `context.resource_space <= 0` 无法 DEPOSIT，触发 `core_capacity_full_vacate` 被腾退移出至门口相邻格；
  3. 工人到达门口相邻格后（distance == 1），旧逻辑未校验满编满仓状态，强行判定 `distance == 1` 并生成 MOVE 进入核心格；
  4. 进入核心格后再次判定满仓被踢出，形成“踏入核心 → 满仓腾退 → 门前再入 → 再次腾退”的 2-Tick 往复振荡与回矿停滞死循环。
- **处置动作**：
  1. 在 `arena_tactic/strategy/workers.py` 中增加核心满编满仓（`context.resource_space <= 0 and context.population >= config.max_population`）判断，当工人处于核心门口（distance <= 1）且核心无法容纳存矿时，跳过进入核心格的 MOVE 与 A* 寻路，就地保持等待（WAIT，理由 `cargo_doorstep_wait_for_entry`）；
  2. 在 `arena_tactic/planning/legacy.py` 中注册 `cargo_doorstep_wait_for_entry` 的等待分类；在前端 `frontend/src/domain/labels.ts` 补充其中文显示标签；
  3. 在 `tests/test_core_congestion.py` 中新增 3 项回归单测（满编满仓门前等待、余量恢复正常进入、多工人门口防振荡），全量通过；同时保持 `test_sdk_contract.py` SDK 契约不受影响；
  4. 报警 HTML 邮件成功发送至 709934831@qq.com 归档；
  5. 重启 Docker 容器加载最新代码生效，彻底消除满编满仓载货工人振荡。

### 2026-09-07 | Tick 238776 远征先锋移动争夺死锁 (MOVE_CONTESTED / INEFFECTIVE_STATIONARY) 与移动目标记录兜底修复
- **现象**：巡检时间窗 Tick 238636..238776，系统处于 `BEACON (信标模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 200/200 满仓。巡检检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`，远征先锋 `2aada0b86a43` 在坐标 `[-1257, -762]` 连续 115 个 Tick 原地移动失败（`failed_moves = 115`，失败原因全为 `MOVE_CONTESTED`），尝试向上移动到 `[-1257, -763]` 陷入长达 115 回合的原地顶牛死锁。
- **根因分析**：
  1. 目标格 `[-1257, -763]` 存在敌方单位或敌方同时尝试移入该格，触发 `UNIT_MOVE_FAILED`（原因码 `MOVE_CONTESTED`）；
  2. 在 `arena_tactic/memory.py` 中处理 `UNIT_MOVE_FAILED` 事件时，仅从 `unit_tasks` 中提取 `task["step"]` 作为 `attempted_cell` 记录到 `temporary_blocks` 冷却。但远征先锋等动态编组单位未在 `unit_tasks` 中记录持久化 `step`，导致 `attempted_cell` 始终为 `None`；
  3. 目标格未能加入 `temporary_blocks`（或 `obstacles`），寻路策略在后续回合因目标格曼哈顿距离最近且无障碍标记，持续重复向同一格移动碰撞，导致陷入 100+ 回合连续撞击的无法自愈死锁。
- **处置动作**：
  1. **应急干预**：通过 Command API 下发 `ASSIGN_TASK` 移动脱困指令；同时单位在 Tick 238773 识别到隐匿敌军转入 `SWEEP` 横扫打击命中目标格，打破物理僵局；
  2. **工程根治**：
     - 在 `arena_tactic/runtime.py` 中记录每个单位最新意图所保留的目标格 `next_memory.last_move_attempt[unit_id] = intent.reserved_cell`；
     - 在 `arena_tactic/memory.py` 中增加 `last_move_attempt` 属性与持久化回放支持，当 `UNIT_MOVE_FAILED` 发生且 `unit_tasks` 无 `step` 时，自动回退使用 `last_move_attempt` 中的目标格；
     - 确保 `MOVE_BLOCKED_TERRAIN`（永久加入 `obstacles`）和 `MOVE_CONTESTED` 等非地形失败（加入 `temporary_blocks` 冷却）均能正确对争夺格实施冷却避让，彻底杜绝此类无任务标记单位的反复碰撞顶牛死锁；
  3. **测试验证**：新增 `tests/test_move_failed_fallback.py`（8 项单元测试，覆盖无 task、空 step、非地形争夺冷却、地形永久障碍、多单位隔离与往复阻断等场景），全量单测 516 项 100% 通过；
  4. **告警闭环**：异常报警 HTML 邮件已成功发送至 709934831@qq.com；
  5. 重启 Docker 容器加载最新代码生效。

### 2026-09-08 | Tick 240347 口袋地形进攻模式下 MOVE 振荡态工兵级联疏散失效修复
- **现象**：巡检时间窗 Tick 240217~240347，系统处于 `ATTACK (攻击模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 196/200。检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及 10 名载货工兵连续多回合无法进入核心卸货；`[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)` 涉及守备先锋 `6ee037b7b2bc` 滞留在核心格 `[-898, 1573]`，执行 `patrol_route_blocked` -> WAIT。
- **根因分析**：
  1. 核心 `[-898, 1573]` 处于绝壁单通道口袋地形，东、北、南三面不可通行，西侧唯一通道是 `[-899, 1573]`；
  2. 此前实现的 `_yield_cargo_doorstep_for_combat` 与 `_cascade_corridor_evacuation` 仅硬编码匹配 `intent.action is ActionKind.WAIT`；
  3. 当战局进入 `ATTACK` 进攻模式时，工兵因走廊避让与回矿侧滑，生成的是 `ActionKind.MOVE`（reason 为 `yield_corridor_for_combat` 或 `return_cargo_to_core_sidestep`），导致原有的 WAIT 过滤器完全无法识别这些处于 MOVE 振荡态的工兵；
  4. 级联疏导无法介入，门口与外围工兵在通道两端来回振荡拉扯，核心格内的先锋无法走出，形成死锁。
- **处置动作**：
  1. **告警闭环**：第一时间生成 Markdown 全景战报并成功发送 HTML 报警邮件至 `709934831@qq.com`；
  2. **策略修复**：
     - 在 `arena_tactic/strategy/common.py` 的 `_yield_cargo_doorstep_for_combat` 中加入 `is_move_yield` 判定，匹配 reason 为 `yield_corridor_for_combat` 的 MOVE 态工兵；
     - 在 `_cascade_corridor_evacuation` 中加入 `is_move_oscillating` 判定，匹配 reason 为 `yield_corridor_for_combat` 或 `return_cargo_to_core_sidestep` 的 MOVE 态工兵，使级联疏导机制能够覆盖动态振荡中的工兵群；
  3. **测试验证**：在 `tests/test_high_density_corridor.py` 中新增 `test_cascade_evacuation_matches_move_oscillating_workers` 回归测试用例，全模块测试通过；
  4. **代码提交与服务重载**：按 auto-commit 规范提交代码至 main 分支，重启 Docker 容器加载最新战术策略生效。

- **现象**：巡检时间窗 Tick 240110~240120，系统处于 `ATTACK (进攻模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 196/200。检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及 10 名载货工人回矿停滞超 500 Ticks；`[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)` 涉及先锋 `6ee037b7b2bc` 滞留在核心格 `[-898, 1573]` 超 500 Ticks。
- **根因分析**：
  1. 核心 `[-898, 1573]` 处于绝壁单通道口袋地形（北、东、南三面障碍物，西侧 `[-899, 1573]` 为唯一通道）；
  2. 先锋 `6ee037b7b2bc` 滞留在核心格，西侧门口被工人占用，执行 `patrol_route_blocked` -> WAIT；
  3. 门前 `[-899, 1573]` 有 2 名载货工人因核心格有友军单位无法进库，执行 `cargo_doorstep_wait_for_entry` -> WAIT；
  4. 外围走廊（[-899, 1572], [-899, 1574], [-900, 1573], [-901, 1573]）塞满了另外 8 名载货工人，每格 2 人均已达到 friendly_occupancy 上限 2/2；
  5. 此前 Tick 239451 新增的 `_yield_cargo_doorstep_for_combat` 仅针对距离核心 1 格的工兵尝试向相邻格避让；但在此类高密度群聚场景下，门口工兵所有相邻候选格全被外围载货工兵塞满（2/2），`reservations.reserve` 预约全部失败，导致门口工兵无法向外避让，先锋无法走出核心格，形成高密度走廊双向群死锁。
- **处置动作**：
  1. **应急干预与告警**：通过 Command API 下发指令疏导外围工兵，并即时向 `709934831@qq.com` 投递 HTML 战况异常告警邮件；
  2. **工程根治**：
     - 在 `arena_tactic/strategy/common.py` 中新增 `_cascade_corridor_evacuation` 与 `_cascade_yield_outward` 级联疏散机制；
     - 当检测到战斗单位滞留核心格时，采用由外向内（优先扫描距离 3、再扫描距离 2）的外围走廊工兵退避机制，将外围工兵向走廊更深处或侧向空地腾退，打破 2/2 饱和堵塞，为门口（距离 1）工兵让出预约空间；
     - 门口工兵成功预约腾退后，核心格战斗单位成功执行 `deploy_sidestep` 走出核心格，全面打通回矿通道；
  3. **测试验证**：新增 `tests/test_high_density_corridor.py`（8 项单元测试，覆盖高密度走廊级联疏散、多方向腾退、防死胡同、防反向振荡等场景），全部 8 项测试全绿通过（总耗时 0.23s）；
  4. **服务生效**：按 auto-commit 规范提交代码至 main 分支，重启 Docker 容器加载最新战术策略生效。

- **现象**：巡检时间窗 Tick 239451，系统处于 `ECONOMY (经济模式)`，核心坐标 `[-898, 1573]`，人口 40 满编。巡检检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及 10 名载货工人连续多回合无法完成资源入库（DEPOSIT），全部滞留堵塞在核心门口外围通道；`[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)` 涉及先锋 `entity_6ee037b7b2bc` 位于核心格 `[-898, 1573]` 连续 60+ Ticks 执行 WAIT，状态原因为 `patrol_route_blocked`。
- **根因分析**：
  1. 核心 `[-898, 1573]` 处于绝壁单通道口袋地形，东、北、南三面不可通行，西侧唯一通道是 `[-899, 1573]`；
  2. 核心格 `[-898, 1573]` 已被 CORE 与先锋占满（容量 2/2），门口格子 `[-899, 1573]` 被 2 名载货工人占满（2/2，持续处于 `cargo_doorstep_wait_for_entry`）；
  3. 先锋要走出去执行 patrol，但西门格满载进不去，`_deploy_sidestep` 返回 None → `patrol_route_blocked`；
  4. 工人要进核心存矿，但核心格已满 2/2 → `cargo_doorstep_wait_for_entry`；
  5. `_evict_combat_from_core_for_cargo` 只尝试让先锋 sidestep，四周被工人占满则放弃；`_yield_cargo_delivery` 不会主动让门口工人退让让先锋出来。双方形成确定性对换死锁（Swap Deadlock）。
- **处置动作**：
  1. 在 `arena_tactic/strategy/common.py` 中新增 `_yield_cargo_doorstep_for_combat` 函数与 `_force_doorstep_yield` 辅助函数：当 `_evict_combat_from_core_for_cargo` 检测到战斗单位（VANGUARD/RANGER）在核心格且其 `_yield_cargo_delivery` sidestep 失败时，扫描意图列表中所有 `cargo_doorstep_wait_for_entry` 的门口载货工人，将其 WAIT 意图替换为向外退让的 MOVE 意图（`yield_corridor_for_combat`），从而释放门口格子，下一回合战斗单位可成功 `deploy_sidestep` 走出核心格，彻底破除死锁；
  2. 退让函数内置防死胡同检测（候选格至少需有 1 个非墙非核心出口）和 `prev_cell` 防振荡机制（5000 分惩罚），确保工人不会走入死胡同或在两格之间往返振荡；
  3. 新增 `tests/test_pocket_deadlock.py`（7 项单元测试，覆盖单通道口袋死锁疏散、无载货工人不触发、死胡同避让、游侠同样触发、防振荡、全封闭安全等待与完整场景验证），全量单测 529 项 100% 通过；
  4. 重启 Docker 容器加载最新代码生效。

### 2026-09-08 | Tick 241029 满仓满编载货工兵走廊过饱和堆叠退避与检测器豁免修复 (CARGO_DELIVERY_STAGNATION)
- **现象**：巡检时间窗 Tick 240909..241029，系统处于 `ATTACK (攻坚模式)`，核心坐标 `[-898, 1573]`，人口 40 满编，核心资源 200/200 满仓。巡检检出 `[CRITICAL] CARGO_DELIVERY_STAGNATION (载货工人回矿停滞)` 涉及 6 名载货工人，全部滞留堵塞在核心门口外围通道（两两重叠，容量达到 2/2 饱和）。
- **根因分析**：
  1. 核心满仓满编（资源 200/200，人口 40 满编），无法再执行资源入库（DEPOSIT），载货工人在核心门前就地安全等待（`cargo_doorstep_wait_for_entry`）；
  2. 旧策略未对门禁走廊待命工人数量进行密度控制，导致 6 名载货工兵全部挤在单通道咽喉走廊（[-899, 1573]、[-899, 1574]、[-898, 1575]），每格达到 2/2 容量饱和，严重堵塞核心要道；
  3. 巡检脚本 `scripts/tactical_inspector.py` 未对满仓满编合法安全待命场景做豁免，导致误报 CRITICAL 级异常。
- **处置动作**：
  1. 在 `arena_tactic/strategy/workers.py` 中引入门口载货工人计数器 `_doorstep_cargo_wait_count`：当核心满仓满编且门前格已有 ≥1 名满载工兵待命时，后续工兵通过 `_evacuate_doorstep_intent` 生成 `cargo_doorstep_saturated_disperse` 意图有序向外围退避，消除 2/2 咽喉走廊饱和堵塞；
  2. 在 `scripts/tactical_inspector.py` 中为 `CARGO_DELIVERY_STAGNATION` 规则增加满仓满编豁免判断：当核心处于满仓满编状态且载货工人在核心安全半径内待命时，豁免停滞异常；
  3. 更新/新增 `tests/test_core_congestion.py` 与 `tests/test_tactical_inspector.py` 单元测试，全量 534 项单测 100% 绿灯；
  4. 重启 Docker 容器加载最新战术策略生效。

### 2026-09-10 | Tick 252773 工兵记忆资源复查锁定与防振荡修复 (UNIT_OSCILLATION)
- **现象**：巡检时间窗 Tick 252652..252771，系统处于 `BEACON (信标争夺模式)`，人口 40 满编，核心资源 42/200。巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)` 涉及工兵 `1bc7ddcb39e8` 在坐标 `[-929, 1505]` 与 `[-930, 1505]` 之间 120 Ticks 内往返 116 次（周期 2 Ticks，净位移 0）；`[WARNING] EXPLORATION_STALL (迷雾探索停滞)` 涉及工兵大幅移动净位移≈0，且全局无可见资源持续 1767 Ticks，其余 11 名工兵因缺乏前沿探索点处于 `idle (no_resource_or_frontier)` 等待。
- **根因分析**：
  1. 工兵 `1bc7ddcb39e8` 执行 `reobserve_remembered_resource`（复查记忆中的资源点），目标为 `[-936, 1481]`；
  2. 旧策略在分配侦察目标（reconnaissance）时缺少类似资源开采的锁定保护机制（`_locked_resource_targets`），每 tick 重新执行匈牙利匹配与就近分配，当工兵在局部地形接近目标时，因相邻格代价抖动或局部动态避障，导致在两个格子之间陷入 2-Tick 周期性钟摆横跳（净位移为 0）；
  3. 任务数据结构中缺少对 `recon_since` 的保留支持，导致无法追踪侦察任务持续时间与施加锁定超时。
- **处置动作**：
  1. 在 `arena_tactic/models.py` 中为 `AgentConfig` 增加 `recon_target_grace_ticks: int = 8` 配置项；
  2. 在 `arena_tactic/memory.py` 的 `_safe_task` 允许字段列表中加入 `recon_since`，确保任务元数据在轮转反序列化与克隆时不丢失；
  3. 在 `arena_tactic/strategy/common.py` 的 `_record_unit_task` 中新增 `recon_since` 继承与初始化逻辑；
  4. 在 `arena_tactic/strategy/workers.py` 中实现 `_locked_recon_targets`，在侦察目标分配前优先对有效侦察窗口内的工兵保持目标锁定与 A* 路径可达性校验，从策略层消除 2 格周期往返振荡；
  5. 新增 `tests/test_recon_lock_and_oscillation.py` 单元测试，并通过全部 530 项核心单元测试（0 失败）；
  6. 提交至 main 分支，重启 Docker 容器加载最新战术策略生效。

### 2026-09-12 | Tick 261349 守备单位防守槽位与门前避让冲突修复 (UNIT_OSCILLATION)
- **现象**：巡检时间窗 Tick 261230..261349，系统处于 `ECONOMY (经济模式)`，核心坐标 `[-834, -577]`，人口 3，资源储量 3/15。巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`，先锋 `cb39bf380f0b` 在坐标 `[-835, -577]` 与 `[-835, -578]` 之间 120 Ticks 内往返反转 30 次（周期 2 Ticks，反转率 100%）。
- **根因分析**：
  1. 核心位于 `[-834, -577]`，左侧直接相邻格子 `[-835, -577]` 为核心的通行出入口（`passable_exits` / doorstep）；
  2. `_guard_slots` 函数将直接相邻的 `[-835, -577]` 分配给先锋作为守备目标 `guard_target`；
  3. 当先锋到达 `guard_target` 时，`_plan_vanguards`（及 `_plan_rangers`）的到达分支调用了 `_evacuate_doorstep_intent`，因其处于核心出入口将其强行驱避至相邻格 `[-835, -578]`；
  4. 先锋离开后下一 Tick 不在 `guard_target`，`core_guard` 寻路逻辑又将其拉回 `[-835, -577]`，两套战术目标互斥交替触发，造成 2-Tick 乒乓往返振荡。
- **处置动作**：
  1. **告警闭环**：第一时间生成 Markdown 战报并成功发送 HTML 报警邮件至 `709934831@qq.com`；
  2. **工程根治**：
     - 在 `arena_tactic/strategy/common.py` 的 `_guard_slots` 中明确排除所有核心直接相邻的通行出入口（`passable_exits`），从源头上杜绝防守槽位与核心大门重叠；
     - 在 `arena_tactic/strategy/vanguards.py` 与 `rangers.py` 中增加守备就位判断：当单位已到达其指定的 `guard_target` 时，以该防守槽位为权威站位，执行 `WAIT ("holding_defense_ring")`，不再触发 `_evacuate_doorstep_intent` 造成反弹；
  3. **单测验证**：在 `tests/test_strategy_regressions.py` 中新增 5 项单元测试（覆盖排除出入口、部分障碍排除、先锋就位防驱离、游侠就位防驱离、多 Tick 仿真防振荡），全量 558 项单测 100% 通过；
  4. **代码提交与服务重载**：按 auto-commit 规范提交代码至 main 分支，重启 Docker 容器加载最新策略生效。

### 2026-09-13 | Tick 268560 残血游侠避险撤退往返振荡与任务记录修复 (UNIT_OSCILLATION)
- **现象**：巡检时间窗 Tick 268429..268560，系统处于 `ATTACK (进攻攻坚模式)`，核心坐标 `[-822, -574]`，人口 21，资源储量 6/105。巡检检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`，残血游侠 `e52fd67a0abb` (HP=1) 在坐标 `[-1064, -645]` 与 `[-1064, -644]` 之间连续 120 Ticks 内往返反转 118 次（周期 2 Ticks，反转率 98.3%）。
- **根因分析**：
  1. 游侠 `e52fd67a0abb` 生命值为 1 点，触发 `critical_ranger_retreat (残血游侠回撤核心)`；
  2. 在 `arena_tactic/strategy/rangers.py` 中，`plan_ranger_intents` 处理 `ranger.hp == 1` 及 `_unit_needs_retreat_heal` 时直接 `continue` 跳出循环，漏掉了 `_record_unit_task`，导致该单位的任务状态与前序位置 `prev_cell` 从未被记忆持久化；
  3. `_return_to_core` 默认优先使用 `avoid_threats=True` 规划安全避险路径。当游侠向东北方向撤退行至威胁边缘时被判定不可进，转向南侧退避一格；后退脱离威胁边缘后下一 Tick 重新生成回撤路径再次北上，由于缺少 `prev_cell` 与振荡计数感知，陷入两格无限死循环钟摆振荡。
- **处置动作**：
  1. **告警闭环**：第一时间生成详细 Markdown 态势战报，成功向 `709934831@qq.com` 投递 HTML 报警邮件；
  2. **工程根治**：
     - 在 `arena_tactic/strategy/rangers.py` 中为 `critical_ranger_retreat` 和 `retreat_heal` 补齐 `_record_unit_task`，确保任务元数据与 `prev_cell` 正确写入 `memory.unit_tasks`；
     - 在 `arena_tactic/strategy/common.py` 的 `_return_to_core` 中加入防往返振荡感知机制：检测到单位连续向 `prev_cell` 往复移动（`oscillation_count >= 2`）时，直接跳过陷入局部极小的 safe 路径，主动降级为 `unsafe_fallback` 强行突破威胁边界破局；
  3. **单测验证**：新增 `tests/test_critical_retreat_oscillation.py`（7 项专项回归单测），全量单测全部通过；
  4. **代码提交与热重载**：代码提交至 main 分支，重启 Docker 容器加载最新战术策略生效。

### 2026-09-13 | Tick 269912 迷雾探索超时工兵停滞与敌方核心边缘视野模式震荡修复 (EXPLORATION_STALL / Mode Oscillation)
- **现象**：巡检时间窗 Tick 269779..269912，系统核心正常（坐标 `[-822, -574]`，HP/Shield 5/5），人口 20，核心资源 14/100。检出 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`（`no_resource_ticks` 达到 755+ Ticks，5 名工兵全部原地 WAIT `no_resource_or_frontier`），同时在 120 Ticks 窗口内发生 19 次模式横跳，呈现高频 `5 Ticks BEACON ↔ 5 Ticks ATTACK` 周期性模式震荡死锁。
- **根因分析**：
  1. **迷雾探索停滞**：大地图累计探索迷雾达到 34,799 格，障碍 60,405 格，frontier 边缘达 4,836 格。旧逻辑在 `_plan_workers` 中硬编码 `explore_deadline = min(deadline, perf_counter() + 0.05)`（仅 50ms 预算），单次 `memory.frontier()` 即消耗近 48ms，导致 `bounded_path_cost` 路径搜索立即判定超时全部中断，所有工人拿不到探索目标全部返回 `None` 并回退至 `no_resource_or_frontier` 永久原地死等；
  2. **模式震荡死锁**：敌方核心探明位于 `[-967, -578]`，处于巡逻兵视野交界边缘。一旦进入视野即触发 `ATTACK` 模式并维持 5 Ticks（`attack_exit_grace_ticks`）；5 Ticks 结束后核心脱离视野瞬间回落 `BEACON`；单位在 BEACON 走位又立即看见核心再次触发 ATTACK，形成 5 周期高频震荡死锁。
- **处置动作**：
  1. **告警闭环**：第一时间生成详细战况分析 Markdown 战报，成功向 `709934831@qq.com` 投递 HTML 告警邮件；
  2. **工程根治**：
     - 在 `arena_tactic/models.py` 中引入 `exploration_budget_ms: float = 200.0`（探索预算提升至 200ms）与 `attack_core_memory_ticks: int = 20`（敌方核心记忆缓冲 20 Ticks）；
     - 在 `arena_tactic/strategy/workers.py` 中重构前沿探索超时降级机制：当 A* 超时耗尽时，安全降级至扇区最佳几何前沿点（`top_geometric[0]`），确保工兵始终有方向前进，根除 `no_resource_or_frontier` 永久卡死；
     - 在 `arena_tactic/memory.py` 中增加 `enemy_core_last_seen_tick` 记忆追踪，并在 `arena_tactic/strategy/mode.py` 中引入 `enemy_core_recently_seen` 记忆阻尼，消除敌方核心边缘视野闪烁导致的 5-Tick 模式震荡；
  3. **单测验证**：新增 `tests/test_exploration_budget_and_attack_memory.py` 专项回归测试，全量 586 项单测 100% 通过；
  4. **代码提交与服务重载**：代码提交至 main 分支，重启 Docker 容器并验证服务健康就绪。

### 2026-09-14 | Tick 275759 前线侦察游侠视野边缘往返振荡与远距猎手寻路振荡修复 (UNIT_OSCILLATION)
- **现象**：巡检时间窗 Tick 275629..275748，系统处于 `ATTACK (进攻模式)`，核心坐标 `[-822, -574]`，HP/Shield 5/5 满值，人口 30，核心资源 51/150。检出 `[WARNING] UNIT_OSCILLATION (单位往返振荡)`，前线游侠 `ad28d81ea5d8` 在 120 Ticks 内发生 118 次 2 格往返振荡（坐标在 `[-1239, -691]` 与 `[-1238, -691]` 之间每 Tick 翻转调头），游侠 `e52fd67a0abb` 发生 78 次往返振荡。
- **根因分析**：
  1. **开火阵位跳变振荡**：在 `_ranger_staging_cell` 中，当游侠向目标移动 1 格后，新位置计算出的 staging cell 候选点因微小的曼哈顿距离变化，导致排序首位候选点在相邻两格之间反复跳变，缺少目标粘滞性（Staging Stickiness）；
  2. **超远距离巡逻寻路失败回退振荡**：游侠前突至敌方核心前线（距基地 400+ 格），当猎手目标处于数百格外时，A* 跨大范围迷雾寻路超时/失败，旧逻辑直接回退至 `_deploy_sidestep`，导致在相邻两格之间往复横跳。
- **处置动作**：
  1. **告警闭环**：第一时间生成 Markdown 全景战报并成功发送 HTML 报警邮件至 `709934831@qq.com`；
  2. **策略修复**：
     - 在 `arena_tactic/strategy/rangers.py` 的 `_ranger_staging_cell` 中引入 `prev_staging` 粘滞性评分加成（STAGING_STICKINESS），当上回合开火阵位仍为有效候选且火力优势与最佳候选相当（`prev_adv + 1 >= best_adv`）时优先锁定，平抑单步位移引发的 tiebreaker 翻转；
     - 引入 `_prev_staging_cell` 从单位任务记录中提取上回合阵位；
     - 在猎手前沿侦察中引入 `LONG_DISTANCE_HUNTER_FALLBACK`：当目标距离超出长途门槛（`long_distance_retreat_threshold`）时，采用带多层禁忌表（anti-oscillation taboo）的远距贪心推进 fallback，平稳推进行进，根除 `_deploy_sidestep` 导致的 2 格往返横跳；
  3. **单测验证**：新增 `tests/test_hunter_distant_fallback.py`（8 项专项回归单测），针对远距贪心推进、禁忌表防振荡、多方向避障等场景全面覆盖，全绿通过；
  4. **代码提交与服务重载**：代码已提交至 main 分支，热重载 Docker 容器加载最新战术策略生效。

### 2026-09-16 | Tick 284718 工兵探索前沿任务覆盖导致 attempt_tick 丢失与防死锁失效修复 (INEFFECTIVE_STATIONARY)
- **现象**：巡检时间窗 Tick 284598..284717，系统核心处于 `NORMAL`，HP/Shield 5/5 满值，坐标 `[-822, -574]`，人口 33，核心资源 42/165。检出 `[WARNING] INEFFECTIVE_STATIONARY (对象长期无效静止)`，工兵 `85b226a3681f` 在坐标 `[-750, -611]` 连续 120 Ticks 保持 `WAIT`（理由 `exploration_route_blocked`），移动失败 0 次，陷入完全静止死锁；伴随 `[WARNING] EXPLORATION_STALL (迷雾探索停滞)`。
- **根因分析**：
  1. 在 `arena_tactic/strategy/workers.py` 的 `_frontier_assignments` 中，构造分配结果时直接用新字典覆写 `memory.unit_tasks[unit_id]`，抹去了前序任务字典 `previous` 中的 `attempt_tick`、`prev_cell`、`failures` 等历史追踪字段；
  2. 当工兵探索移动受阻（`intent is None`）进入 `_record_unit_task` 时，因 `task` 中缺失 `attempt_tick`，每回合都被重新初始化为当前 `context.tick`；
  3. 导致 `_stuck_sidestep` 防死锁脱困机制中的门限检查 `context.tick - attempt_tick < _STUCK_THRESHOLD` 恒为真提前退出，原本应在连续 3 Ticks 卡顿时触发的侧滑脱困逻辑永久失效，工兵在障碍物边缘无限静止死锁。
- **处置动作**：
  1. **告警闭环**：第一时间生成详细 Markdown 报警战报，成功向 `709934831@qq.com` 发送 HTML 告警邮件；
  2. **即时应急干预**：通过 Command API 下发 `ASSIGN_TASK` 指令（`MOVE_TO_CELL` 疏导至相邻空闲格 `[-751, -610]`），Command Version: 2 成功被采纳为 `APPLIED`，工兵从 `[-750, -611]` 成功位移至 `[-751, -611]`，死锁即刻解除；
  3. **策略根治**：
     - 在 `arena_tactic/strategy/workers.py` 中的两处 `_frontier_assignments` 中改用 `task = dict(previous); task.update(...)`，完整保留 `attempt_tick`、`prev_cell` 等历史任务上下文；
  4. **单测验证**：新增 `tests/test_frontier_attempt_tick_preservation.py`（5 项针对 `attempt_tick` 继承、失败计数延续、死锁侧滑激活及未达阈值不误触发的回归测试），测试 100% 通过；
  5. **代码提交与服务重载**：按规范提交代码至 main 分支，重启 Docker 容器加载最新策略生效。










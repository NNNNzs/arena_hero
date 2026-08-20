# Arena Hero 自适应战略决策表

## 1. 决策总则

Agent 的长期目标是 Core 生存、稳定提交、资源增长和机会性战斗，而不是固定经济流或战斗流。每 Tick 的固定优先级是：

1. 生命周期安全。
2. Core 紧急防御。
3. 治疗恢复。
4. Worker 存取资源。
5. 当前合法攻击。
6. 生产。
7. Beacon。
8. 探索与 Core 迁移。

所有实体、敌人、资源、Beacon 状态和动作合法性来自当前 `Turn`。永久障碍和探索历史可以记忆；历史敌人不能生成攻击，历史资源只能生成重新侦察移动。

## 2. 战略模式

| 模式 | 进入条件 | 退出条件或迟滞 | 主要行为 |
| --- | --- | --- | --- |
| `RESPAWN` | 当前 Core 缺失 | 新 Turn 出现 Core | 不为任何历史对象生成动作 |
| `RECOVER` | Core 迁移中、HP 不满、护盾低于 3，或受伤 Unit 在静止 Core 格 | 恢复条件消失 | 迁移中等待；按原始 UUID 顺序预算 Unit 治疗；Core 优先治疗或紧急修盾 |
| `DEFEND` | 敌人本 Tick 可攻击 Core、进入 Manhattan 距离 4，或连续受击但攻击者不可见 | 距离迟滞和连续受击压力均解除 | 战斗 Unit 拦截/搜索，Worker 回 Core，暂停普通探索、巡逻和生产 |
| `ATTACK` | 敌 Core 可见、战斗 Unit 至少 3、己 Core 满 HP 且护盾至少 3 | 已在进攻时允许战斗 Unit 降至 2、护盾降至 2 | 优先摧毁敌 Core；无射线时进入射击阵位；Vanguard 向高价值目标推进 |
| `BEACON` | Core 满 HP、护盾至少 3、人口至少 6、无防守压力，Beacon 未由己方携带 | 已在 Beacon 模式时允许护盾降至 2 | 最近 Vanguard 携带；无 Vanguard 时 Core 可兜底；持有后不主动丢弃 |
| `ECONOMY` | 有可见资源、有 Worker 货物，或早期阵容未完成 | 条件消失 | 唯一资源分配、存入、恢复和生产 |
| `EXPLORE` | 无更高优先模式且早期阵容稳定 | 出现资源、威胁或更高价值目标 | Worker 探边界；战斗编组按近卫、巡逻和猎人分工 |

`DEFEND`、`ATTACK` 和 `BEACON` 使用不同的进入和保留阈值，避免边缘状态下每 Tick 来回切换。Core 迁移期间强制进入恢复语义，提交 `WAIT` 让四 Tick 迁移自然推进。

`CORE_DAMAGED` 只累计连续受击压力，不推断攻击者坐标。连续两 Tick 受击且当前无可见敌人时，战斗 Unit 前往 Core 周边轮换搜索槽位；连续三 Tick 后，仅当 Core HP 至少 3 且权威视野内存在空的相邻非资源格时，Core 才启动一次迁移脱离火力。已有迁移只自然推进；当前可见敌人（尤其明确近战威胁）会禁止这类盲迁移。Core 缺失或 `CORE_RESPAWNED` 会清空压力和旧防守任务。

## 3. 经济和阵容

早期阵容依次补到 3 Worker、2 Vanguard、1 Ranger。之后以 8/6/6 为成熟目标，按绝对缺口最大的兵种生产；相同缺口按 Worker、Vanguard、Ranger 顺序决定。默认人口上限为 20，因此不会进入第 21 个 Unit 的涨价区间。

生产价格始终调用 SDK `unit_cost(unit_type, population)` 预览。Core 格必须在己方 Unit 动作结算后留出出生槽位。生产安全储备为：

```text
max(5, Core 缺失 HP + 本 Tick 已规划 Unit 治疗成本)
```

治疗成本按每点缺失 HP 一资源估算，并按服务器实际使用的原始 UUID 顺序分配。Worker 同格存入可以在服务器结算时为后续治疗和 Core 动作补充资源，但本地生产判断保持保守。

## 4. Worker 与探索

Worker 有货时返回静止 Core；Core 迁移时前往其公开 `destination`，到达后等待。空载 Worker 使用障碍感知最短路径成本与当前可见资源做一对一匹配，避免多人追逐同一资源节点。

没有当前资源时，最多 1 名空载 Worker 重新侦察仍在雾中的历史资源观察，其余 Worker 立即按原始 UUID 稳定分配东、南、西、北四个扇区积极探索，避免整个采集队长期追逐旧坐标。历史资源格连续 2 个权威 Turn 重新进入视野但仍没有资源时，从 `resource_observations` 移除并冷却 8 Tick；雾中没有得到复查的回合不累计失败。资源在冷却期间重新可见时，以当前 Turn 为准立即清除失败与冷却并恢复采集。两个探索者优先覆盖相反方向，四个探索者覆盖完整四周。每个探索者保留自己的边界目标，不在每 Tick 重新争抢最近坐标；连续 6 Tick 后顺时针轮换扇区，当前扇区没有候选时也会轮换到下一个可探索扇区。

移动避开永久障碍、临时失败格、敌方占用和 Vanguard 威胁格，并服从两实体终点容量限制。计划会保存本 Tick 实际尝试的下一格；普通移动失败会将该格冷却 4 Tick 并立即轮换探索扇区，`MOVE_BLOCKED_TERRAIN` 会把该格升级为永久障碍，避免围着同一障碍反复试探。

## 5. 战斗编组与评分

默认战斗编组不让全部 Vanguard/Ranger 停在 Core 周围：按稳定 UUID 顺序保留最多 2 名 Vanguard 与 1 名 Ranger 为 `core_guard`（实际数量不足时自然降级），其余 Vanguard 为 `patrol`，在距 Core 5–8 格的东、南、西、北外围槽位每 6 Tick 轮换；其余 Ranger 为 `hunter`，前往不同的 7–10 格前沿槽位扩大合并视野。所有槽位均经当前障碍、敌方占用和两实体容量检查，无法前往时安全等待。Worker 的资源与扇区探索分配不受这套战斗编组替代。

敌情只来自当前 `visible_enemies`；玩家的当前视野是己方 Core 和全部 Unit 的合并视野。内存以脱敏 alias 保存至多 3 Tick 的轻量位置、上次可见 Tick、相对 Core 距离和连续接近次数，绝不把失视敌人当作目标或攻击事实。当前可攻击 Core 的敌人最高优先；否则同一敌人连续两帧可见且距离递减、现距 Core 不超过 8 格时，启动 `intercept`：从外围抽最多 2 Vanguard 和 1 Ranger 迎击/找射线，始终保留既定 Core 近卫。敌人远离或失视即取消临时拦截；敌 Core 可见时仍遵循 `ATTACK` 及其 `attack_exit_grace_ticks`，不会据历史位置攻击或与 Beacon 来回振荡。

这些默认数量、半径、轮换、拦截距离与历史上限均在 `AgentConfig`（`core_guard_*`、`patrol_*`、`hunter_*`、`intercept_*`、`enemy_track_ttl_ticks`）集中配置。

Ranger 只使用当前可见目标，并通过水平、垂直或精确 45 度射线、1 到 3 格距离和中间射击格障碍检查。对角射击只检查射线上的中间对角格，射线两侧的障碍不会阻挡；integer-supercover 仅用于视野遮挡。目标评分为：

```text
敌方 Core +100
本次可击杀 +40
当前可攻击己方 Core +30
每格 Manhattan 距离 -5
```

同分时使用原始 UUID 字节升序。Ranger 没有合法射线时，在防守或进攻模式前往距离目标 2 到 3 格的合法射击阵位。

Vanguard 对相邻目标格整体评分：敌方 Core 为 100，每个敌方 Unit 为 10；选择最高分格横扫。无相邻目标时，`intercept` 先靠近当前接近 Core 的目标，进攻模式靠近当前高价值敌人，否则按 `patrol` 或 `core_guard` 槽位行动。Ranger 有当前合法射击目标时先射击；没有目标时，`intercept` 前往合法射击位，否则按 `hunter` 前沿槽位或 `core_guard` 槽位行动。

HP 为 1 的战斗 Unit 默认返回 Core 治疗。若当前攻击能击杀目标或目标正在威胁己方 Core，则攻击优先。v1 不生成 `SELF_DESTRUCT` 或 `DROP_BEACON`。

## 6. Core 迁移

Core 只有在连续 8 个新 Tick 没有可见资源、没有可见敌人、HP 满、护盾至少 3、目标相邻格无障碍且无任何实体时，才向高价值探索边界启动迁移。启动后不重复提交迁移动作，也不自动取消；每 Tick 显式等待直至服务器完成或报告失败。

所有阈值集中在 `AgentConfig`。真实对局调参必须使用脱敏回放和离线指标，不直接在实时连接期间修改策略。

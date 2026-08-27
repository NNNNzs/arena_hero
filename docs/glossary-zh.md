# Arena Hero 术语中英对照表

> Agent 输出（巡检、告警、复盘、晨报、聊天回复）中出现的术语，首次出现必须按
> `English (中文)` 格式双语标注。新术语出现时由 Agent 追加到对应分区。

## 模式 (Modes)

| English | 中文 |
| --- | --- |
| ECONOMY | 经济模式（发展采矿） |
| COMBAT / WAR | 战斗模式 |
| DEFEND | 防守模式 |
| EXPLORE | 探索模式 |

## 单位与角色 (Units & Roles)

| English | 中文 |
| --- | --- |
| CORE | 核心（基地） |
| WORKER | 工人 |
| VANGUARD | 先锋 |
| RANGER | 游侠 |
| BEACON | 信标 |

## 动作 (Actions)

| English | 中文 |
| --- | --- |
| MOVE | 移动 |
| WAIT | 等待 |
| HARVEST / MINE | 采集/挖矿 |
| DEPOSIT | 回矿入库 |
| ATTACK | 攻击 |
| HEAL / REPAIR SHIELD | 治疗/修盾 |
| SPAWN / PRODUCE | 孵化/生产 |
| SELF_DESTRUCT | 自毁 |
| ASSIGN_TASK | 下发任务指令 |

## 告警代码 (Alert Codes)

| English | 中文 |
| --- | --- |
| CORE_UNDER_ATTACK | 核心正在遭受攻击 |
| HIDDEN_CORE_ATTACK | 视野外的核心遭袭（间接证据） |
| CORE_LOST_OR_RESPAWNED | 核心丢失或已重生 |
| CORE_MIGRATION_LOOP | 核心迁移陷入循环 |
| UNANSWERED_DAMAGE | 单位受击后无任何反击或规避响应 |
| UNIT_OSCILLATION | 单位往返振荡（移动死锁） |
| CARGO_DELIVERY_STAGNATION | 载货工人连续无法完成回矿 |
| DEPOSIT_FAILURES | 资源入库连续失败 |
| PRODUCTION_FREEZE | 兵营生产冻结 |
| INEFFECTIVE_STATIONARY | 对象长期原地等待或移动失败 |
| EXPLORATION_STALL | 迷雾探索停滞 |
| DEFENSE_DISENGAGED | 防守单位脱离交战 |
| BEACON_CARRIER_ISOLATED | 信标携带者被孤立 |
| DECISION_LATENCY_SPIKE | 决策延迟激增 |

## 策略任务标识 (Strategy task/reason)

| English | 中文 |
| --- | --- |
| explore_sector_frontier | 扇区前沿探索 |
| holding_defense_ring | 保持防御圈 |
| critical_vanguard_retreat | 先锋紧急撤退 |
| legacy_patrol | 常规巡逻 |
| fast_reroll | 开局极速自杀刷矿 |
| evade_threat_without_combat_roster | 无战斗编制时风筝规避威胁 |
| emergency_worker_rally_blocked | 紧急工人集结受阻 |
| resource_grace_ticks | 资源目标宽限回合 |
| sidestep | 侧滑解卡 |
| Ring Patrol | 环形巡逻 |
| patrol arc | 巡逻弧段 |
| intercept | 拦截 |
| recon escort | 侦察护航 |
| escort formation slot | 护航编队槽位 |
| expedition_cohesion_hold | 远征编队领先等待 |
| expedition_regroup | 远征编队重组 |
| expedition_regroup_pace_hold | 远征节奏单位等待重组 |
| expedition_regroup_slot_hold | 远征单位在重组槽位等待 |
| expedition_contact_hold | 远征编队接敌停止推进 |
| expedition_pickup_waits_for_escort | 信标拾取等待护卫到位 |
| expedition_formation_move | 远征编队队形移动 |
| expedition_formation_hold | 远征编队槽位等待 |
| expedition_formation_route_blocked | 远征编队路径受阻 |
| beacon_exfil_formation_move | 信标撤离编队移动 |
| beacon_exfil_cohesion_hold | 信标撤离领先等待 |
| beacon_exfil_regroup | 信标撤离编队重组 |
| beacon_exfil_core_repair | 信标撤离期间核心修盾 |
| beacon_secure_core_repair | 信标安全固守期间核心修盾 |
| ASSEMBLE | 信标护卫集结阶段 |
| PICKUP | 信标拾取阶段 |
| EXFIL | 信标护送撤离阶段 |
| SECURE | 信标基地固守阶段 |
| RECOVER | 信标失落恢复阶段 |
| Squad cohesion | 编组凝聚度 |
| Squad-level arbitration | 编组级动作仲裁 |
| Beacon security screen | 信标安全警戒圈 |
| Expanded mining escort | 扩展采矿护航 |

## 游戏机制 (Mechanics)

| English | 中文 |
| --- | --- |
| Tick / Turn | 回合 |
| Fog of war | 战争迷雾 |
| Vision | 视野 |
| Cargo | 载货 |
| Respawn | 重生 |
| Shield | 护盾 |
| Population cap | 人口上限 |
| Storage rule max(10, population * 5) | 核心储量规则：max(10, 人口×5) |

# Arena Hero 高级战术机制调研报告

> 调研时间: 2026-08-21
> 范围: Screeps World/Arena, Battlecode (MIT), Halite, Lux AI, microRTS, CodinGame 等网格战术竞技项目

---

## 一、相关开源项目与优秀实现

### 1. Screeps 生态
| 项目 | 链接 | 亮点 |
|------|------|------|
| **The International Open Source** | github.com/The-International-Screeps-Bot | 3576 commits, Quad/Duo编队, 跨玩家通信协议, 自动扩张 |
| **KasamiBot** | github.com/kasami/kasamibot | TypeScript, 难度可调, 完整经济+战斗自动化 |
| **screeps-cartographer** | github.com/glitchassassin/screeps-cartographer | 高级寻路+侦察系统 |
| **Screeps Combat Wiki** | wiki.screepspl.us/Combat | Duo/Quad/Platoon编队, 塔防战术, 核弹攻防 |

### 2. Battlecode (MIT)
| 项目 | 链接 | 亮点 |
|------|------|------|
| **Just Woke Up (2025冠军)** | battlecode.org postmortem | Tower Flickering, 对称性快速检测, SRP瓦片化 |
| **confused (2025亚军)** | battlecode.org postmortem | 核心哲学: 最小化单位空闲时间, Splasher微操 |
| **Om Nom (2025季军)** | battlecode.org postmortem | Nuke Towers (钱→油漆转换), Jinja模板代码生成 |
| **SPAARK (2025 HS冠军)** | github.com/erikji/battlecode25 | 智能SRP建造, 通信优化 |
| **ecoArcGaming/battlecode25** | github.com/ecoArcGaming/battlecode25 | Novice 2nd, Java实现 |

### 3. 其他重要参考
| 项目 | 亮点 |
|------|------|
| **Halite III** (Two Sigma/Kaggle) | 多人资源管理, 路径规划, 蜂群战术 |
| **Lux AI Season 3** (NeurIPS 2024) | 元学习, 部分可观察, 5局适应赛制 |
| **microRTS / Gym-µRTS** | GridNet动作空间, IMPALA+UPGO, 纯战术隔离环境 |
| **CodinGame Clash of Bots** | 多机器人矩形网格对战 |
| **Gladiabots** | 行为树AI编队对战 |

---

## 二、Arena Hero 当前实现分析

基于代码审查, 当前系统已具备:

### 已实现
- ✅ 行为树驱动的单位决策 (Vanguard/Ranger/Worker/Core)
- ✅ 确定性调度器 (DeterministicScheduler) + 租约机制
- ✅ 信标争夺生命周期 (ASSEMBLE → PICKUP → HOLD → RECOVER)
- ✅ 视野迷雾 + 探索记忆
- ✅ 核心迁移 (4-Tick)
- ✅ 采矿/存矿/造兵循环
- ✅ 威胁拦截目标选择
- ✅ 护卫/巡逻/猎人角色分配
- ✅ 预约表 (ReservationTable) 避免路径冲突
- ✅ 行为追踪 (DecisionTrace) 可观测性

### 当前行为树结构 (简化)
```
Vanguard: critical_retreat → sweep_adjacent → assignment_move → guard
Ranger:   critical_retreat → shoot_legal → assignment_move → guard
```

---

## 三、未考虑或可极大增强的高级战术机制

### 🔴 高优先级 (直接提升胜率)

#### 1. 集火协调 (Focus Fire Coordination)
**现状**: Ranger 独立选择目标 (最低HP优先), 无多单位协调
**改进**:
- 引入**目标分配系统**: 多个Ranger共享一个焦点目标, 确保1-tick击杀
- 计算每tick的总DPS vs 目标HP, 选择恰好能击杀的目标分配给多余单位
- **过杀避免** (Overkill Prevention): 如果目标已被足够火力覆盖, 转向次优目标
- 来源: Battlecode XSquare微操, Screeps focus fire

```python
# 伪代码: 集火分配
def assign_focus_targets(rangers, enemies):
    remaining_hp = {e.id: e.hp for e in enemies}
    assignments = {}
    for ranger in sorted(rangers, key=lambda r: r.id.bytes):
        # 选择当前剩余HP最高的可击杀目标
        viable = [e for e in enemies if remaining_hp.get(e.id, 0) > 0 
                  and shot_range(ranger.pos, e.pos, obstacles)]
        if viable:
            target = max(viable, key=lambda e: remaining_hp[e.id])
            assignments[ranger.id] = target
            remaining_hp[target.id] -= 1  # 每Ranger 1 damage
    return assignments
```

#### 2. 微操: 走A与风筝 (Kiting / Hit-and-Run)
**现状**: Ranger射击后不移动, Vanguard近战后不重新定位
**改进**:
- **Ranger风筝**: 射击后向远离敌人的方向移动, 保持3格射程优势
- **Vanguard走A**: 近战攻击后移动到敌人对角, 迫使敌人重新寻路
- **安全射击位置**: Ranger优先选择有障碍物掩护的位置射击
- 来源: Screeps Duo战术, RTS micro通用技巧

```python
# Ranger风筝逻辑
def ranger_kite(unit, enemies, obstacles):
    targets = get_legal_targets(unit, enemies, obstacles)
    if targets:
        shoot(target)
        # 射击后向远离最近敌人的方向移动
        nearest_enemy = min(enemies, key=lambda e: dist(unit.pos, e.pos))
        retreat_dir = away_from(unit.pos, nearest_enemy.pos)
        if can_move(unit, retreat_dir):
            move(unit, retreat_dir)
```

#### 3. 编队系统 (Formation System)
**现状**: 无编队概念, 各单位独立行动
**改进**:
- **Duo** (Vanguard+Ranger): Vanguard前排吸收伤害, Ranger后排输出
- **Quad** (2V+2R): 四单位锁步移动, 共享路径计算
- **攻击楔形**: 集结进攻时形成V字阵型, Vanguard在前
- **防御环**: Core周围形成环形防御, 覆盖所有方向
- 来源: Screeps Duo/Quad/Platoon, 军事战术

```python
# Duo编队
class DuoFormation:
    def __init__(self, vanguard_id, ranger_id):
        self.front = vanguard_id  # 前排: 吸引火力
        self.back = ranger_id     # 后排: 安全输出
    
    def move(self, direction):
        # 前排先动, 后排跟随
        move_unit(self.front, direction)
        move_unit(self.back, direction)  # 保持相邻
```

#### 4. 地形利用与掩体系统 (Terrain Exploitation)
**现状**: 障碍物仅用于寻路绕行和视线计算
**改进**:
- **掩体射击**: Ranger优先移动到障碍物旁射击, 利用障碍物阻挡敌人反击路线
- **隘口控制**: 识别地图上的chokepoint, 派Vanguard驻守
- **高地优势**: 优先占据视野开阔的位置
- **墙壁挡路**: 利用障碍物形成的口袋地形伏击敌人
- 来源: Battlecode路径权重优化, 军事战术

```python
def best_shooting_position(ranger, targets, obstacles):
    """选择有掩体的射击位置"""
    candidates = get_positions_in_range(ranger, targets, range=3)
    return max(candidates, key=lambda pos: 
        count_adjacent_obstacles(pos, obstacles)  # 越多掩体越好
    )
```

#### 5. 动态战略模式切换 (Dynamic Strategy Switching)
**现状**: 护卫/巡逻/猎人角色分配是静态的
**改进**:
- **经济优先模式**: 早期全力采矿扩张
- **压制模式**: 检测到敌人弱小时全军进攻
- **防守模式**: Core受威胁时收缩防线
- **信标争夺模式**: 信标出现时集中力量
- 根据**游戏阶段**、**资源状况**、**敌人行为**自动切换
- 来源: Battlecode "confused" 的游戏哲学, RTS通用宏观策略

---

### 🟡 中优先级 (显著提升战术深度)

#### 6. 威胁评估与预测系统 (Threat Assessment & Prediction)
**现状**: 仅基于当前位置判断威胁
**改进**:
- **敌人轨迹跟踪**: 记录敌人历史位置, 预测移动方向
- **威胁热力图**: 基于敌人可能出现的位置生成风险地图
- **突袭预警**: 检测敌人集结行为, 提前发出警报
- **对称性推断**: 利用已知地图特征推断敌人Core可能位置
- 来源: Battlecode对称性检测, Screeps入侵预警

```python
class ThreatPredictor:
    def predict_next_position(self, enemy_alias, current_pos):
        history = self.memory.enemy_tracks.get(enemy_alias, {})
        positions = history.get('positions', [])
        if len(positions) >= 2:
            # 线性外推
            dx = positions[-1][0] - positions[-2][0]
            dy = positions[-1][1] - positions[-2][1]
            return (current_pos[0] + dx, current_pos[1] + dy)
        return current_pos
```

#### 7. 诱饵与欺骗战术 (Bait & Deception)
**现状**: 无欺骗机制
**改进**:
- **诱饵Worker**: 用低价值Worker引诱敌人进入伏击圈
- **假撤退**: Vanguard假装撤退, 引诱敌人追击到Ranger射程内
- **声东击西**: 一部分兵力佯攻, 主力从另一方向突袭
- **Core假迁移**: 开始迁移后取消, 误导敌人判断Core位置
- 来源: 军事战术, RTS欺骗策略

#### 8. 资源经济优化 (Economic Optimization)
**现状**: Worker简单采矿/存矿循环
**改进**:
- **采矿路线优化**: Worker规划最优采矿路线, 减少空闲移动
- **资源预测**: 根据chunk配额和当前存量预测未来资源产出
- **造兵时机优化**: 在资源刚好足够时立即造兵, 避免资源闲置
- **Worker动态调配**: 根据当前需求动态调整采矿/建设Worker比例
- 来源: Screeps经济管理, Battlecode "Om Nom" 资源优先策略

#### 9. 信标高级战术 (Advanced Beacon Tactics)
**现状**: 信标有基本的ASSEMBLE/PICKUP/HOLD/RECOVER生命周期
**改进**:
- **信标诱饵**: 故意丢弃信标引诱敌人拾取, 然后伏击拾取者
- **信标接力**: 多单位轮换携带信标, 分散被集火风险
- **信标封锁**: 在信标周围形成防御圈, 阻止敌人接近
- **信标+Core联动**: 携带信标的单位与Core保持特定距离, 利用Core防御
- 来源: 信标机制独有, 无直接参考

#### 10. 精确伤害计算 (Damage Calculation Engine)
**现状**: 无战斗模拟系统
**改进**:
- 实现**战斗模拟器**: 在决策前模拟多tick的战斗结果
- 计算**DPS/HP比**: 评估每种单位组合的战斗效率
- **存活概率估算**: 预测单位在特定交战中的存活概率
- **交换比计算**: 评估主动交战是否划算
- 来源: Screeps combat simulator, XCOM AI

---

### 🟢 低优先级 (长期竞争力)

#### 11. 探索策略优化 (Exploration Strategy)
**现状**: 简单的随机探索 + 调度器分配
**改进**:
- **扇区扫描**: 将地图划分为扇区, 系统性扫描
- **优先级探索**: 优先探索敌人可能存在的方向
- **侦察兵专用**: 指定低HP单位专职侦察, 高HP单位保留战斗
- **探索记忆衰减**: 已探索区域随时间降低优先级
- 来源: Battlecode探索策略, Lux AI部分可观察

#### 12. 多线程决策 (Parallel Decision Making)
**现状**: 按UUID顺序串行决策
**改进**:
- 先计算所有单位的"意图", 然后全局优化分配
- 解决**死锁**: 多单位互相等待对方让路
- **优先级抢占**: 高优先级单位可以抢占低优先级单位的路径
- 来源: 多智能体路径规划 (MAPF) 研究

#### 13. 适应性学习 (Adaptive Learning)
**现状**: 静态配置参数
**改进**:
- 记录对战结果, 动态调整策略参数
- 识别对手的**行为模式** (如总是先造Ranger), 针对性反制
- **元策略**: 在多局对战中学习对手的习惯
- 来源: Lux AI元学习, 强化学习研究

#### 14. 自毁战术 (Self-Destruct Tactics)
**现状**: 有SELF_DESTRUCT但无战术运用
**改进**:
- **濒死自毁**: 单位HP=1时自毁, 防止敌人获得击杀统计
- **Worker清空**: 自毁前确保Worker已存矿, 减少资源损失
- **Core自毁迁移**: 在敌人即将摧毁Core时自毁, 选择更有利的重生位置
- 来源: Arena Hero规则允许, 无直接参考

#### 15. 通信协议 (Communication Protocol)
**现状**: 无跨单位通信
**改进**:
- 利用行为树的Blackboard事件作为隐式通信
- 共享**敌人位置**信息, 减少重复侦察
- 协调**进攻时机**, 同时发起攻击
- 来源: Screeps跨玩家通信协议, Battlecode通信

---

## 四、按优先级的实施建议

### Phase 1: 核心战斗力 (1-2周)
1. **集火协调** — 最高ROI, 直接提升击杀效率
2. **地形利用** — 利用现有obstacle_cells数据, 低成本实现
3. **威胁评估** — 利用现有enemy_tracks数据, 增强预测

### Phase 2: 战术深度 (2-3周)
4. **编队系统** — 需要重构Vanguard/Ranger协同逻辑
5. **风筝/走A** — 需要修改行为树增加射击后移动
6. **动态战略模式** — 需要状态机支持

### Phase 3: 高级机制 (3-4周)
7. **诱饵欺骗** — 需要行为树支持复杂战术序列
8. **经济优化** — 需要资源预测模型
9. **精确伤害计算** — 需要战斗模拟器

### Phase 4: 长期竞争力 (持续)
10. **适应性学习** — 需要对战数据积累
11. **多线程决策** — 需要MAPF算法
12. **通信协议** — 需要消息传递机制

---

## 五、关键参考代码

### Screeps Quad编队 (The International Bot)
```
核心思想: 4个creep保持1格距离锁步移动
- 路径计算考虑4个creep的碰撞
- 前排吸引火力, 后排输出/治疗
- 移动时保持菱形阵型
```

### Battlecode XSquare微操
```
核心思想: 每个格子计算最优行动
- 评估所有可能的移动+攻击组合
- 选择最大化伤害输出/最小化受伤的组合
- 被整个竞赛生态复制的"标准微操"
```

### Battlecode Tower Flickering (confused 2025)
```
核心思想: 动态摧毁/重建防御塔
- 积累资源超过阈值时摧毁防御塔
- 用资源建造更多经济塔
- 将"防御"转化为"经济优势"
```

---

## 六、与Arena Hero机制的独特结合点

Arena Hero有一些独特机制可以深度挖掘:

1. **Core迁移**: 利用4-Tick迁移窗口期作为战术节点, 在迁移中/完成后切换战略
2. **信标系统**: 信标携带者获得额外护盾上限, 可以作为"超级坦克"
3. **资源捕获**: 摧毁敌人Core后转移资源, 激励主动进攻
4. **动态价格**: 20+单位后价格递增, 需要精确控制人口
5. **视野迷雾**: 利用障碍物视线遮挡进行隐蔽接近
6. **格子双占**: 一格最多2个实体, 可以用"人墙"封锁关键位置

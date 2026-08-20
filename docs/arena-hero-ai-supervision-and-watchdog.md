# Arena Hero AI 监管与看门狗（Watchdog）架构设计与实施规范

更新时间：2026-08-20。本文档定义 `Arena Hero` 对战系统的 AI 宏观托管、突发情况接管与自动化看门狗（Watchdog）巡检机制。

---

## 1. 架构目标与分层理念

实时竞技与战术对战系统对时延要求极高（通常每回合决策窗口在 100ms~500ms），大模型（LLM）由于网络与推理延迟（秒级）无法直接参与每 Tick 的底层微操。因此系统采用 **双层混合智能架构（Hierarchical AI Architecture）**：

```
┌────────────────────────────────────────────────────────┐
│             AI 宏观监管与看门狗层 (Agent Layer)           │
│   • 战局态势研判与战略覆写 (Command API)                 │
│   • 定时体检、指标分析、振荡/死锁巡检 (Hermes Watchdog)   │
│   • 异常自愈 (Docker 重启 / 紧急制动 / Codex 补丁修复)   │
└───────────────────────────┬────────────────────────────┘
                            │ 异步 / 事件驱动 / 战略指令
┌───────────────────────────▼────────────────────────────┐
│             本地战术与执行引擎层 (Local Engine)          │
│   • 行为树引擎 (Worker, Vanguard, Ranger, Core Canary) │
│   • 确定性调度器与统一仲裁 (Scheduler & Arbitrator)     │
│   • 毫秒级寻路、微操与 WebSocket 权威提交              │
└────────────────────────────────────────────────────────┘
```

- **底层执行层（毫秒级）**：由本地 Python 运行时、状态机、行为树与调度器驱动，保证 0 延迟响应与规则安全。
- **上层监管层（秒级 / 定时 / 突发）**：由 Hermes Agent / 外部智能体通过观察 Trace/Audit 日志与 Command API，负责异常预警、战略指导与突发接管。

---

## 2. AI 宏观托管与突发接管机制

### 2.1 介入途径：Command API
系统依托 `arena_tactic/command_api.py` 与 `CommandQueue`，为外部 AI 提供经过鉴权与版本化控制的安全指令接口：

| 指令类型 | 适用场景 | 预期行为 |
| :--- | :--- | :--- |
| `START_CORE_MIGRATION` | 核心受袭 / 资源枯竭 / 占领新锚点 | 触发 Core 安全迁移序列（回收货工 -> 清空 cargo -> 阶段性迁跃） |
| `CANCEL_CORE_MIGRATION` | 迁移路径突发敌情 / 紧急中止 | 立即撤回正在排队的迁跃指令，恢复防守阵型 |
| `UPDATE_POLICY` | 局势转换（劣势防守 / 优势推进） | 切换全局策略姿态：`BALANCED`、`DEFENSIVE`、`ECONOMY`、`AGGRESSIVE` |
| `ASSIGN_TASK` | 突发重要目标指派 | 为指定单位分派高优先级任务（`HOLD_POSITION` / `RETREAT_TO_CORE` / `HARVEST_VISIBLE` / `MOVE_TO_CELL`） |
| `EMERGENCY_STOP` | 发现逻辑陷入恶性循环 / 产生非法动作 | 强制各单位下发显式 `WAIT`，停止主动行动以待人工或 AI 修复 |
| `RESUME_AUTO` | 异常排除后 | 解除紧急停机，重新激活行为树与调度器 |

### 2.2 契约与安全约束
1. **必须携带幂等键与版本校验**：写请求必须包含 `Idempotency-Key` 与 `If-Match: "<version>"`，防止重复执行与并发写冲突。
2. **权威 Turn 延迟确认**：Command 接收后先处于 `QUEUED` 状态，仅在本地引擎于下一次权威 Turn 执行且被官方服务器 `ACCEPTED` 后，才流转为 `APPLIED`。
3. **安全优先级抢占**：Core 濒死撤退等安全硬规则优先级始终高于人工/AI 派发的宏观任务。

---

## 3. 看门狗（Watchdog）巡检与体检体系

### 3.1 巡检维度与异常判定规则

| 巡检维度 | 数据来源 | 正常基线 | 异常触发阈值 / 判定标准 | 严重等级 |
| :--- | :--- | :--- | :--- | :--- |
| **服务连通性** | `GET /livez` | `running=true`, `connected=true` | `running=false` 或 `connected=false` | **CRITICAL** |
| **Tick 推进活性** | `GET /livez` | `last_tick` 持续递增 | 两次巡检间隔（如 15m）内 `last_tick` 增量为 0 | **CRITICAL** |
| **服务器拒绝** | `GET /livez` | `rejected == 0` | `rejected > 0` 且持续增加 | **WARN** |
| **决策耗时** | `decision-trace.jsonl` | `decision_ms < 200ms` | `decision_ms > 500ms`（接近超时窗口） | **WARN** |
| **单位移动振荡** | `decision-trace.jsonl` | 轨迹平滑推进 | 同一单位在最近 10 Tick 内在 2 个坐标点来回往复 >= 3 次 | **WARN** |
| **降级兜底频次** | `decision-trace.jsonl` | 行为树正常产出 | 出现 `FALLBACK_LEGACY` 或未预期 `WAIT` | **INFO / WARN** |

### 3.2 巡检脚本架构
- 脚本位置：`/root/project/arena_hero/scripts/watchdog.py`
- Hermes 执行入口：`/root/.hermes/scripts/arena_hero_watchdog.py`
- 运行机制：
  - 采集 `/livez` 实时状态。
  - 读取尾部 `decision-trace.jsonl`，抽取最近 N 个 Tick 的时延与单位轨迹。
  - 维护本地持久化检查点 `/tmp/arena_hero_watchdog_state.json`，计算 Tick 增速与重连增量。
  - 结构化输出运行报告；当探测到 `CRITICAL` 或 `WARN` 时，输出详细异常诊断并触发报警。

---

## 4. 异常自愈与响应阶梯

```
[ Watchdog 巡检探测到异常 ]
            │
            ├─► [连通性/进程卡死] ──► 自动执行 Docker 重启并等待 /livez 恢复
            │
            ├─► [逻辑死锁/振荡] ──► 通过 Command API 发送 RESUME_AUTO 或重置策略
            │
            └─► [代码级未捕获 Bug] ──► 抓取 Trace 现场日志 ──► 通知 Telegram ──► 派发 Codex 修复
```

1. **第一梯队（自动化自愈）**：
   - 进程假死/连接中断：由 Watchdog 或运维守护触发容器重启 `docker compose restart arena-hero`。
2. **第二梯队（战略重置）**：
   - 战术陷入死锁：通过 Command API 下发策略重置或指派解除阻塞。
3. **第三梯队（人机协同与代码自愈）**：
   - 重复出现的逻辑缺陷（如特定地形寻路死循环）：提取脱敏 Trace 样本，生成复现测试用例，调度 Codex (gpt-5.6-sol) 执行修复并验证容器内测试。

---

## 5. 运维与调度配置

- **巡检周期**：每 30 分钟（可根据需要调整为 15 分钟）。
- **告警通知通道**：Telegram 主会话 / 应急通知。
- **状态维护**：无异常时定期聚合战况报表，出现 WARN/CRITICAL 实时推送上下文详情。

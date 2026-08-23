# Arena Hero 哨兵介入机制改造（Phase 1：夜间勿扰修复 + 自更新检测）

## 背景

Arena Hero 巡检体系正在升级为「异常→Agent 介入→自迭代」闭环。本次改造由 Codex 完成代码部分，Hermes 负责验证与部署。

当前发现的问题与需求：

### 问题 1（必须修）：夜间勿扰时段导致哨兵漏报

`scripts/tactical_inspector.py` 的 `--alert-only` 分支在 quiet hours（23:00-08:00）直接 return 0（静默），即使存在 CRITICAL 异常也不输出。这会让 5 分钟看门狗整夜失明。

**修复要求**：
- 新增 CLI 参数 `--force-alert`（或 `--no-quiet-hours`）：跳过 quiet hours 静默逻辑，照常输出告警。
- CRITICAL 级异常不受 quiet hours 抑制，始终输出；WARNING 级维持现有静默行为。
- 保持向后兼容：不带新参数时行为不变。
- 为上述行为补单测（参考 tests/ 现有测试风格）。

### 问题 2（新增能力）：检测规则自更新支持

为后续 Agent 动态扩展检测范围打基础：

- 将告警代码注册表重构为模块级结构化数据（如 `ALERT_RULES: dict[str, AlertRule]`，含 code/severity/zh_label/描述/研判建议字段），替代散落的硬编码字符串。
- `render_alert_text` 与 JSON 输出从该注册表读取。
- 新增 `--list-rules` 参数：打印全部已注册检测规则的 code、severity、中文标签（供 Agent 运行时发现可用检测项）。
- 不改变现有 14 个检测项的判定逻辑本身。

## 项目约束（必须遵守）

1. 读项目 `AGENTS.md` 并遵守全部规范，特别是：
   - 战术决策保持在可测试的纯函数中
   - 术语双语标注规范
   - 禁止生成 SELF_DESTRUCT 相关代码
2. 单文件超长拆分偏好：若 tactical_inspector.py 改动后明显臃肿，可将告警注册表拆到独立模块（如 `scripts/alert_rules.py` 或 arena_tactic 包内），保持 import 兼容。
3. 测试命令：`python3 -m pytest tests/ -q`（或项目现有测试方式）；JS 测试用 `node --test tests/*.mjs`。改动必须全量通过。
4. 提交信息遵循仓库惯例（type: subject），一次性合并提交，不要留半成品文件。
5. 不要重启容器、不要 git push——Hermes 验证后再推。

## 验收标准

- [ ] `--force-alert` 下，quiet hours 内 CRITICAL 异常正常输出，WARNING 维持静默
- [ ] 非 quiet hours 行为与现在完全一致
- [ ] `--list-rules` 输出全部 14 条规则
- [ ] 全量测试通过
- [ ] 输出报告改动文件清单与关键 diff 摘要

## 执行方式

```bash
cd /vol1/project/arena_hero && codex exec --sandbox workspace-write '读 AGENTS.md 后按 docs/tasks/sentinel-intervention-upgrade.md 任务书执行'
```

后台运行（预计 10~20 分钟），完成后 Hermes 审查 diff、跑测试、验证三个验收点，再提交推送并更新 cron 配置。

---
name: supabase
description: "Arena Hero 项目级 Supabase 数据字典与查询规范，适用于本项目表结构、JSONB 状态、回放、事件、决策轨迹、报告和 Storage 元数据的安全核验。"
---

# Arena Hero Supabase 数据查询

本技能只补充 Arena Hero 项目的数据地图、字段边界和查询方法，不替代通用 Supabase skill、MCP 或 CLI。连接、登录、迁移、部署和平台级操作遵循官方 Supabase 能力；本技能默认执行只读查询。

## 项目边界

- 目标项目由项目 `.env` 的 `SUPABASE_PROJECT_REF` 指定；Access Token 使用同一文件中的 `SUPABASE_ACCESS_TOKEN`。不要把 project ref、表名或旧记忆当成当前环境的充分证明。
- CLI 查询远程数据库时使用 `supabase db query --linked --project-ref "$SUPABASE_PROJECT_REF" '<SELECT ...>'`。`--project-ref` 必须和 `--linked` 一起使用；版本差异先运行 `supabase db query --help`。
- 只读取变量名和必要的查询结果摘要；不打印 token、service role key、数据库密码、连接串、Authorization header 或含敏感内容的原始 JSONB。
- 当前仓库没有 `supabase/` migration 目录，也没有 Supabase SDK/SQL writer。不要据表名推断 tactic 是否正在写入这些表；需要业务语义时必须继续追查写入方、迁移或实际查询证据。

详细字段、索引和本次核验快照见 [references/schema.md](references/schema.md)。发现线上 schema 与该文件不一致时，以实际 `information_schema`/`pg_catalog` 查询为准，并更新该项目参考文件。

## 查询路由

先把问题归到一个明确的数据粒度，再选择查询对象：

| 问题 | 首选表 | 查询粒度 |
| --- | --- | --- |
| 当前 Agent 持久状态、资源和人口 | `public.arena_agent_state` | `id`/最新 `tick` |
| 某 Tick 的动作意图和接受结果 | `public.arena_decision_traces` | `tick` |
| 战斗、采集、运营和异常事件 | `public.arena_events` | `tick` + `type/category` |
| 可重放的权威态势快照 | `public.arena_replays` | 一个 `tick` |
| 汇总报告和指标 | `public.arena_reports` | `report_type` + 时间区间 |
| 文件容器和对象元数据 | `storage.buckets`、`storage.objects` | `bucket_id` + object path |

### 只读查询流程

1. 先说明 project ref、环境、时间/Tick 范围、目标表和需要的字段。
2. 先检查结构和索引，再写业务查询；默认不用 `SELECT *`，给所有查询加合理 `LIMIT`，大表先用时间或 Tick 索引过滤。
3. 对 `jsonb`、数组和可空字段区分“字段不存在”“字段为 null”“字段为空数组/对象”；先用 `jsonb_typeof`、`jsonb_object_keys` 或受限样本确认结构，再展开嵌套对象。
4. 查询结果报告实际过滤条件、返回行数、最大/最小 Tick 或时间和证据来源；不要把近似统计或显示层计数说成业务事实。

常用查询方向：

- 当前态势：从 `arena_agent_state` 读取 `tick`、`mode`、`core`、`population`、`resources`、`resource_capacity`，需要地图时再按需读取受限的 `explored_cells`、`obstacles`、`remembered_resources`、`squads`。
- 回放时间线：从 `arena_replays` 按 `tick DESC` 取窗口，比较 `accepted`、`decision_ms`、`events`、`intents` 和态势 JSONB；不要把回放中的 `intents` 当成已执行事实，先结合 `accepted` 和事件。
- 决策审计：从 `arena_decision_traces` 按 `tick DESC` 查询 `accepted`、`mode`、`decision_ms`、`intents`、`rejected`，需要因果解释时再关联同 Tick 事件。
- 事件统计：从 `arena_events` 用 `category`、`type`、Tick 区间过滤；`position` 是数组，`target`/`values` 是 JSONB，先确认实际 JSON 结构再展开。
- 报告查询：从 `arena_reports` 按 `report_type` 和 `created_at` 过滤；`metrics` 可做结构检查，`raw_content` 默认不读取。
- Storage：先查询 `storage.buckets` 的 bucket 元数据，再按 `bucket_id` 和 path 前缀查询 `storage.objects` 的 `name`、时间、大小/metadata 等元数据；不默认下载文件内容，也不查询对象正文来推断业务数据。

## 结构核验与安全边界

- 表结构核验使用 `information_schema.columns`；主键、唯一约束和索引使用 `pg_catalog`/`pg_indexes`；RLS 使用 `pg_policies`。不要只看应用类型或截图。
- 变更前检查现有索引：当前回放、事件和决策轨迹按 Tick 有索引，报告按类型/创建时间有索引，Storage 对象按 bucket/path 有索引；查询应沿用这些过滤条件。
- 本技能中的 schema 说明是项目级事实快照，不是业务字段语义的替代品。字段的真正含义必须以写入代码、迁移、SQL 或接口契约为证；无法证明时明确标记“语义未确认”。
- 只读任务不执行 `INSERT`、`UPDATE`、`DELETE`、DDL、RLS 修改、Storage 上传/删除、迁移推送或函数部署。用户明确授权写入时，仍要先列出受影响表、行范围、锁/耗时、幂等性和回滚方案。
- `SUPABASE_SERVICE_ROLE_KEY` 仅可在受保护的服务端流程使用，不能用于浏览器、技能文件、日志或 Git；本技能优先使用 Access Token + linked database query 或已授权 MCP。

## 交付格式

每次查询结束时说明：连接方式、目标 project ref 的来源、环境、查询表和粒度、实际过滤条件、返回规模、结构/权限发现、未执行的写操作，以及结果是否足以支持结论。若需要新的表、字段或 JSONB 语义，先更新 [references/schema.md](references/schema.md) 的核验记录，再继续业务分析。

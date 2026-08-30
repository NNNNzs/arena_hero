# Arena Hero Supabase Schema

本文档是 Arena Hero 项目的 Supabase 数据字典和查询入口。字段类型来自 `information_schema.columns`，主键/索引来自 `pg_catalog`；它描述数据库结构，不单独证明字段的业务语义或当前 tactic 的写入路径。

## 当前核验快照

核验时间：2026-08-30。目标 project ref 从项目 `.env` 的 `SUPABASE_PROJECT_REF` 读取，未写入本文档；查询通过 Supabase CLI 的 linked database query 完成，只读取元数据和聚合计数。

本次计数结果会随运行变化，仅用于定位数据量和选择查询策略：

| 表 | 行数 |
| --- | ---: |
| `public.arena_agent_state` | 1 |
| `public.arena_decision_traces` | 0 |
| `public.arena_events` | 10021 |
| `public.arena_replays` | 3525 |
| `public.arena_reports` | 443 |
| `storage.buckets` | 0 |
| `storage.objects` | 0 |

当前仓库没有 `supabase/` migration 目录或 Supabase writer；这些表的持久化来源需要另行核验，不能仅凭表名推断。

## Arena Hero 业务表

### `public.arena_agent_state`

当前唯一索引：`arena_agent_state_pkey`，主键 `id`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `character varying` | 否 |
| `tick` | `bigint` | 否 |
| `mode` | `character varying` | 否 |
| `core` | `jsonb` | 否 |
| `population` | `integer` | 否 |
| `resources` | `integer` | 否 |
| `resource_capacity` | `integer` | 否 |
| `explored_cells` | `jsonb` | 否 |
| `obstacles` | `jsonb` | 否 |
| `remembered_resources` | `jsonb` | 否 |
| `enemy_tracks` | `jsonb` | 是 |
| `squads` | `jsonb` | 是 |
| `scheduler_state` | `jsonb` | 是 |
| `updated_at` | `timestamp with time zone` | 是 |

### `public.arena_decision_traces`

索引：主键 `arena_decision_traces_pkey(id)`；`idx_arena_decision_traces_tick(tick DESC)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `bigint` | 否 |
| `tick` | `bigint` | 否 |
| `decision_ms` | `double precision` | 否 |
| `accepted` | `boolean` | 否 |
| `mode` | `character varying` | 否 |
| `intents` | `jsonb` | 否 |
| `rejected` | `jsonb` | 是 |
| `created_at` | `timestamp with time zone` | 是 |

### `public.arena_events`

索引：主键 `arena_events_pkey(id)`；`idx_arena_events_tick(tick DESC)`；`idx_arena_events_type(type)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `bigint` | 否 |
| `tick` | `bigint` | 否 |
| `type` | `character varying` | 否 |
| `category` | `character varying` | 否 |
| `position` | `ARRAY` | 是 |
| `target` | `jsonb` | 是 |
| `values` | `jsonb` | 是 |
| `count` | `integer` | 是 |
| `description` | `text` | 是 |
| `created_at` | `timestamp with time zone` | 是 |

### `public.arena_replays`

索引：主键 `arena_replays_pkey(tick)`；`idx_arena_replays_created_at(created_at DESC)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `tick` | `bigint` | 否 |
| `mode` | `character varying` | 否 |
| `core` | `jsonb` | 否 |
| `population` | `integer` | 否 |
| `resources` | `integer` | 否 |
| `resource_capacity` | `integer` | 否 |
| `units` | `jsonb` | 否 |
| `visible_enemies` | `jsonb` | 是 |
| `obstacle_cells` | `jsonb` | 是 |
| `resource_cells` | `jsonb` | 是 |
| `beacon` | `jsonb` | 是 |
| `events` | `jsonb` | 是 |
| `intents` | `jsonb` | 是 |
| `decision_ms` | `double precision` | 是 |
| `accepted` | `boolean` | 是 |
| `created_at` | `timestamp with time zone` | 是 |

### `public.arena_reports`

索引：主键 `arena_reports_pkey(id)`；`idx_arena_reports_type_created(report_type, created_at DESC)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `bigint` | 否 |
| `report_type` | `character varying` | 否 |
| `tick_start` | `bigint` | 是 |
| `tick_end` | `bigint` | 是 |
| `summary` | `text` | 是 |
| `metrics` | `jsonb` | 否 |
| `raw_content` | `text` | 是 |
| `created_at` | `timestamp with time zone` | 是 |

## Supabase Storage 元数据

### `storage.buckets`

索引：主键 `buckets_pkey(id)`；唯一索引 `bname(name)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `text` | 否 |
| `name` | `text` | 否 |
| `owner` | `uuid` | 是 |
| `created_at` | `timestamp with time zone` | 是 |
| `updated_at` | `timestamp with time zone` | 是 |
| `public` | `boolean` | 是 |
| `avif_autodetection` | `boolean` | 是 |
| `file_size_limit` | `bigint` | 是 |
| `allowed_mime_types` | `ARRAY` | 是 |
| `owner_id` | `text` | 是 |
| `type` | `USER-DEFINED` | 否 |
| `versioning_status` | `text` | 否 |

### `storage.objects`

索引：主键 `objects_pkey(id)`；唯一索引 `bucketid_objname(bucket_id, name)`；辅助索引 `idx_objects_bucket_id_name(bucket_id, name COLLATE "C")`、`idx_objects_bucket_id_name_lower(bucket_id, lower(name) COLLATE "C")`、`name_prefix_search(name text_pattern_ops)`。

| 字段 | PostgreSQL 类型 | 可空 |
| --- | --- | --- |
| `id` | `uuid` | 否 |
| `bucket_id` | `text` | 是 |
| `name` | `text` | 是 |
| `owner` | `uuid` | 是 |
| `created_at` | `timestamp with time zone` | 是 |
| `updated_at` | `timestamp with time zone` | 是 |
| `last_accessed_at` | `timestamp with time zone` | 是 |
| `metadata` | `jsonb` | 是 |
| `path_tokens` | `ARRAY` | 是 |
| `version` | `text` | 是 |
| `owner_id` | `text` | 是 |
| `user_metadata` | `jsonb` | 是 |
| `archived_at` | `timestamp with time zone` | 是 |
| `is_delete_marker` | `boolean` | 否 |
| `is_versioned` | `boolean` | 否 |

## 查询模板

以下模板仅用于只读检查，执行前替换项目 ref 并确认时间/Tick 范围：

```sql
-- 当前 Agent 状态，避免默认读取所有 JSONB
select id, tick, mode, population, resources, resource_capacity, updated_at
from public.arena_agent_state
order by tick desc
limit 10;

-- 回放窗口
select tick, mode, population, resources, resource_capacity, decision_ms, accepted, created_at
from public.arena_replays
where tick between :from_tick and :to_tick
order by tick;

-- 事件统计，不读取事件正文
select category, type, count(*) as event_count, min(tick) as first_tick, max(tick) as last_tick
from public.arena_events
where tick between :from_tick and :to_tick
group by category, type
order by last_tick desc, event_count desc;

-- JSONB 结构检查：只返回键名，不返回值
select distinct jsonb_object_keys(metrics) as metric_key
from public.arena_reports
where metrics is not null
limit 200;

-- Storage 文件清单只看元数据
select bucket_id, name, created_at, updated_at, metadata, user_metadata
from storage.objects
where bucket_id = :bucket_id
  and name like :path_prefix
order by name
limit 200;
```

`:from_tick`、`:to_tick`、`:bucket_id` 和 `:path_prefix` 是示意占位符；CLI 执行前必须改成安全的参数值，不能把它们原样交给 PostgreSQL。

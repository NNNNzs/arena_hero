import os
import glob
import json
import time
import psycopg2
from psycopg2.extras import execute_values
from pathlib import Path

env_file = Path("/vol1/project/arena_hero/.env")
db_url = ""
with open(env_file, "r", encoding="utf-8") as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            db_url = line.strip().split("=", 1)[1].strip("'\"")

if not db_url:
    print("❌ 未找到 DATABASE_URL")
    exit(1)

conn = psycopg2.connect(db_url)
cur = conn.cursor()
runtime_dir = Path("/vol1/project/arena_hero/runtime")

print("=== [1/2] 开始全量补录历史回放 (replay.jsonl*) ===")
replay_files = sorted(glob.glob(str(runtime_dir / "replay.jsonl*")))
cur.execute("SELECT tick FROM arena_replays;")
existing_replay_ticks = set(r[0] for r in cur.fetchall())
print(f"数据库当前已有 {len(existing_replay_ticks)} 帧回放")

all_missing_replays = []
for rf in replay_files:
    count_in_file = 0
    with open(rf, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                tick = r.get("tick")
                if tick is not None and tick not in existing_replay_ticks:
                    state = r.get("state", {})
                    bcn = json.dumps(state.get("beacon", {})) if state.get("beacon") else None
                    all_missing_replays.append((
                        tick,
                        str(r.get("mode", "NORMAL")),
                        json.dumps(state.get("core", {})),
                        state.get("population", 0),
                        state.get("resources", 0),
                        state.get("resource_capacity", 0),
                        json.dumps(state.get("units", [])),
                        json.dumps(state.get("visible_enemies", [])),
                        json.dumps(state.get("obstacle_cells", [])),
                        json.dumps(state.get("resource_cells", [])),
                        bcn,
                        json.dumps(r.get("events", [])),
                        json.dumps(r.get("intents", [])),
                        r.get("decision_ms", 0.0),
                        r.get("accepted", True)
                    ))
                    existing_replay_ticks.add(tick)
                    count_in_file += 1
            except Exception:
                pass
    print(f"  扫描 {os.path.basename(rf)}: 提取到 {count_in_file} 帧待同步")

print(f"-> 待插入历史回放总数: {len(all_missing_replays)} 帧")

replay_sql = """
INSERT INTO arena_replays (
    tick, mode, core, population, resources, resource_capacity,
    units, visible_enemies, obstacle_cells, resource_cells, beacon,
    events, intents, decision_ms, accepted
) VALUES %s
ON CONFLICT (tick) DO NOTHING;
"""

batch_size = 200
total_batches = (len(all_missing_replays) + batch_size - 1) // batch_size
for i in range(0, len(all_missing_replays), batch_size):
    batch = all_missing_replays[i:i+batch_size]
    t0 = time.time()
    execute_values(cur, replay_sql, batch, template="(%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)", page_size=200)
    conn.commit()
    batch_num = i // batch_size + 1
    if batch_num % 10 == 0 or batch_num == total_batches:
        print(f"  [Replays] 进度: {batch_num}/{total_batches} ({min(i+batch_size, len(all_missing_replays))}/{len(all_missing_replays)}), 本批耗时 {time.time()-t0:.2f}s")

print("=== [2/2] 开始全量补录决策追踪 (decision-trace.jsonl*) ===")
trace_files = sorted(glob.glob(str(runtime_dir / "decision-trace.jsonl*")))
cur.execute("SELECT tick FROM arena_decision_traces;")
existing_trace_ticks = set(r[0] for r in cur.fetchall())
print(f"数据库当前已有 {len(existing_trace_ticks)} 条决策追踪")

all_missing_traces = []
for tf in trace_files:
    count_in_file = 0
    with open(tf, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                t = json.loads(line)
                tick = t.get("tick")
                if tick is not None and tick not in existing_trace_ticks:
                    causality = t.get("causality", {})
                    mode_info = causality.get("mode", {})
                    mode_val = mode_info.get("mode", "NORMAL") if isinstance(mode_info, dict) else "NORMAL"
                    timings = t.get("timings", {})
                    decision_ms = timings.get("decision_ms", 0.0) if isinstance(timings, dict) else 0.0
                    validation = t.get("validation", [])
                    accepted = len(validation) == 0
                    
                    arbitration = t.get("arbitration", [])
                    entity_traces = t.get("entity_traces", [])
                    intents_data = json.dumps({"arbitration": arbitration, "entity_traces": entity_traces})
                    rejected_data = json.dumps(t.get("truncation", {}))
                    
                    all_missing_traces.append((
                        tick,
                        decision_ms,
                        accepted,
                        mode_val,
                        intents_data,
                        rejected_data
                    ))
                    existing_trace_ticks.add(tick)
                    count_in_file += 1
            except Exception:
                pass
    print(f"  扫描 {os.path.basename(tf)}: 提取到 {count_in_file} 条待同步")

print(f"-> 待插入决策追踪总数: {len(all_missing_traces)} 条")

trace_sql = """
INSERT INTO arena_decision_traces (
    tick, decision_ms, accepted, mode, intents, rejected
) VALUES %s;
"""

batch_size = 200
total_batches = (len(all_missing_traces) + batch_size - 1) // batch_size
for i in range(0, len(all_missing_traces), batch_size):
    batch = all_missing_traces[i:i+batch_size]
    t0 = time.time()
    execute_values(cur, trace_sql, batch, template="(%s, %s, %s, %s, %s::jsonb, %s::jsonb)", page_size=200)
    conn.commit()
    batch_num = i // batch_size + 1
    if batch_num % 5 == 0 or batch_num == total_batches:
        print(f"  [Traces] 进度: {batch_num}/{total_batches} ({min(i+batch_size, len(all_missing_traces))}/{len(all_missing_traces)}), 本批耗时 {time.time()-t0:.2f}s")

# 最终校验
print("\n=== 🎉 最终全量数据校验 ===")
cur.execute("SELECT count(*), min(tick), max(tick) FROM arena_replays;")
r_cnt, r_min, r_max = cur.fetchone()
print(f"  表 arena_replays: {r_cnt} 帧 | Tick: {r_min} ~ {r_max}")

cur.execute("SELECT count(*), min(tick), max(tick) FROM arena_decision_traces;")
t_cnt, t_min, t_max = cur.fetchone()
print(f"  表 arena_decision_traces: {t_cnt} 条 | Tick: {t_min} ~ {t_max}")

cur.close()
conn.close()
print("全部历史数据同步完成！")

import json
import os
import glob
import psycopg2
from psycopg2.extras import execute_values
from pathlib import Path

# Load connection string from /root/project/arena_hero/.env
env_file = Path("/root/project/arena_hero/.env")
db_url = ""
with open(env_file, "r", encoding="utf-8") as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            db_url = line.strip().split("=", 1)[1].strip("'\"")

if not db_url:
    print("Error: DATABASE_URL not found in .env")
    exit(1)

print(f"Connecting to database...")
conn = psycopg2.connect(db_url)
cur = conn.cursor()

runtime_dir = Path("/root/project/arena_hero/runtime")

print("=== 1. 同步 agent-state.json ===")
agent_state_file = runtime_dir / "agent-state.json"
if agent_state_file.exists():
    with open(agent_state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    sql = """
    INSERT INTO arena_agent_state (
        id, tick, mode, core, population, resources, resource_capacity,
        explored_cells, obstacles, remembered_resources, enemy_tracks, squads, scheduler_state, updated_at
    ) VALUES (
        'current', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
    ) ON CONFLICT (id) DO UPDATE SET
        tick = EXCLUDED.tick,
        mode = EXCLUDED.mode,
        core = EXCLUDED.core,
        population = EXCLUDED.population,
        resources = EXCLUDED.resources,
        resource_capacity = EXCLUDED.resource_capacity,
        explored_cells = EXCLUDED.explored_cells,
        obstacles = EXCLUDED.obstacles,
        remembered_resources = EXCLUDED.remembered_resources,
        enemy_tracks = EXCLUDED.enemy_tracks,
        squads = EXCLUDED.squads,
        scheduler_state = EXCLUDED.scheduler_state,
        updated_at = NOW();
    """
    cur.execute(sql, (
        data.get("tick", 0),
        data.get("mode", "NORMAL"),
        json.dumps(data.get("core", {})),
        data.get("population", 0),
        data.get("resources", 0),
        data.get("resource_capacity", 0),
        json.dumps(data.get("explored_cells", [])),
        json.dumps(data.get("obstacles", [])),
        json.dumps(data.get("remembered_resources", [])),
        json.dumps(data.get("enemy_tracks", {})),
        json.dumps(data.get("squads", {})),
        json.dumps(data.get("scheduler_state", {}))
    ))
    conn.commit()
    print("agent-state.json 成功同步到 arena_agent_state！")

print("=== 2. 同步 events.jsonl ===")
events_file = runtime_dir / "events.jsonl"
if events_file.exists():
    rows = []
    with open(events_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                pos = e.get("position")
                pos_val = [pos[0], pos[1]] if pos and len(pos) == 2 else None
                tgt = json.dumps(e.get("target")) if e.get("target") is not None else None
                rows.append((
                    e.get("tick", 0),
                    str(e.get("type", "UNKNOWN")),
                    str(e.get("category", "ops")),
                    pos_val,
                    tgt,
                    json.dumps(e.get("values", {})),
                    e.get("count", 1),
                    str(e.get("description", ""))
                ))
            except:
                pass
    
    if rows:
        sql = """
        INSERT INTO arena_events (tick, type, category, position, target, values, count, description)
        VALUES %s;
        """
        execute_values(cur, sql, rows, template="(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)", page_size=1000)
        conn.commit()
        print(f"events.jsonl 成功同步 {len(rows)} 条战术事件！")

print("=== 3. 同步 nightly / audit 报告 ===")
report_files = sorted(glob.glob(str(runtime_dir / "nightly_reports_*.jsonl")))
rep_rows = []
for rf in report_files:
    with open(rf, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rep = json.loads(line)
                rep_rows.append((
                    'nightly',
                    rep.get("tick_start", rep.get("start_tick", 0)),
                    rep.get("tick_end", rep.get("end_tick", 0)),
                    rep.get("summary", rep.get("message", "")),
                    json.dumps(rep.get("metrics", rep)),
                    line.strip()
                ))
            except:
                pass

if rep_rows:
    sql = """
    INSERT INTO arena_reports (report_type, tick_start, tick_end, summary, metrics, raw_content)
    VALUES %s;
    """
    execute_values(cur, sql, rep_rows, template="(%s, %s, %s, %s, %s::jsonb, %s)", page_size=500)
    conn.commit()
    print(f"战报报告 成功同步 {len(rep_rows)} 条！")

print("=== 4. 同步 replay.jsonl 关键回放帧 ===")
replay_file = runtime_dir / "replay.jsonl"
if replay_file.exists():
    r_rows = []
    with open(replay_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                state = r.get("state", {})
                bcn = json.dumps(state.get("beacon", {})) if state.get("beacon") else None
                r_rows.append((
                    r.get("tick", 0),
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
            except:
                pass
    
    # 抽取最近 3000 帧
    sample = r_rows[-3000:] if len(r_rows) > 3000 else r_rows
    if sample:
        sql = """
        INSERT INTO arena_replays (
            tick, mode, core, population, resources, resource_capacity,
            units, visible_enemies, obstacle_cells, resource_cells, beacon,
            events, intents, decision_ms, accepted
        ) VALUES %s
        ON CONFLICT (tick) DO NOTHING;
        """
        execute_values(cur, sql, sample, template="(%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)", page_size=500)
        conn.commit()
        print(f"replay.jsonl 成功同步 {len(sample)} 帧战局回放！")

print("=== 5. 校验数据库中的各表行数 ===")
tables = ["arena_agent_state", "arena_events", "arena_reports", "arena_replays", "arena_decision_traces"]
for t in tables:
    cur.execute(f"SELECT count(*) FROM {t};")
    cnt = cur.fetchone()[0]
    print(f"表 {t}: {cnt} 条记录")

cur.close()
conn.close()
print("🎉 全量迁移完成！")

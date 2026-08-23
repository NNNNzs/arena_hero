# Arena Hero Tactic

## Project overview

This repository contains a synchronous Python tactic for Arena Hero. The tactic
receives complete authoritative `Turn` states, queues at most one action per
controlled object, and submits one complete Agent plan for the current Tick.

## Directory index

| Path | Purpose |
| --- | --- |
| `tactic.py` | Minimal API key, SDK loop, submit, persistence, and safe-exit entrypoint |
| `arena_tactic/` | Perception, memory, navigation, strategy, validation, controller allocation, and observability |
| `tests/` | Strategy, SDK contract, property, replay, and performance tests |
| `docs/arena-hero-tactic-design.md` | Overall architecture, rules mapping, recovery, and design decisions |
| `docs/arena-hero-strategy.md` | Strategic modes, thresholds, scoring, economy, combat, and migration tables |
| `docs/arena-hero-runtime-testing.md` | Runtime, persistence, redacted replay, metrics, test, and live-run boundaries |
| `docs/arena-hero-service.md` | Docker Compose 24/7 operation and status endpoints |
| `pyproject.toml` | Python 3.11+ metadata and official SDK dependency |
| `Dockerfile` | Python runtime/dependency image; source/runtime paths are mounted in development |
| `docker-compose.yml` | Development source mount, restart policy, health port, and log rotation |
| `.agents/skills/arena-hero/` | Complete official Arena Hero skill and bundled references |
| `.env` | Local API credential; never commit this file |
| `.venv/` | Local Python virtual environment; never commit this directory |

## Development commands

```bash
source .venv/bin/activate
python3 -m compileall -q tactic.py arena_tactic tests
python3 -m pip check
python3 -m pytest -q
PYTHONPATH=.agents/skills/arena-hero python3 -m pytest -q .agents/skills/arena-hero/tests
```

Run the live tactic in the foreground only for debugging. For 24/7 development
operation, use Docker Compose; source code is bind-mounted into the container:

```bash
source .venv/bin/activate
python3 tactic.py
```

```bash
docker compose up -d --build
curl http://127.0.0.1:8787/livez
curl http://127.0.0.1:8787/status
docker compose logs -f --tail=100 arena-hero
```

After changing Python or Dashboard source, reload without rebuilding:

```bash
docker compose restart arena-hero
```

Rebuild only after changing `pyproject.toml`, the Dockerfile, or runtime
dependencies:

```bash
docker compose up -d --build
```

## Tactical invariants

- Read only the current `Turn`; never reuse controllers from an older Tick.
- Submit a complete plan once after queuing current-Turn actions.
- Treat `turn.resource_cells` as current visible resource nodes or cargo piles.
- Remembered invisible resources are reconnaissance hints only; harvest current visible cells only.
- When no resource is visible, Workers and one stable-roster Ranger keep persistent frontier targets across stable East/South/West/North sectors.
- Cool down failed move destinations and promote terrain-blocked destinations to permanent obstacles instead of retrying them.
- Do not invent Core, Unit, enemy, coordinate, resource, or action identifiers.
- If `turn.core is None`, submit no invented actions and wait for a later state.
- Keep the Core storage rule `max(10, population * 5)` intact.
- There is no per-Tick upkeep; preview production with SDK `unit_cost()`.
- Never generate `SELF_DESTRUCT` or proactively drop the Beacon in v1.
- Never print, commit, or include API keys, cookies, or authorization headers.

## Change boundaries

Keep tactical decisions in testable pure helpers where practical. The service
wrapper must remain a thin worker, health endpoint, and reconnect boundary; do
not add a web framework. Validate syntax, dependency health, image build, and
the live health endpoint separately from real-match verification.

## Language: bilingual output (中英双语输出)

All agent-facing scheduled tasks (cron jobs, watchdogs, hourly reviews, morning
reports) and any analysis/review output MUST present game terms, mode names,
alert codes, task/reason identifiers, and strategy terminology in BOTH English
and Chinese. Chinese translation is mandatory — never output an English-only
term the operator may not understand.

Rules:
- First occurrence format: `English (中文)` — e.g. `ECONOMY (经济模式)`,
  `DEFENSE_DISENGAGED (防守单位脱离交战)`, `explore_sector_frontier (扇区前沿探索)`.
- Reuse the translation table in `docs/glossary-zh.md` when present; extend it
  when a new term appears.
- WeChat notifications, email reports, and chat replies follow the same rule.

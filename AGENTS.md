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
| `pyproject.toml` | Python 3.11+ metadata and official SDK dependency |
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

Run the live tactic only when explicitly requested:

```bash
source .venv/bin/activate
python3 tactic.py
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

Keep tactical decisions in testable pure helpers where practical. Do not add a
framework or start a background service. Validate syntax and dependency health
before live play, and report live verification separately from local tests.

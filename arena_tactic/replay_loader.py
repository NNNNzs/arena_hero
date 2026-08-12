"""Rehydrate the project's redacted replay-v1 records for offline canaries.

The replay intentionally stores aliases rather than live UUIDs.  This loader
maps each alias to a deterministic *offline-only* UUID so current-Turn code can
be exercised without credentials, network access, or a possible live submit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from arena_hero import (
    BeaconStatus, ChampionBeacon, CoreState, CoreView, Direction, PlayerState,
    PlayerStatus, ResolutionEvent, TerrainView, Turn, UnitType, UnitView,
)


@dataclass(frozen=True, slots=True)
class FrozenRedactedReplay:
    """Immutable, validated replay-v1 input captured at one point in time.

    Records remain JSON text so every hydration creates separate record, state,
    and ``Turn`` object graphs for independent offline replay runs.
    """

    records: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.records)


def freeze_redacted_replay(path: Path, *, limit: int | None = None) -> FrozenRedactedReplay:
    """Capture valid records from one replay file as a read-only input snapshot."""
    return _freeze_redacted_replay_paths((path,), limit=limit)


def load_frozen_redacted_replay(snapshot: FrozenRedactedReplay) -> tuple[Turn, ...]:
    """Rehydrate fresh offline ``Turn`` values from a fixed replay snapshot."""
    return tuple(
        _turn_from_record(json.loads(record), sequence=index)
        for index, record in enumerate(snapshot.records)
    )


def load_redacted_replay(path: Path, *, limit: int | None = None) -> tuple[Turn, ...]:
    """Load valid replay-v1 lines into deterministic offline ``Turn`` values."""
    return load_frozen_redacted_replay(freeze_redacted_replay(path, limit=limit))


def _freeze_redacted_replay_paths(paths: tuple[Path, ...], *, limit: int | None) -> FrozenRedactedReplay:
    records: list[str] = []
    for path in paths:
        records.extend(_valid_redacted_replay_lines(path))
    if limit is not None:
        records = records[-limit:]
    return FrozenRedactedReplay(tuple(records))


def _valid_redacted_replay_lines(path: Path) -> list[str]:
    """Return validated JSON lines without retaining mutable decoded records."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("schema_version") == 1 and isinstance(item.get("state"), dict):
            records.append(line)
    return records


def load_redacted_replay_history(path: Path, *, limit: int | None = None) -> tuple[Turn, ...]:
    """Load replay rotation history oldest-to-newest without treating files as live state."""
    return load_frozen_redacted_replay(freeze_redacted_replay_history(path, limit=limit))


def freeze_redacted_replay_history(path: Path, *, limit: int | None = None) -> FrozenRedactedReplay:
    """Capture replay rotations oldest-to-newest as one read-only input snapshot."""
    history: list[Path] = []
    prefix = f"{path.name}."
    try:
        candidates = path.parent.iterdir()
    except OSError:
        candidates = ()
    for candidate in candidates:
        if not candidate.is_file() or not candidate.name.startswith(prefix):
            continue
        suffix = candidate.name.removeprefix(prefix)
        if suffix.isdigit() and int(suffix) > 0:
            history.append(candidate)
    paths = (*sorted(history, key=lambda item: int(item.name.removeprefix(prefix)), reverse=True), path)
    return _freeze_redacted_replay_paths(paths, limit=limit)


def _offline_uuid(alias: object, *, fallback: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"arena-hero-redacted-replay/{alias if isinstance(alias, str) else fallback}")


def _cell(value: object) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(type(axis) is int for axis in value):
        return value[0], value[1]
    return None


def _direction(start: tuple[int, int], destination: tuple[int, int]) -> Direction:
    delta = destination[0] - start[0], destination[1] - start[1]
    return {(0, -1): Direction.UP, (0, 1): Direction.DOWN, (-1, 0): Direction.LEFT, (1, 0): Direction.RIGHT}.get(delta, Direction.UP)


def _turn_from_record(record: dict, *, sequence: int) -> Turn:
    state = record["state"]
    core_data = state.get("core")
    core: CoreView | None = None
    if isinstance(core_data, dict) and (position := _cell(core_data.get("position"))) is not None:
        try:
            core_state = CoreState(core_data.get("state", "NORMAL"))
        except ValueError:
            core_state = CoreState.NORMAL
        destination = _cell(core_data.get("destination"))
        moving = core_state is CoreState.MOVING
        progress = _core_progress(record.get("events")) if moving else None
        core = CoreView(
            kind="CORE", id=_offline_uuid(core_data.get("id"), fallback=f"core-{sequence}"), controlled=True,
            owner_username="offline_replay", position=position, hp=int(core_data.get("hp", 5)),
            shield=int(core_data.get("shield", 5)), state=core_state,
            move_direction=_direction(position, destination) if moving and destination else None,
            move_progress=progress if moving else None, move_required_ticks=4 if moving else None,
            destination=destination if moving else None,
        )
    units = tuple(_unit(item, controlled=True, fallback=f"unit-{sequence}-{index}")
                  for index, item in enumerate(state.get("units", ())) if isinstance(item, dict))
    enemies = tuple(_object(item, controlled=False, fallback=f"enemy-{sequence}-{index}")
                    for index, item in enumerate(state.get("visible_enemies", ())) if isinstance(item, dict))
    beacon_data = state.get("beacon") if isinstance(state.get("beacon"), dict) else {}
    beacon_position = _cell(beacon_data.get("position")) or (0, 0)
    try:
        beacon_status = BeaconStatus(beacon_data.get("status")) if beacon_data.get("status") else None
    except ValueError:
        beacon_status = None
    beacon = ChampionBeacon(position=beacon_position, status=beacon_status,
                             carrier_id=_offline_uuid(beacon_data.get("carrier"), fallback=f"beacon-{sequence}") if beacon_data.get("carrier") else None)
    terrain = []
    resources = tuple(cell for item in state.get("resource_cells", ()) if (cell := _cell(item)) is not None)
    obstacles = tuple(cell for item in state.get("obstacle_cells", ()) if (cell := _cell(item)) is not None)
    if resources:
        terrain.append(TerrainView(kind="RESOURCE", positions=resources))
    if obstacles:
        terrain.append(TerrainView(kind="OBSTACLE", positions=obstacles))
    tick = int(record.get("tick", sequence))
    player = PlayerState(status=PlayerStatus.ACTIVE if core else PlayerStatus.RESPAWNING,
                         respawn_at_tick=None if core else tick + 1, resources=int(state.get("resources", 0)),
                         population=len(units), champion_beacon=beacon,
                         objects=(*((core,) if core else ()), *units, *enemies, *terrain),
                         events=tuple(_event(item, tick=tick, sequence=index) for index, item in enumerate(record.get("events", ())) if isinstance(item, dict)))
    return Turn(tick=tick, state=player, submitter=lambda _plan, _key: None)


def _core_progress(events: object) -> int:
    if not isinstance(events, list):
        return 1
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") in {"CORE_MOVE_PROGRESS", "CORE_MOVE_STARTED"}:
            values = event.get("values")
            if isinstance(values, dict) and type(values.get("progress")) is int:
                return values["progress"]
    return 1


def _unit(data: dict, *, controlled: bool, fallback: str) -> UnitView:
    try:
        unit_type = UnitType(data.get("unit_type"))
    except ValueError:
        unit_type = UnitType.WORKER
    return UnitView(kind="UNIT", id=_offline_uuid(data.get("id"), fallback=fallback), controlled=controlled,
                    position=_cell(data.get("position")) or (0, 0), hp=int(data.get("hp", 1)), unit_type=unit_type,
                    cargo=int(data.get("cargo", 0)) if controlled and unit_type is UnitType.WORKER else None)


def _object(data: dict, *, controlled: bool, fallback: str) -> CoreView | UnitView:
    if data.get("kind") == "CORE":
        position = _cell(data.get("position")) or (0, 0)
        return CoreView(kind="CORE", id=_offline_uuid(data.get("id"), fallback=fallback), controlled=controlled,
                        owner_username="offline_enemy", position=position, hp=int(data.get("hp", 5)),
                        shield=int(data.get("shield", 0)), state=CoreState.NORMAL)
    return _unit(data, controlled=controlled, fallback=fallback)


def _event(data: dict, *, tick: int, sequence: int) -> ResolutionEvent:
    return ResolutionEvent(event_id=_offline_uuid(data.get("id"), fallback=f"event-{tick}-{sequence}"), tick=tick,
                           event_type=str(data.get("type", "UNKNOWN")), reason_code=data.get("reason") if isinstance(data.get("reason"), str) else None,
                           actor_id=_offline_uuid(data.get("actor"), fallback=f"actor-{tick}-{sequence}") if data.get("actor") else None,
                           target_id=_offline_uuid(data.get("target"), fallback=f"target-{tick}-{sequence}") if data.get("target") else None,
                           position=_cell(data.get("position")), values=data.get("values") if isinstance(data.get("values"), dict) else {})

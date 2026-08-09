from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from arena_hero import (
    BeaconStatus,
    ChampionBeacon,
    CoreState,
    CoreView,
    Direction,
    PlayerState,
    PlayerStatus,
    ResolutionEvent,
    TerrainView,
    Turn,
    UnitType,
    UnitView,
)


MAX_HP = {
    UnitType.WORKER: 2,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 2,
}


def uuid(value: int) -> UUID:
    return UUID(int=value)


def unit(
    value: int,
    unit_type: UnitType,
    position: tuple[int, int],
    *,
    hp: int | None = None,
    cargo: int = 0,
    controlled: bool = True,
) -> UnitView:
    return UnitView(
        kind="UNIT",
        id=uuid(value),
        controlled=controlled,
        position=position,
        hp=MAX_HP[unit_type] if hp is None else hp,
        unit_type=unit_type,
        cargo=cargo if controlled and unit_type is UnitType.WORKER else None,
    )


def core(
    *,
    value: int = 100,
    position: tuple[int, int] = (0, 0),
    hp: int = 5,
    shield: int = 5,
    state: CoreState = CoreState.NORMAL,
    controlled: bool = True,
) -> CoreView:
    moving = state is CoreState.MOVING
    return CoreView(
        kind="CORE",
        id=uuid(value),
        controlled=controlled,
        owner_username="bot_owner" if controlled else "enemy_bot",
        position=position,
        hp=hp,
        shield=shield,
        state=state,
        move_direction=Direction.RIGHT if moving else None,
        move_progress=1 if moving else None,
        move_required_ticks=4 if moving else None,
        destination=(position[0] + 1, position[1]) if moving else None,
    )


def event(
    value: int,
    event_type: str,
    *,
    tick: int = 1,
    reason_code: str | None = None,
    position: tuple[int, int] | None = None,
    actor_id: UUID | None = None,
    target_id: UUID | None = None,
    values: dict | None = None,
) -> ResolutionEvent:
    return ResolutionEvent(
        event_id=uuid(value),
        tick=tick,
        event_type=event_type,
        reason_code=reason_code,
        actor_id=actor_id,
        target_id=target_id,
        position=position,
        values=values,
    )


def turn(
    *,
    tick: int = 1,
    owned_core: CoreView | None = None,
    units: Iterable[UnitView] = (),
    enemies: Iterable[CoreView | UnitView] = (),
    resources: int = 0,
    resource_cells: Iterable[tuple[int, int]] = (),
    obstacle_cells: Iterable[tuple[int, int]] = (),
    events: Iterable[ResolutionEvent] = (),
    beacon_position: tuple[int, int] = (20, 20),
    beacon_status: BeaconStatus | None = None,
    beacon_carrier_id: UUID | None = None,
) -> Turn:
    units = tuple(units)
    enemies = tuple(enemies)
    resource_cells = tuple(resource_cells)
    obstacle_cells = tuple(obstacle_cells)
    terrain = []
    if resource_cells:
        terrain.append(TerrainView(kind="RESOURCE", positions=resource_cells))
    if obstacle_cells:
        terrain.append(TerrainView(kind="OBSTACLE", positions=obstacle_cells))
    objects = (
        *((owned_core,) if owned_core else ()),
        *units,
        *enemies,
        *terrain,
    )
    state = PlayerState(
        status=PlayerStatus.ACTIVE if owned_core else PlayerStatus.RESPAWNING,
        respawn_at_tick=None if owned_core else tick + 1,
        resources=resources,
        population=len(units),
        champion_beacon=ChampionBeacon(
            position=beacon_position,
            status=beacon_status,
            carrier_id=beacon_carrier_id,
        ),
        objects=objects,
        events=tuple(events),
    )
    return Turn(tick=tick, state=state, submitter=lambda plan, key: None)

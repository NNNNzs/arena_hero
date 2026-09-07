from arena_tactic.memory import _resolve_unit_task, _resolve_and_pop_unit_task


def test_resolve_unit_task_direct_and_aliased():
    tasks = {
        "raw_uuid_1234": {"kind": "explore", "step": [10, 20]},
        "entity_5678abcd": {"kind": "squad_evasion", "target": [30, 40]},
    }

    # Direct match by raw key
    key, task = _resolve_unit_task(tasks, "raw_uuid_1234")
    assert key == "raw_uuid_1234"
    assert task is not None and task["kind"] == "explore"

    # Match raw UUID to entity_ alias
    key, task = _resolve_unit_task(tasks, "5678abcd")
    assert key == "entity_5678abcd"
    assert task is not None and task["kind"] == "squad_evasion"

    # Miss
    key, task = _resolve_unit_task(tasks, "unknown_9999")
    assert key is None
    assert task is None


def test_resolve_and_pop_unit_task():
    tasks = {
        "entity_5678abcd": {"kind": "squad_evasion", "target": [30, 40]},
    }
    popped = _resolve_and_pop_unit_task(tasks, "5678abcd")
    assert popped is not None and popped["kind"] == "squad_evasion"
    assert "entity_5678abcd" not in tasks

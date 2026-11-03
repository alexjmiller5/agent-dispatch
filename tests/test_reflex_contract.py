import os
import runpy
import pytest
from pathlib import Path
from dataclasses import replace
from datetime import datetime, UTC
from agent_dispatch import dispatch as consumer
from test_dispatch import dispatcher
from fakes import FakeHub

reflex = Path(os.environ.get("REFLEX_REPO", ""))
pytestmark = pytest.mark.skipif(
    not os.environ.get("REFLEX_REPO"), reason="set REFLEX_REPO for real producer/consumer contract"
)


def test_real_reflex_occurrence_waits_for_due_and_dependency(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(reflex / "src"))
    producer = runpy.run_path(str(reflex / "tests/test_soma_dispatch.py"))
    hub, state = producer["Hub"](), {}
    base = producer["spec"]()
    spec = replace(
        base,
        templates=tuple(
            replace(t, lane="AI", due_offset_days=i * 7) for i, t in enumerate(base.templates)
        ),
    )
    config = {
        **producer["CONFIG"],
        "columns": {
            "title": "title",
            "status": "status",
            "due": "due_date",
            "completed": "closed_date",
            "tags": "tags",
            "priority": "priority",
            "notes": "notes",
            "links": "links",
            "blocked_by": "blocked_by_ids",
            "lane": "lane",
            "dispatch": "dispatch",
        },
    }
    producer["run"](hub, state, spec, config)
    fake = FakeHub(hub.rows.values())
    parent = next(r for r in fake.rows.values() if not r.get("blocked_by_ids"))
    child = next(r for r in fake.rows.values() if r.get("blocked_by_ids"))
    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr(consumer, "datetime", Frozen)
    d, herdr, _ = dispatcher(tmp_path, fake)
    d.reconcile()
    assert parent["dispatch"] == "Started"
    assert child["dispatch"] == "Requested"
    assert herdr.verbs().count("tab create") == 1
    clock[0] = datetime(2026, 1, 8, 12, tzinfo=UTC)
    d.reconcile()
    assert child["dispatch"] == "Requested"
    fake.edit(parent["id"], status="Completed")
    d.reconcile()
    d.reconcile()
    assert child["dispatch"] == "Started"
    assert herdr.verbs().count("tab create") == 2

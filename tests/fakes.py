"""In-process fakes: the Soma hub routes the job uses (durable-pull-v1, projected
pull, revision-checked patch) and the Herdr CLI."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from agent_dispatch.herdr import HerdrError

SUB = "1d6f0a52-8c1e-4b0e-9a57-3e2f4c6b7d80"
URL = "https://hub.example"
TOKEN = "fixture-token"
_clock = datetime(2026, 10, 12, tzinfo=UTC)


def stamp() -> str:
    global _clock
    _clock += timedelta(milliseconds=1)
    return _clock.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_clock.microsecond // 1000:03d}Z"


class FakeHub:
    def __init__(self, rows=()):
        self.rows = {r["id"]: self.row(**r) for r in rows}
        self.batches: list[dict] = []
        self.acks: list[str] = []
        self.patches: list[dict] = []
        self.conflicts = 0  # next N patches lose to a concurrent edit
        self.status = None  # force every response to this status

    @staticmethod
    def row(**values):
        base = {
            "title": "a task",
            "lane": "AI",
            "status": "To Do",
            "dispatch": None,
            "notes": None,
            "deleted_at": None,
        }
        t = stamp()
        return {**base, "updated_at": t, "hub_at": t, **values}

    def edit(self, row_id, **values):
        t = stamp()
        self.rows[row_id].update(values, updated_at=t, hub_at=t)

    def request(self, row_id, title="a task", **values):
        """A task arriving with dispatch Requested, plus the event it records."""
        if row_id in self.rows:
            self.edit(row_id, dispatch="Requested", **values)
        else:
            self.rows[row_id] = self.row(id=row_id, title=title, dispatch="Requested", **values)
        seq = str(len(self.acks) + len(self.batches) + 1)
        event = {
            "id": f"e{seq}",
            "seq": seq,
            "operation": "insert",
            "source": {"table": "tasks", "row_id": row_id},
            "changes": [{"column": "dispatch", "old_value": None, "new_value": "Requested"}],
        }
        self.batches.append(
            {
                "subscription_id": SUB,
                "delivery_id": f"d{seq}",
                "through_seq": seq,
                "events": [event],
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.status:
            return httpx.Response(self.status, json={"error": "x"})
        path = request.url.path
        if path == f"/v1/subscriptions/{SUB}/events":
            if not self.batches:
                empty = {"subscription_id": SUB, "delivery_id": None, "through_seq": "0"}
                return httpx.Response(200, json={**empty, "events": []})
            return httpx.Response(200, json=self.batches[0])
        if path == f"/v1/subscriptions/{SUB}/ack":
            delivery = json.loads(request.content)["delivery_id"]
            assert self.batches and self.batches[0]["delivery_id"] == delivery
            self.acks.append(self.batches.pop(0)["delivery_id"])
            return httpx.Response(200, json={"acked_seq": "1"})
        if path == "/v1/rows/pull":
            return self.pull(json.loads(request.content))
        if path == "/v1/rows/patch":
            return self.patch(json.loads(request.content))
        return httpx.Response(404)

    def pull(self, body):
        assert body["table"] == "tasks" and 1 <= body["limit"] <= 200
        rows = sorted(self.rows.values(), key=lambda r: r["id"])
        rows = [r for r in rows if all(r.get(k) == v for k, v in body.get("where", {}).items())]
        rows = [r for r in rows if r["id"] > body.get("after", "")][: body["limit"]]
        out = [{c: r.get(c) for c in body["columns"]} for r in rows]
        cursor = rows[-1]["id"] if len(rows) == body["limit"] else None
        return httpx.Response(200, json={"rows": out, "next_cursor": cursor})

    def patch(self, body):
        assert set(body["values"]) <= {"dispatch", "notes"}, body["values"]
        row, revision = self.rows.get(body["id"]), body["expected_revision"]
        if self.conflicts:
            self.conflicts -= 1
            self.edit(body["id"], title=row["title"])  # someone else saved the row
            return httpx.Response(409, json={"error": "revision_conflict"})
        if (
            row is None
            or row["deleted_at"]
            or (row["updated_at"], row["hub_at"]) != (revision["updated_at"], revision["hub_at"])
        ):
            return httpx.Response(409, json={"error": "revision_conflict"})
        self.patches.append({"id": body["id"], **body["values"]})
        self.edit(body["id"], **body["values"])
        return httpx.Response(200, json={"id": row["id"], "revision": {}})


class FakeHerdr:
    """Records every herdr call; `fail` maps 'group verb' to the error it raises."""

    def __init__(self, agents=None, fail=None, hooks=None):
        self.calls: list[tuple] = []
        self.hooks = dict(hooks or {})  # 'group verb' -> called before answering
        self.agents = dict(agents or {})  # name -> pane
        self.fail = dict(fail or {})
        self.tabs = 0

    def __call__(self, *args):
        self.calls.append(args)
        verb = " ".join(args[:2])
        if verb in self.hooks:
            self.hooks[verb]()
        if verb in self.fail:
            raise self.fail[verb]
        if verb == "tab create":
            self.tabs += 1
            return {
                "result": {
                    "tab": {"tab_id": f"w1:t{self.tabs}"},
                    "root_pane": {"pane_id": f"w1:p{self.tabs}"},
                }
            }
        if verb == "agent get":
            if args[2] not in self.agents:
                raise HerdrError("agent_not_found", args[2])
            return {"result": {"agent": {"name": args[2], "pane_id": self.agents[args[2]]}}}
        if verb == "agent start":
            self.agents[args[2]] = args[args.index("--pane") + 1]
        return {}

    def verbs(self):
        return [" ".join(c[:2]) for c in self.calls]

    def call(self, verb):
        (match,) = [c for c in self.calls if " ".join(c[:2]) == verb]
        return match

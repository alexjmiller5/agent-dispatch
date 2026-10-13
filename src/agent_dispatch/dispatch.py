"""One task in, one agent session out.

A tasks row with dispatch = Requested gets a new Herdr tab, an agent started
bare, then the filled prompt; the row moves to Started. Anything that stops
that moves it to Failed with the reason appended to notes. Rows are the truth:
every decision re-reads the row, so redelivered events and restarts are safe.
"""

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from .config import AgentSpec, Settings
from .herdr import HerdrError
from .hub import Hub, RevisionConflict

log = structlog.get_logger()

TABLE = "tasks"
COLUMNS = [
    "id",
    "title",
    "lane",
    "status",
    "dispatch",
    "notes",
    "deleted_at",
    "updated_at",
    "hub_at",
]
KINDS = ("claude", "codex")

DEFAULT_PROMPT = """\
Agent Dispatch sent you one task to work: soma `tasks` row {task_id} - "{title}".

1. Load the `soma` skill and read the task row in full first (title, notes, links, body, agent_sessions, project_ids, blocked_by_ids). Its notes are the user's instructions.
2. This row is your session's task: follow soma-map's agent session-task workflow. Start it with one write - status In Progress and APPEND your resume line to agent_sessions. Close it when the work is done and verified: status Completed, ai_completed 1, completed_date today.
3. Do the work. Propose first and wait for the user's OK in this pane when the task is a design decision or outward-facing (posting, messaging people, submitting forms, spending money, deleting or revoking).
4. If the last step is the user's (buy, send, decide, pick), write the prep into notes, set lane Me and leave the task open.
5. Never close your own tab: leave it open for review when you finish. End with what you did and what needs the user.
"""

STOPWORDS = {"a", "an", "the", "to", "for", "of", "in", "on", "at", "and", "or", "with", "my"}
STOPWORDS |= {"from", "by", "into", "about", "is", "it", "be"}


def agent_name(task_id: str) -> str:
    """Herdr agent names are unique per machine: one per task, so a relaunch adopts."""
    return f"task-{task_id[:16]}"


def short_label(title: str) -> str:
    """A 2-3 word tab label from the title (ponytail: stopword heuristic; Herdr's
    auto-title retitles the tab from the session anyway)."""
    words = [w.strip(".,:;!?()[]\"'") for w in (title or "").split()]
    words = [w for w in words if w]
    kept = [w for w in words if w.lower() not in STOPWORDS] or words
    return " ".join(w[:1].upper() + w[1:] for w in kept[:3]) or "Task"


def parse_quota(text: str) -> dict[str, float | None]:
    """quota-axi --json -> {provider: effective percent left across all models}."""
    try:
        providers = json.loads(text)["providers"]
    except (ValueError, KeyError, TypeError):
        return {}
    out = {}
    for provider in providers:
        known = [
            a.get("effectivePercentRemaining")
            for a in (provider.get("quotaSemantics") or {}).get("effectiveAvailability") or []
            if a.get("scope") == "all_models" and a.get("status") == "known"
        ]
        out[provider.get("provider")] = known[0] if known else None
    return out


def quota_axi(settings: Settings) -> Callable[[], dict[str, float | None]]:
    def read():
        try:
            result = subprocess.run(
                [settings.quota_axi, "--provider", ",".join(KINDS), "--json"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("quota unreadable", error=str(error))
            return {}
        return parse_quota(result.stdout)

    return read


def _alias_pattern(alias: str) -> re.Pattern:
    words = r"\s+".join(re.escape(w) for w in alias.split())
    return re.compile(rf"(?<![\w.]){words}(?![\w.])", re.IGNORECASE)


class Dispatcher:
    def __init__(self, hub: Hub, settings: Settings, herdr, quota):
        self.hub, self.settings, self.herdr, self.quota = hub, settings, herdr, quota

    # --- entry points ---------------------------------------------------------

    def reconcile(self) -> None:
        """Every task still Requested, whether or not its event was seen."""
        for row in self.hub.rows(TABLE, COLUMNS, {"dispatch": "Requested"}):
            self.handle(row)

    def handle_ids(self, row_ids) -> None:
        for row_id in row_ids:
            row = self.read(row_id)
            if row:
                self.handle(row)

    # --- one task -------------------------------------------------------------

    def read(self, row_id: str) -> dict | None:
        rows = self.hub.rows(TABLE, COLUMNS, {"id": row_id})
        return rows[0] if rows else None

    def handle(self, row: dict) -> None:
        if row["deleted_at"] or row["dispatch"] != "Requested":
            return
        task = row["id"]
        if row["lane"] == "Me":
            return self.fail(task, "lane is Me: only the user takes this task")
        if row["status"] in ("Completed", "Canceled"):
            return self.fail(task, f"task is {row['status']}")
        name = agent_name(task)
        try:
            self.herdr("agent", "get", name)
            log.info("adopting existing agent", task=task, agent=name)
            return self.mark(task, {"dispatch": "Started"})
        except HerdrError as error:
            if error.code != "agent_not_found":
                return self.fail(task, f"Herdr is unavailable: {error}")
        spec, why = self.choose(row["title"] or "")
        if spec is None:
            return self.fail(task, why)
        try:
            self.launch(name, spec, row)
        except HerdrError as error:
            return self.fail(task, f"Could not start an agent ({spec.kind}): {error}")
        log.info("dispatched", task=task, agent=name, kind=spec.kind, model=spec.model, why=why)
        self.mark(task, {"dispatch": "Started"})

    def choose(self, title: str) -> tuple[AgentSpec | None, str]:
        agents = self.settings.agents
        for alias in sorted(agents, key=len, reverse=True):
            if _alias_pattern(alias).search(title):
                return agents[alias], f"named in the title ({alias})"
        left = self.quota()
        floor = self.settings.min_quota_percent
        for role in ("default_agent", "fallback_agent"):
            spec = agents[getattr(self.settings, role)]
            percent = left.get(spec.kind)
            if percent is not None and percent >= floor:
                return spec, f"{spec.kind} quota {percent:g}% left"
        seen = ", ".join(
            f"{kind} {left[kind]:g}%" if left.get(kind) is not None else f"{kind} not measured"
            for kind in KINDS
        )
        return None, f"no agent quota left ({seen}; minimum {floor:g}%)"

    def launch(self, name: str, spec: AgentSpec, row: dict) -> None:
        workspace = (
            ("--workspace", self.settings.herdr_workspace) if self.settings.herdr_workspace else ()
        )
        created = self.herdr(
            "tab", "create", *workspace, "--cwd", str(self.settings.cwd),
            "--label", short_label(row["title"]), "--no-focus",
        )  # fmt: skip
        tab = created["result"]["tab"]["tab_id"]
        pane = created["result"]["root_pane"]["pane_id"]
        try:
            self.herdr(
                "agent", "start", name, "--kind", spec.kind, "--pane", pane,
                "--timeout", "90000", "--", *spec.args(),
            )  # fmt: skip
            self.herdr("agent", "wait", name, "--until", "idle", "--timeout", "90000")
            text = (
                self.prompt().replace("{task_id}", row["id"]).replace("{title}", row["title"] or "")
            )
            try:
                self.herdr(
                    "agent",
                    "prompt",
                    name,
                    text,
                    "--wait",
                    "--until",
                    "working",
                    "--timeout",
                    "30000",
                )
            except HerdrError as error:
                if error.code != "agent_prompt_stalled":
                    raise
                self.herdr("agent", "send-keys", name, "enter")  # never resend the text
        except HerdrError:
            try:
                self.herdr("tab", "close", tab)
            except HerdrError:
                pass
            raise

    def prompt(self) -> str:
        try:
            return self.settings.prompt_file.read_text()
        except FileNotFoundError:
            return DEFAULT_PROMPT

    # --- writes ---------------------------------------------------------------

    def fail(self, task: str, reason: str) -> None:
        log.warning("dispatch failed", task=task, reason=reason)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        self.mark(task, {"dispatch": "Failed"}, note=f"Agent Dispatch {stamp}: {reason}")

    def mark(self, task: str, values: dict, note: str | None = None) -> None:
        """Revision-checked patch from a fresh read; a concurrent edit is re-read,
        and a row no longer Requested is left as the other writer set it."""
        for _ in range(3):
            row = self.read(task)
            if not row or row["deleted_at"] or row["dispatch"] != "Requested":
                return
            patch = dict(values)
            if note:
                patch["notes"] = f"{row['notes']}\n\n{note}" if row["notes"] else note
            try:
                return self.hub.patch(
                    TABLE, task, patch, {"updated_at": row["updated_at"], "hub_at": row["hub_at"]}
                )
            except RevisionConflict:
                continue
        raise RevisionConflict(task)

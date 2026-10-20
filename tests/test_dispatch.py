import json

import httpx
import pytest
from pydantic import ValidationError

from agent_dispatch.config import CredentialError, Settings, resolve_token
from agent_dispatch.dispatch import (
    DEFAULT_PROMPT,
    Dispatcher,
    agent_name,
    parse_quota,
    short_label,
)
from agent_dispatch.herdr import HerdrError
from agent_dispatch.hub import Hub
from agent_dispatch.main import watch
from fakes import SUB, TOKEN, URL, FakeHerdr, FakeHub

TASK = "3f403953a8af81518075e507c97ee7e4"
OTHER = "0aa0aa0aa0aa0aa0aa0aa0aa0aa0aa01"


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "hub_url": URL,
        "subscription_id": SUB,
        "token": TOKEN,
        "state_dir": tmp_path / "state",
        "prompt_file": tmp_path / "prompt.md",
        "cwd": tmp_path,
    }
    return Settings(**{**values, **overrides})


def hub_for(fake) -> Hub:
    return Hub(URL, lambda: TOKEN, httpx.Client(transport=httpx.MockTransport(fake)))


class Quota:
    def __init__(self, claude=60.0, codex=100.0):
        self.left = {"claude": claude, "codex": codex}
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.left)


def dispatcher(tmp_path, fake, herdr=None, quota=None, **overrides):
    herdr = herdr or FakeHerdr()
    quota = quota or Quota()
    d = Dispatcher(hub_for(fake), settings(tmp_path, **overrides), herdr, quota)
    return d, herdr, quota


def run_watch(d, fake, iterations=1):
    sleeps = []
    watch(d.hub, d.settings, d, sleep=sleeps.append, iterations=iterations)
    return sleeps


# --- the happy path ----------------------------------------------------------


def test_requested_task_gets_a_claude_agent_tab_and_is_marked_started(tmp_path):
    fake = FakeHub()
    fake.request(TASK, title="Compare standing desks for the office")
    d, herdr, _ = dispatcher(tmp_path, fake)
    run_watch(d, fake, iterations=2)  # 1: reconcile + poll; 2: idle poll

    assert herdr.verbs() == ["agent get", "tab create", "agent start", "agent wait", "agent prompt"]
    create = herdr.call("tab create")
    assert create[create.index("--label") + 1] == "Compare Standing Desks"
    assert create[create.index("--cwd") + 1] == str(tmp_path) and "--no-focus" in create
    start = herdr.call("agent start")
    name = agent_name(TASK)
    assert start[2] == name and start[start.index("--kind") + 1] == "claude"
    assert start[start.index("--pane") + 1] == "w1:p1"
    assert start[start.index("--") + 1 :] == ("--model", "claude-opus-5-5")
    prompt = herdr.call("agent prompt")
    assert TASK in prompt[3] and "Compare standing desks for the office" in prompt[3]
    assert fake.rows[TASK]["dispatch"] == "Started"
    assert fake.acks == ["d1"]


def test_default_prompt_names_the_task_and_the_rules():
    for needle in ("{task_id}", "{title}", "soma", "agent_sessions", "Never close your own tab"):
        assert needle in DEFAULT_PROMPT


def test_default_prompt_preserves_lane_while_waiting_for_user():
    assert "set lane Me" not in DEFAULT_PROMPT
    assert "majority of the work" in DEFAULT_PROMPT
    assert "explicitly instructs" in DEFAULT_PROMPT
    assert "misclassification" in DEFAULT_PROMPT
    assert "keep its lane" in DEFAULT_PROMPT


def test_prompt_file_overrides_the_default(tmp_path):
    (tmp_path / "prompt.md").write_text("Work task {task_id}: {title}. Keep {braces}.")
    fake = FakeHub()
    fake.request(TASK, title="Sort photos")
    d, herdr, _ = dispatcher(tmp_path, fake)
    d.reconcile()
    assert herdr.call("agent prompt")[3] == f"Work task {TASK}: Sort photos. Keep {{braces}}."


def test_workspace_is_passed_to_tab_create_when_configured(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake, herdr_workspace="w1")
    d.reconcile()
    create = herdr.call("tab create")
    assert create[create.index("--workspace") + 1] == "w1"


# --- agent choice ------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "kind", "args"),
    [
        ("Use sonnet to tidy the notes", "claude", ("--model", "claude-sonnet-5")),
        ("research fridges with Fable 5.1", "claude", ("--model", "claude-fable-5-1")),
        ("opus: rewrite the README", "claude", ("--model", "claude-opus-5-5")),
        ("codex fix the flaky test", "codex", ("-c", "model_reasoning_effort=medium")),
    ],
)
def test_a_model_named_in_the_title_wins_without_a_quota_check(tmp_path, title, kind, args):
    fake = FakeHub()
    fake.request(TASK, title=title)
    d, herdr, quota = dispatcher(tmp_path, fake, quota=Quota(claude=0, codex=0))
    d.reconcile()
    start = herdr.call("agent start")
    assert start[start.index("--kind") + 1] == kind
    assert start[start.index("--") + 1 :] == args
    assert quota.calls == 0 and fake.rows[TASK]["dispatch"] == "Started"


def test_alias_inside_a_word_is_not_a_model_choice(tmp_path):
    fake = FakeHub()
    fake.request(TASK, title="Read the magnum opusculum")
    d, herdr, quota = dispatcher(tmp_path, fake)
    d.reconcile()
    assert quota.calls == 1


def test_alias_table_is_configurable(tmp_path):
    fake = FakeHub()
    fake.request(TASK, title="quick: rename files")
    agents = {"quick": {"kind": "claude", "model": "claude-haiku-5", "effort": "low"}}
    d, herdr, _ = dispatcher(
        tmp_path, fake, agents=agents, default_agent="quick", fallback_agent="quick"
    )
    d.reconcile()
    start = herdr.call("agent start")
    assert start[start.index("--") + 1 :] == ("--model", "claude-haiku-5", "--effort", "low")


def test_the_longest_matching_alias_wins(tmp_path):
    fake = FakeHub()
    fake.request(TASK, title="Use fable 5.1 here")
    agents = {
        "fable": {"kind": "claude", "model": "claude-fable-5"},
        "fable 5.1": {"kind": "claude", "model": "claude-fable-5-1"},
    }
    d, herdr, _ = dispatcher(
        tmp_path, fake, agents=agents, default_agent="fable", fallback_agent="fable"
    )
    d.reconcile()
    start = herdr.call("agent start")
    assert start[start.index("--") + 1 :] == ("--model", "claude-fable-5-1")


def test_codex_at_medium_effort_when_claude_quota_is_gone(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake, quota=Quota(claude=0, codex=40))
    d.reconcile()
    start = herdr.call("agent start")
    assert start[start.index("--kind") + 1] == "codex"
    assert start[start.index("--") + 1 :] == ("-c", "model_reasoning_effort=medium")


def test_unmeasured_claude_quota_counts_as_unavailable(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake, quota=Quota(claude=None, codex=40))
    d.reconcile()
    start = herdr.call("agent start")
    assert start[start.index("--kind") + 1] == "codex"


def test_no_quota_anywhere_fails_the_task_with_a_note(tmp_path):
    fake = FakeHub([{"id": TASK, "notes": "Earlier context."}])
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake, quota=Quota(claude=0.5, codex=None))
    run_watch(d, fake)
    row = fake.rows[TASK]
    assert herdr.verbs() == ["agent get"] and row["dispatch"] == "Failed"
    assert row["notes"].startswith("Earlier context.\n\nAgent Dispatch ")
    assert "no agent quota left" in row["notes"] and "claude 0.5%" in row["notes"]
    assert "codex not measured" in row["notes"]
    assert fake.acks == ["d1"]


def test_parse_quota_reads_effective_all_models_percent():
    report = {
        "providers": [
            {
                "provider": "claude",
                "quotaSemantics": {
                    "effectiveAvailability": [
                        {"scope": "model:fable", "status": "known", "effectivePercentRemaining": 9},
                        {"scope": "all_models", "status": "known", "effectivePercentRemaining": 61},
                    ]
                },
            },
            {"provider": "codex", "quotaSemantics": {"status": "unknown"}},
        ]
    }
    assert parse_quota(json.dumps(report)) == {"claude": 61, "codex": None}
    assert parse_quota("not json") == {}


# --- refusals and failures ----------------------------------------------------


@pytest.mark.parametrize(
    ("values", "reason"),
    [({"lane": "Me"}, "lane is Me"), ({"status": "Completed"}, "task is Completed")],
)
def test_me_and_closed_tasks_are_refused(tmp_path, values, reason):
    fake = FakeHub([{"id": TASK, **values}])
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake)
    d.reconcile()
    assert herdr.calls == [] and fake.rows[TASK]["dispatch"] == "Failed"
    assert reason in fake.rows[TASK]["notes"]


def test_herdr_failure_closes_the_tab_and_fails_the_task(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    herdr = FakeHerdr(fail={"agent wait": HerdrError("timeout", "agent never became idle")})
    d, herdr, _ = dispatcher(tmp_path, fake, herdr=herdr)
    d.reconcile()
    assert herdr.verbs()[-1] == "tab close" and herdr.calls[-1][2] == "w1:t1"
    assert fake.rows[TASK]["dispatch"] == "Failed"
    assert "timeout: agent never became idle" in fake.rows[TASK]["notes"]


def test_stalled_prompt_gets_an_enter_never_a_resend(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    herdr = FakeHerdr(fail={"agent prompt": HerdrError("agent_prompt_stalled", "unsent")})
    d, herdr, _ = dispatcher(tmp_path, fake, herdr=herdr)
    d.reconcile()
    assert herdr.verbs()[-2:] == ["agent prompt", "agent send-keys"]
    assert herdr.calls[-1][2:] == (agent_name(TASK), "enter")
    assert fake.rows[TASK]["dispatch"] == "Started"


def test_an_existing_agent_for_the_task_is_adopted_not_launched_twice(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake, herdr=FakeHerdr(agents={agent_name(TASK): "w1:p7"}))
    d.reconcile()
    assert herdr.verbs() == ["agent get"] and fake.rows[TASK]["dispatch"] == "Started"


def test_a_row_changed_during_launch_keeps_the_other_writers_value(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    herdr = FakeHerdr(hooks={"agent prompt": lambda: fake.edit(TASK, dispatch=None)})
    d, herdr, _ = dispatcher(tmp_path, fake, herdr=herdr)
    d.reconcile()
    assert fake.rows[TASK]["dispatch"] is None and fake.patches == []


def test_patch_conflict_rereads_and_retries(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    fake.conflicts = 1
    d, herdr, _ = dispatcher(tmp_path, fake)
    d.reconcile()
    assert fake.rows[TASK]["dispatch"] == "Started" and herdr.verbs().count("tab create") == 1


def test_hub_outage_after_launch_redelivers_and_adopts(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    herdr = FakeHerdr()
    d, herdr, _ = dispatcher(tmp_path, fake, herdr=herdr)
    real_patch = fake.patch
    fake.patch = lambda body: httpx.Response(503, json={"error": "down"})
    sleeps = run_watch(d, fake)
    assert fake.acks == [] and sleeps  # not acknowledged, backed off
    fake.patch = real_patch
    run_watch(d, fake)
    assert herdr.verbs().count("tab create") == 1  # adopted on redelivery
    assert fake.rows[TASK]["dispatch"] == "Started" and fake.acks == ["d1"]


# --- events and reconciliation ------------------------------------------------


def test_a_failed_pass_is_not_acknowledged(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake)
    d.reconcile = lambda: None  # the event arrives after startup
    fake.patch = lambda body: httpx.Response(503, json={"error": "down"})
    run_watch(d, fake)  # the event's pass fails at the hub write
    assert herdr.verbs().count("tab create") == 1
    assert fake.batches and fake.acks == []


def test_events_for_rows_not_requested_are_acked_and_ignored(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    fake.edit(TASK, dispatch="Started")  # already handled elsewhere
    d, herdr, _ = dispatcher(tmp_path, fake)
    run_watch(d, fake)
    assert herdr.calls == [] and fake.acks == ["d1"]


def test_deleted_task_is_ignored(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    fake.edit(TASK, deleted_at="2026-10-12T00:00:00.000Z")
    d, herdr, _ = dispatcher(tmp_path, fake)
    run_watch(d, fake)
    assert herdr.calls == [] and fake.acks == ["d1"]


def test_startup_reconciles_requested_tasks_without_events(tmp_path):
    fake = FakeHub([{"id": TASK, "dispatch": "Requested"}, {"id": OTHER, "dispatch": "Started"}])
    d, herdr, _ = dispatcher(tmp_path, fake)
    run_watch(d, fake)
    assert herdr.verbs().count("tab create") == 1
    assert fake.rows[TASK]["dispatch"] == "Started"


def test_one_batch_with_two_events_for_one_task_launches_once(tmp_path):
    fake = FakeHub()
    fake.request(TASK)
    fake.batches[0]["events"] *= 2
    d, herdr, _ = dispatcher(tmp_path, fake)
    run_watch(d, fake)
    assert herdr.verbs().count("tab create") == 1


# --- the daemon never spins ----------------------------------------------------


def test_rejected_credential_waits_an_hour(tmp_path):
    fake = FakeHub()
    fake.status = 401
    d, _, _ = dispatcher(tmp_path, fake)
    assert run_watch(d, fake) == [3600]


def test_missing_credential_waits_an_hour(tmp_path):
    d = Dispatcher(
        Hub(URL, lambda: resolve_token(settings(tmp_path, token=None))),
        settings(tmp_path, token=None),
        FakeHerdr(),
        Quota(),
    )
    assert run_watch(d, None) == [3600]


def test_unavailable_hub_backs_off(tmp_path):
    fake = FakeHub()
    fake.status = 503
    d, _, _ = dispatcher(tmp_path, fake)
    assert run_watch(d, fake, iterations=3) == [1.0, 2.0, 4.0]


# --- settings ----------------------------------------------------------------


def test_token_command_output_is_the_credential(tmp_path):
    s = settings(tmp_path, token=None, token_command=["printf", "%s", "abc def"])
    assert resolve_token(s) == "abc def"


@pytest.mark.parametrize("command", [["false"], ["true"], ["/nonexistent/command"]])
def test_failed_token_command_is_a_credential_error(tmp_path, command):
    with pytest.raises(CredentialError):
        resolve_token(settings(tmp_path, token=None, token_command=command))


@pytest.mark.parametrize("url", ["http://hub.example", "https://u:p@hub.example", "https://h/x"])
def test_hub_url_must_be_an_https_origin(tmp_path, url):
    with pytest.raises(ValidationError):
        settings(tmp_path, hub_url=url)


def test_default_and_fallback_must_name_configured_agents(tmp_path):
    with pytest.raises(ValidationError):
        settings(tmp_path, default_agent="missing")


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("Compare standing desks for the office", "Compare Standing Desks"),
        ("fix the url bug in synapse", "Fix Url Bug"),
        ("a", "A"),
        ("", "Task"),
    ],
)
def test_short_label(title, label):
    assert short_label(title) == label


def test_agent_name_is_a_valid_herdr_name():
    name = agent_name(TASK)
    assert name == "task-3f403953a8af8151" and len(name) <= 32

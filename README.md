# Agent Dispatch

Sends a task straight to an AI coding agent. Mark a Soma `tasks` row with
`dispatch = Requested` (Synapse does it for a capture whose `$` context is
exactly `agent`) and, within about a second, an always-on Mac opens a new
[Herdr](https://github.com/ogulcancelik/herdr) tab, starts an agent there and
hands it the task. The row moves to `Started`, or to `Failed` with the reason
appended to its notes.

The hub never connects to the Mac: the job holds an outbound long poll on a hub
subscription (`durable-pull-v1`) over `tasks.dispatch`.

## Row contract (`tasks`)

| Column | Writer | Meaning |
|---|---|---|
| `dispatch` | anyone / job | `Requested` = open an agent session; `Started` and `Failed` are written only by this job |
| `notes` | job (append only) | on `Failed`: `Agent Dispatch <time>: <reason>` |
| `title`, `lane`, `status` | read | `lane = Me` and closed tasks are refused (`Failed`) |

Set `Requested` again to retry a `Failed` task. A task whose agent is still
running in Herdr (agent name `task-<first 16 hex of the id>`) is adopted, never
launched twice.

## Which agent

1. An alias named in the title wins (whole words, longest first, no quota
   check). Built-in table: `opus` -> Claude `claude-opus-5-5`, `sonnet` ->
   `claude-sonnet-5`, `fable 5.1` -> `claude-fable-5-1`, `codex` -> Codex at
   medium reasoning effort (its configured model).
2. Otherwise the default agent (`opus`) when [quota-axi](https://github.com/kunchenguid/quota-axi)
   shows its provider at or above `minQuotaPercent` (1), else the fallback
   (`codex`) by the same test.
3. Neither: `Failed`, with both providers' numbers in the note.

The agent starts bare in the tab, Agent Dispatch waits for it to be idle, then
sends the prompt.

## The prompt

`~/.config/agent-dispatch/prompt.md` (the `promptFile` option) is user state:
absent means the built-in default, which tells the agent to load the `soma`
skill, read the task in full, start it per the soma-map session-task workflow
(status In Progress plus its resume line), do the work, propose first for
design or outward-facing work, and never close its own tab. `{task_id}` and
`{title}` are filled in. To customize:

```bash
agent-dispatch prompt > ~/.config/agent-dispatch/prompt.md   # then edit
```

## Install

The flake exports the package and `darwinModules.default`:

```nix
services.agent-dispatch = {
  enable = true;
  user = "<login user>";
  hubUrl = "https://<hub>";
  subscriptionId = "<subscription id>";
  credentialCommand = [ "/usr/bin/security" "find-generic-password" "-s" "agent-dispatch" "-a" "hub" "-w" ];
  herdrWorkspace = "w1";            # optional
  # agents / defaultAgent / fallbackAgent / minQuotaPercent / promptFile / cwd: see nix/darwin.nix
};
```

It runs `agent-dispatch watch` as a kept-alive launchd user agent in the login
session (so it can read the Keychain and reach the user's Herdr server); the log
is `~/.local/state/agent-dispatch/agent-dispatch.log`. `herdr` and `quota-axi`
must be on the daemon's `path` (default: the user's and the system's Nix
profiles). Claude quota needs quota-axi's one-time Keychain grant
(`quota-axi --allow-keychain-prompt` from a desktop terminal).

## Service setup (once per hub)

1. Catalog `tasks.dispatch` (select `Requested` / `Started` / `Failed`).
2. As the hub operator create the subscription:
   `POST /v1/subscriptions {"label": "Agent Dispatch", "sources": [{"table": "tasks", "columns": ["dispatch"]}], "start": "now"}`.
3. Add an enrollment profile with exactly these grants and redeploy the hub:
   `subscriptions:consume:<id>`, `tables:read:tasks` (the hub requires a
   whole-table read to consume a subscription),
   `tables:read:tasks:{id,updated_at,hub_at,dispatch,notes}`,
   `tables:patch:tasks:{dispatch,notes}`.
4. Enroll headlessly and approve the printed link:
   `soma login --profile <profile> --name "Agent Dispatch <machine>" --start <state>`,
   then `soma login --claim <state> --wait` prints the token once.
5. Store it in the Mac's login Keychain from the desktop session (ssh sessions
   cannot write the Keychain), passing it to `security -i` on stdin:
   `add-generic-password -A -U -s agent-dispatch -a hub -w <token>`. Then
   `launchctl kickstart -k gui/$(id -u)/com.alexmiller.agent-dispatch`.

A replacement Mac enrolls a new credential; revoke the old one at
`<hub>/login/devices`.

## Develop

`just test`, `just check`, `just fmt`; `just dev` runs the watcher and
`just run` one reconciliation pass with your `AGENT_DISPATCH_*` environment.
Tests drive the job against an in-process fake hub and a fake Herdr.

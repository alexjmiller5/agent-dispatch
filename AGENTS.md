# Agent Dispatch - agent instructions

A launchd job on an always-on Mac that turns Soma tasks marked
`dispatch = Requested` into agent sessions in new Herdr tabs. Built from the
mini-job template: one long-lived user agent (`agent-dispatch watch`) holding an
outbound long poll on a Soma hub subscription. README.md has the row contract,
agent choice, install and service setup.

## Layout

- `src/agent_dispatch/config.py` - `Settings` (`AGENT_DISPATCH_*` env, built in
  `cli()`, never at import), the agent alias table, the credential seam
  (`TOKEN` literal or `TOKEN_COMMAND` JSON argv).
- `src/agent_dispatch/hub.py` - durable-pull-v1 poll/ack, projected pulls,
  revision-checked patches. Never a hot loop: 401/403 and a missing credential
  wait an hour, 429/5xx honor Retry-After or back off 1 to 60 s.
- `src/agent_dispatch/herdr.py` - the herdr CLI as a function; errors carry
  Herdr's error code.
- `src/agent_dispatch/dispatch.py` - one task: guards, adopt-by-agent-name,
  agent choice (title alias, then quota-axi), tab create -> agent start bare ->
  wait idle -> prompt (a stalled prompt gets Enter, never a resend), then the
  `Started` / `Failed` patch.
- `src/agent_dispatch/main.py` - the watch loop and CLI (`watch`, `once`, `prompt`).
- `nix/darwin.nix` - `services.agent-dispatch` (generic options only).

## Rules

- **Rows are the truth; events are wake-ups.** Every decision re-reads the row;
  startup and any failed pass reconcile all `Requested` rows; a batch is ACKed
  only after its pass succeeded. Writes are revision-checked patches from a
  fresh read; a row no longer `Requested` keeps the other writer's value.
- **Never launch twice.** The Herdr agent name is derived from the task id; an
  existing agent is adopted. `Failed` is terminal until someone sets `Requested`.
- **Outcomes live on the row** (`dispatch`, a `notes` line), not only the log.
- The app names no secret store and no machine path; the consumer's config
  supplies hub URL, subscription id, credential command, Herdr workspace.
- The default prompt preserves assigned task lanes while waiting for user action;
  lane means who will do most of the work. Only an explicit user instruction
  changes an assigned lane, including correction of an initial misclassification.
- The default prompt is product text; a user's prompt is state in
  `promptFile`, never a repo change.

## Service contract (Soma)

Consumer of the Soma hub through its supported API with its own enrolled
profile credential (`agent-dispatch-v1`): `subscriptions:consume:<id>`,
`tables:read:tasks` (the hub requires a whole-table read for a subscription
consumer), column reads `id,updated_at,hub_at,dispatch,notes` and patches of
`dispatch,notes` only. No files, no inserts, no other tables. The token lives
in the Mac's login Keychain; rotating or revoking it touches only this job.
Herdr, quota-axi and the agents are the user's own installs on the same Mac.

## Development

uv + pytest + Ruff, venv and bytecode outside the iCloud checkout (justfile).
`just test` (fake hub via `httpx.MockTransport`, fake Herdr in `tests/fakes.py`),
`just check` (Ruff + `nix flake check`), `nix build`. TDD; mutation-test new
branches. Deploy = push, then bump the flake input in the consumer's nix-config
and rebuild that Mac; verify `launchctl list com.alexmiller.agent-dispatch` and
the log (`just logs`).

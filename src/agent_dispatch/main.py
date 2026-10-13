"""agent-dispatch: an outbound long-polling consumer, run by launchd.

agent-dispatch watch    reconcile, then long-poll the subscription forever
agent-dispatch once     one reconciliation pass over Requested tasks, no polling
agent-dispatch prompt   print the prompt template in effect (copy it to edit)
"""

import sys
import time

import structlog

from .config import CredentialError, Settings, resolve_token
from .dispatch import DEFAULT_PROMPT, Dispatcher, quota_axi
from .herdr import Herdr
from .hub import Hub, HubError

log = structlog.get_logger()


def watch(
    hub: Hub, settings: Settings, dispatcher: Dispatcher, *, sleep=time.sleep, iterations=None
):
    dirty, backoff = True, 1.0
    while iterations is None or iterations > 0:
        iterations = None if iterations is None else iterations - 1
        try:
            if dirty:
                dispatcher.reconcile()
                dirty = False
            batch = hub.poll(settings.subscription_id, settings.hold_seconds)
            if batch["events"]:
                # Events are wake-ups; the rows decide. ACK only after the pass.
                dispatcher.handle_ids(
                    e["source"]["row_id"] for e in batch["events"] if e["source"].get("row_id")
                )
                hub.ack(settings.subscription_id, batch["delivery_id"])
            backoff = 1.0
        except (HubError, CredentialError) as error:
            dirty = True
            credential = isinstance(error, CredentialError) or error.credential
            delay = min(3600, 3600 if credential else error.retry_after or backoff)
            backoff = min(60.0, backoff * 2)
            log.warning("hub", error=str(error), retry_in=delay)
            sleep(delay)
        except Exception as error:  # noqa: BLE001 - the daemon outlives one bad pass
            dirty = True
            log.error("pass failed", error=f"{type(error).__name__}: {error}")
            sleep(30)


def cli(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "--help"
    if command not in ("watch", "once", "prompt"):
        print(__doc__.strip(), file=sys.stderr)
        return 0 if command in ("-h", "--help") else 2
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )
    settings = Settings()
    if command == "prompt":
        path = settings.prompt_file
        print(path.read_text() if path.exists() else DEFAULT_PROMPT, end="")
        return 0
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub(settings.hub_url, lambda: resolve_token(settings))
    dispatcher = Dispatcher(
        hub, settings, Herdr(settings.herdr, settings.herdr_session), quota_axi(settings)
    )
    if command == "once":
        dispatcher.reconcile()
    else:
        log.info("watching", hub=settings.hub_url, subscription=settings.subscription_id)
        watch(hub, settings, dispatcher)
    return 0


if __name__ == "__main__":
    sys.exit(cli())

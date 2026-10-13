"""Settings from the caller environment (AGENT_DISPATCH_*), resolved at start.

The credential seam: AGENT_DISPATCH_TOKEN (literal) or AGENT_DISPATCH_TOKEN_COMMAND
(a JSON argv whose stdout is the token). The job never knows which secret store
its caller uses.
"""

import os
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP = "agent-dispatch"


class CredentialError(Exception):
    pass


class AgentSpec(BaseModel):
    """One launchable agent: a Herdr agent kind plus its model and effort."""

    kind: Literal["claude", "codex"]
    model: str | None = None  # None: the agent's own configured default
    effort: str | None = None

    def args(self) -> tuple[str, ...]:
        out = ("--model", self.model) if self.model else ()
        if self.effort:
            effort = (
                ("--effort", self.effort)
                if self.kind == "claude"
                else ("-c", f"model_reasoning_effort={self.effort}")
            )
            out += effort
        return out


# Aliases matched in a task title (case-insensitive, whole words). Replaced
# wholesale by AGENT_DISPATCH_AGENTS.
DEFAULT_AGENTS = {
    "opus": AgentSpec(kind="claude", model="claude-opus-5-5"),
    "sonnet": AgentSpec(kind="claude", model="claude-sonnet-5"),
    "fable 5.1": AgentSpec(kind="claude", model="claude-fable-5-1"),
    "codex": AgentSpec(kind="codex", effort="medium"),
}


def _xdg(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / fallback) / APP


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_DISPATCH_", hide_input_in_errors=True)

    hub_url: str
    subscription_id: str
    token: SecretStr | None = None
    token_command: list[str] | None = None
    state_dir: Path = Field(default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state"))
    hold_seconds: int = Field(default=30, ge=0, le=30)
    # Herdr destination for agent tabs; cwd is where each agent session starts.
    herdr: str = "herdr"
    herdr_session: str = "default"
    herdr_workspace: str | None = None
    cwd: Path = Field(default_factory=Path.home)
    # The prompt each agent receives; {task_id} and {title} are filled in.
    prompt_file: Path = Field(
        default_factory=lambda: _xdg("XDG_CONFIG_HOME", ".config") / "prompt.md"
    )
    agents: dict[str, AgentSpec] = Field(default_factory=lambda: dict(DEFAULT_AGENTS))
    default_agent: str = "opus"  # used while its provider has quota left
    fallback_agent: str = "codex"  # used when the default's provider has none
    quota_axi: str = "quota-axi"
    min_quota_percent: float = Field(default=1, ge=0, le=100)

    @field_validator("hub_url")
    @classmethod
    def https_origin(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("hub_url must be an HTTPS origin without credentials")
        return value.rstrip("/")

    @model_validator(mode="after")
    def known_agents(self):
        for name in (self.default_agent, self.fallback_agent):
            if name not in self.agents:
                raise ValueError(f"agent {name!r} is not in the agents table")
        return self


def resolve_token(settings: Settings) -> str:
    if settings.token:
        return settings.token.get_secret_value()
    if not settings.token_command:
        raise CredentialError(
            "no credential: set AGENT_DISPATCH_TOKEN or AGENT_DISPATCH_TOKEN_COMMAND"
        )
    try:
        result = subprocess.run(
            settings.token_command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialError("token command could not run") from None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise CredentialError("token command failed")
    return token

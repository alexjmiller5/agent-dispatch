"""Private delivery phases, retained before handing work to an external agent."""

import hashlib
import os
from pathlib import Path
import tempfile

PHASES = {"blank", "delivering", "delivered"}


class Handoffs:
    def __init__(self, state_dir: Path):
        self.directory = state_dir / "handoffs"

    def path(self, task: str) -> Path:
        return self.directory / hashlib.sha256(task.encode()).hexdigest()

    def read(self, task: str) -> str | None:
        try:
            phase = self.path(task).read_text()
        except FileNotFoundError:
            return None
        if phase not in PHASES:
            raise ValueError("Invalid retained handoff phase; refusing to resend work")
        return phase

    def write(self, task: str, phase: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(phase)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.path(task))
            finally:
                temporary.unlink(missing_ok=True)

"""Private delivery phases, retained before handing work to an external agent."""

import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import sys

PHASES = {"blank", "delivering", "delivered"}


def _sync(fd: int) -> None:
    os.fsync(fd)
    if sys.platform == "darwin":
        # Darwin fsync alone does not request a device-cache flush.
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _sync(fd)
    finally:
        os.close(fd)


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
        _sync_directory(self.directory.parent)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(phase)
                stream.flush()
                _sync(stream.fileno())
                os.replace(temporary, self.path(task))
                _sync_directory(self.directory)
            finally:
                temporary.unlink(missing_ok=True)

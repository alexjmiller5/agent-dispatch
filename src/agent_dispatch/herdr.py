"""The Herdr CLI as a function: herdr("tab", "create", ...) returns parsed JSON
(plain text for `agent read`) or raises HerdrError with Herdr's error code."""

import json
import subprocess


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class Herdr:
    def __init__(self, executable: str, session: str):
        self.base = [executable, "--session", session]

    def __call__(self, *args: str):
        try:
            result = subprocess.run(
                [*self.base, *args], capture_output=True, text=True, timeout=180, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HerdrError("unavailable", f"{type(error).__name__}: {error}") from None
        if result.returncode:
            text = (result.stderr or result.stdout).strip()
            try:
                error = json.loads(text)["error"]
                raise HerdrError(error["code"], error["message"])
            except (ValueError, KeyError, TypeError):
                raise HerdrError("failed", text[:300]) from None
        if args[:2] == ("agent", "read"):
            return result.stdout
        return json.loads(result.stdout) if result.stdout.strip() else {}

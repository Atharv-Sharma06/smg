"""Single-active-trader lock.

SMG allows one active trader per account at a time. The bot enforces that for
its own processes with a PID lock file: a second run (cron overlap, a manual
run while the scheduler is running) refuses to trade instead of racing the
first one. Stale locks from crashed processes are reclaimed automatically.
"""

from __future__ import annotations

import os
from pathlib import Path


class TraderLockError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class TraderLock:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def holder_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip() or 0)
        except (FileNotFoundError, ValueError):
            return None

    def acquire(self) -> None:
        if self._held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self.holder_pid()
                if pid is not None and pid != os.getpid() and _pid_alive(pid):
                    raise TraderLockError(
                        f"Another trader is active (pid {pid}, lock {self.path}). "
                        "SMG allows one active trader at a time."
                    ) from None
                self.path.unlink(missing_ok=True)  # stale lock
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self._held = True
            return
        raise TraderLockError(f"Could not acquire trader lock {self.path}")

    def release(self) -> None:
        if self._held:
            if self.holder_pid() == os.getpid():
                self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> TraderLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

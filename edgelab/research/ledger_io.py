"""One cross-process lock for every ledger read/modify/write transaction."""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import fcntl


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

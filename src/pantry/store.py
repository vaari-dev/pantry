"""JSON files, written atomically, readable only by the owner."""

import contextlib
import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()


def read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())  # the ledger must outlive a power cut right after an order
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@contextlib.contextmanager
def edit(path: Path, default: dict):
    """The file's data to change in place, written back when the block ends. Edits never interleave:
    a read-modify-write from another thread would otherwise write back what it read before ours."""
    with _lock:
        data = {**copy.deepcopy(default), **read(path, {})}
        yield data
        write(path, data)

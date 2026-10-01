"""Where the app keeps scratch and cache files, and how it writes its outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WORK_ROOT = Path.home() / "tmp" / "dolbytool"

FileStamp = tuple[Path, int | None]


def work_root() -> Path:
    """This process's scratch directory; `sweep_scratch` removes it once the process is gone."""
    path = WORK_ROOT / "scratch" / str(os.getpid())
    path.mkdir(parents=True, exist_ok=True)
    return path


def sweep_scratch() -> None:
    """Remove the scratch left by processes that died without cleaning up (a crash or a kill)."""
    root = WORK_ROOT / "scratch"
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.name.isdigit() and not _alive(int(entry.name)):
            shutil.rmtree(entry, ignore_errors=True)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # someone else's process, alive
    return True


def _alive_windows(pid: int) -> bool:
    # os.kill(pid, 0) on Windows terminates the process instead of probing it.
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, not ours
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def cache_dir(name: str = "cache") -> Path:
    """A directory under the work root that outlives the process, such as the trace cache."""
    path = WORK_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_stamp(path: Path | None) -> FileStamp | None:
    """Identity of a file's current contents, for caches keyed on a path that may be rewritten."""
    if path is None:
        return None
    try:
        return path, path.stat().st_mtime_ns
    except OSError:
        return path, None


def content_key(path: Path) -> str:
    """A cache key that changes whenever the file at `path` is replaced or rewritten."""
    stat = path.stat()
    ident = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha1(ident.encode()).hexdigest()[:20]


def partial_path(target: Path) -> Path:
    """Where `target` is written until it is complete; keeps the suffix for tools that need it."""
    return target.with_name(f".{target.stem}.partial{target.suffix}")


@contextmanager
def atomic_output(target: Path) -> Iterator[Path]:
    """A scratch path beside `target` that replaces it only once the block succeeds.

    The scratch name keeps the suffix, so tools that pick a format by extension
    still do; a failed or cancelled run leaves any earlier `target` untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(target)
    partial.unlink(missing_ok=True)
    try:
        yield partial
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)

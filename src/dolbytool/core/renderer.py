"""Running the headless-browser renderers in `renderer/` (Node + chrome-headless-shell).

Each script prints one JSON event per line on stdout: `progress` with the
frame count, `status` with a message.  This module finds a JavaScript
runtime, runs a built script and turns those events into callbacks.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.i18n import t
from dolbytool.core.process import run_streaming

# A PyInstaller bundle carries renderer/dist under its data root (sys._MEIPASS).
if getattr(sys, "frozen", False):
    RENDERER_DIR = Path(sys._MEIPASS) / "renderer"
else:
    RENDERER_DIR = Path(__file__).resolve().parents[3] / "renderer"


class RendererError(RuntimeError):
    pass


def renderer_script(name: str) -> Path:
    """A built script in renderer/dist, or RendererError telling how to build it."""
    script = RENDERER_DIR / "dist" / name
    if not script.is_file():
        raise RendererError(t("error.renderer_not_built", dir=RENDERER_DIR))
    return script


def renderer_problem(name: str) -> str | None:
    """Why the renderer script `name` cannot run here, or None when it can."""
    try:
        renderer_script(name)
        find_runtime()
    except RendererError as error:
        return str(error)
    return None


@cache
def find_runtime() -> str:
    """node on PATH, else the newest nvm install (a desktop launch skips nvm's PATH), else bun."""
    found = shutil.which("node")
    if found:
        return found
    nvm = sorted(
        (Path.home() / ".nvm" / "versions" / "node").glob("*/bin/node"),
        key=lambda p: [int(x) for x in p.parent.parent.name.lstrip("v").split(".") if x.isdigit()],
    )
    if nvm:
        return str(nvm[-1])
    for bun in (shutil.which("bun"), Path.home() / ".bun" / "bin" / "bun"):
        if bun and Path(bun).is_file():
            return str(bun)
    raise RendererError(t("error.no_js_runtime"))


def run_renderer(
    script: Path,
    args: Sequence[str],
    token: CancellationToken | None,
    on_fraction: Callable[[float], None],
    on_status: Callable[[str], None],
    exit_message: str | None = None,
) -> None:
    """Run `script` to completion; raises RendererError on failure.

    The error is the renderer's own when it names one by key, put in the
    user's language; otherwise its stderr.
    """
    failure: dict[str, object] = {}

    def on_line(line: str) -> None:
        # stdout carries one JSON event per line; anything else is not ours to parse
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "progress" and event.get("total"):
            on_fraction(min(1.0, event["frame"] / event["total"]))
        elif event.get("type") == "status":
            on_status(t(f"renderer.{event.get('key')}", **_values(event)))
        elif event.get("type") == "error":
            failure.update(event)

    try:
        # SIGTERM lets the renderer close its browsers and remove its segment files
        run_streaming(
            [find_runtime(), str(script), *args], on_line, token,
            error=RendererError, exit_message=exit_message or t("error.renderer_exit"), grace=10,
        )  # fmt: skip
    except RendererError as error:
        if not failure:
            raise
        raise RendererError(
            t(f"renderer.error.{failure.get('key')}", **_values(failure))
        ) from error


def _values(event: dict) -> dict[str, object]:
    """An event's values, without what names it."""
    return {k: v for k, v in event.items() if k not in ("type", "key")}

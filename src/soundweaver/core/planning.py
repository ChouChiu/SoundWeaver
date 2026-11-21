"""Shared planning contract for single-track and batch outputs."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from soundweaver.core.i18n import t
from soundweaver.core.project import PlanContext, Project
from soundweaver.core.task import Step


class OutputPlanner(Protocol):
    """The planning operations a feature exposes, independent of its controls."""

    @property
    def title(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def needs_audio(self) -> bool: ...

    @property
    def per_album(self) -> bool: ...

    def required_stems(self, project: Project) -> set[str]: ...
    def is_active(self, ctx: PlanContext) -> bool: ...
    def problem(self, ctx: PlanContext) -> str | None: ...
    def plan(self, ctx: PlanContext) -> list[Step]: ...
    def album_plan(
        self, ctx: PlanContext, tracks: Sequence[tuple[Project, list[Step]]]
    ) -> list[Step]: ...


@dataclass(slots=True)
class ExportPlan:
    """Runnable steps and the files they read or write."""

    steps: list[Step] = field(default_factory=list)
    inputs: set[Path] = field(default_factory=set)
    output_dirs: list[Path] = field(default_factory=list)

    @property
    def targets(self) -> list[Path]:
        return [target for step in self.steps for target in step.targets]


def build_context(
    panels: Sequence[OutputPlanner],
    project: Project,
    *,
    batch: bool,
    album: bool = False,
) -> PlanContext:
    """Assemble `PlanContext` gathering required stems from all enabled panels."""
    required: set[str] = set()
    requested_by: list[str] = []
    for panel in panels:
        if panel.enabled and (need := panel.required_stems(project)):
            required |= need
            requested_by.append(panel.title)
    return PlanContext(project, frozenset(required), tuple(requested_by), batch, album)


def panel_why_not(ctx: PlanContext, panel: OutputPlanner) -> str | None:
    """Reason `panel` cannot run for `ctx`, checking shared audio first."""
    project = ctx.project
    if panel.needs_audio:
        if project.audio is None:
            return t("source.prompt_add_audio") if ctx.batch else t("source.prompt_pick_audio")
        if project.probing:
            return t("status.parsing_audio")
        if project.info is None:
            return t("error.audio_parse_failed", error=project.audio_error or t("error.unknown"))
    return panel.problem(ctx)


def output_problems(ctx: PlanContext, panels: Sequence[OutputPlanner]) -> dict[int, str]:
    return {
        index: reason
        for index, panel in enumerate(panels)
        if panel.enabled and (reason := panel_why_not(ctx, panel))
    }


def dir_problem(out: Path | None) -> str | None:
    """Check the nearest existing ancestor before attempting to create directories."""
    if out is None:
        return t("error.invalid_output_dir")
    existing = next((path for path in (out, *out.parents) if path.exists()), None)
    if existing is None:
        return t("error.output_no_write")
    if not existing.is_dir():
        return t("error.output_not_dir")
    if not os.access(existing, os.W_OK | os.X_OK):
        return t("error.output_no_write")
    return None


def find_target_clash(inputs: set[Path], steps: list[Step]) -> str | None:
    """Detect output targets that collide with an input file or with another step."""
    targets = [target.resolve() for step in steps for target in step.targets]
    resolved = {p.resolve() for p in inputs}
    if hit := next((target for target in targets if target in resolved), None):
        return t("error.clash_input", name=hit.name)
    if duplicate := next((target for target, n in Counter(targets).items() if n > 1), None):
        return t("error.clash_steps", name=duplicate.name)
    return None

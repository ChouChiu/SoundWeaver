"""Compose export feature planners for the desktop workspace."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from soundweaver.core.i18n import t
from soundweaver.core.planning import (
    ExportPlan,
    OutputPlanner,
    build_context,
    dir_problem,
    output_problems,
)
from soundweaver.core.project import PlanContext, Project
from soundweaver.features.batch.planner import BatchPlan, build_batch_plan


class ExportOutput(OutputPlanner, Protocol):
    def summary(self, ctx: PlanContext) -> str | None: ...


@dataclass(frozen=True, slots=True)
class BatchSelection:
    projects: list[Project]
    album_project: Project | None
    is_album: bool
    busy: bool


@dataclass(frozen=True, slots=True)
class ExportAssessment:
    context: PlanContext
    active: tuple[int, ...]
    statuses: tuple[tuple[str, bool], ...]
    summary: str
    blocker: str | None
    batch_plan: BatchPlan | None


def assess_exports(
    panels: Sequence[ExportOutput],
    project: Project,
    missing_tools: Sequence[str],
    batch: BatchSelection | None,
) -> ExportAssessment:
    ctx = build_context(panels, project, batch=batch is not None)
    problems = output_problems(ctx, panels)
    active = tuple(index for index, panel in enumerate(panels) if panel.is_active(ctx))
    batch_plan = (
        build_batch_plan(panels, batch.projects, batch.album_project, is_album=batch.is_album)
        if batch is not None
        else None
    )
    shares = batch_plan.shares if batch_plan is not None else {}
    summaries: list[str] = []
    statuses: list[tuple[str, bool]] = []
    for index, panel in enumerate(panels):
        if index in shares:
            statuses.append(shares[index])
        elif index in problems:
            statuses.append((problems[index], True))
        elif index in active:
            summary = panel.summary(ctx) or ""
            statuses.append((summary, False))
            if summary:
                summaries.append(summary)
        else:
            statuses.append((t("switch.disabled"), False))

    blocker = _blocker(panels, project, problems, missing_tools, batch, batch_plan)
    if blocker:
        summary = blocker
    elif batch is not None and batch_plan is not None:
        summary = t(
            "batch.summary_label",
            mode=t("batch.album_prefix" if batch.is_album else "batch.batch_prefix"),
            tracks=batch_plan.tracks,
            total=len(batch.projects),
            steps=len(batch_plan.steps),
        )
    else:
        summary = t("inputs.to_generate", items=t("common.list_sep").join(summaries))

    return ExportAssessment(ctx, active, tuple(statuses), summary, blocker, batch_plan)


def _blocker(
    panels: Sequence[OutputPlanner],
    project: Project,
    problems: dict[int, str],
    missing_tools: Sequence[str],
    batch: BatchSelection | None,
    batch_plan: BatchPlan | None,
) -> str | None:
    if missing_tools:
        return t(
            "status.missing_dependencies_blocker",
            tools=t("common.list_sep").join(missing_tools),
        )
    if not any(panel.enabled for panel in panels):
        return t("status.no_output_selected")
    if batch_plan is not None:
        assert batch is not None
        if not batch.projects:
            return t("batch.blocker_no_tracks")
        if batch.busy:
            return t("batch.reading_track_info")
        return None if batch_plan.steps else t("batch.blocker_no_runnable")
    if problems:
        index, text = next(iter(problems.items()))
        return t("common.labeled", label=panels[index].title, text=text)
    return dir_problem(project.output_dir)


def plan_single(ctx: PlanContext, panels: Sequence[OutputPlanner]) -> ExportPlan:
    """Build steps after the single-track checks and feature confirmations pass."""
    out = ctx.project.output_dir
    assert out is not None
    return ExportPlan(
        steps=[step for panel in panels for step in panel.plan(ctx)],
        inputs=set(ctx.project.inputs()),
        output_dirs=[out],
    )

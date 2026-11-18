"""Batch & single-track planning helpers separated from the main window."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from soundweaver.core.i18n import t
from soundweaver.core.planning import (
    OutputPlanner,
    build_context,
    dir_problem,
    find_target_clash,
    output_problems,
    panel_why_not,
)
from soundweaver.core.project import PlanContext, Project
from soundweaver.core.task import Step
from soundweaver.core.ttml import without_number

__all__ = [
    "BatchPlan",
    "batch_shares",
    "build_batch_plan",
    "build_context",
    "dir_problem",
    "find_target_clash",
    "panel_why_not",
    "plan_track",
]


@dataclass(slots=True)
class BatchPlan:
    """Every track of a batch planned with the current settings."""

    steps: list[Step] = field(default_factory=list)
    status: list[tuple[str, bool]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    output_dirs: list[Path] = field(default_factory=list)
    inputs: set[Path] = field(default_factory=set)
    tracks: int = 0
    shares: dict[int, tuple[str, bool]] = field(default_factory=dict)


def plan_track(
    panels: Sequence[OutputPlanner],
    project: Project,
    album: bool,
) -> tuple[dict[OutputPlanner, list[Step]], list[str]]:
    """Compute one batch track's runnable steps per panel and any skipped-output reasons."""
    ctx = build_context(panels, project, batch=True, album=album)
    return _plan_track(panels, ctx, output_problems(ctx, panels))


def _plan_track(
    panels: Sequence[OutputPlanner], ctx: PlanContext, problems: dict[int, str]
) -> tuple[dict[OutputPlanner, list[Step]], list[str]]:
    steps: dict[OutputPlanner, list[Step]] = {}
    left_out: list[str] = []
    for index, panel in enumerate(panels):
        if (ctx.album and panel.per_album) or not panel.is_active(ctx):
            continue
        if reason := problems.get(index):
            left_out.append(t("common.with_note", text=panel.title, note=reason))
        else:
            steps[panel] = panel.plan(ctx)
    return steps, left_out


def build_batch_plan(
    panels: Sequence[OutputPlanner],
    projects: list[Project],
    album_project: Project | None,
    *,
    is_album: bool,
) -> BatchPlan:
    """Build the full step plan, per-track statuses, and album-closing steps for a batch."""
    plan = BatchPlan()
    album_prefix = t("batch.album_prefix")
    made: dict[OutputPlanner, list[tuple[Project, list[Step]]]] = {}
    assessed: list[dict[int, str]] = []

    if album_project is not None:
        album_ctx = build_context(panels, album_project, batch=True)
        for panel in panels:
            if not (panel.enabled and panel.per_album):
                continue
            if problem := panel.problem(album_ctx):
                plan.skipped.append(
                    t("common.labeled", label=f"{album_prefix} · {panel.title}", text=problem)
                )
                continue
            steps = panel.plan(album_ctx)
            plan.steps += [replace(s, title=f"{album_prefix} · {s.title}") for s in steps]
        if plan.steps and album_project.output_dir:
            plan.output_dirs.append(album_project.output_dir)
            plan.inputs |= set(album_project.inputs()) - {album_project.audio}

    for number, project in enumerate(projects, 1):
        assert project.audio is not None
        plan.inputs |= set(project.inputs())
        name = f"{number:02d} {without_number(project.audio.stem)}"
        if project.probing:
            plan.status.append((t("status.reading"), False))
            continue
        if project.info is None:
            plan.status.append(
                (
                    t("batch.skipped_prefix", reason=project.audio_error or t("error.cannot_read_short")),
                    True,
                )
            )
            continue
        ctx = build_context(panels, project, batch=True, album=is_album)
        problems = output_problems(ctx, panels)
        assessed.append(problems)
        if d_prob := dir_problem(project.output_dir):
            plan.status.append((t("batch.skipped_prefix", reason=d_prob), True))
            continue
        by_panel, left_out = _plan_track(panels, ctx, problems)
        for panel, panel_steps in by_panel.items():
            made.setdefault(panel, []).append((project, panel_steps))
        steps = [step for panel_steps in by_panel.values() for step in panel_steps]
        for note in left_out:
            plan.skipped.append(t("batch.will_not_generate", name=name, note=note))
        if not steps and is_album and not left_out:
            plan.tracks += 1
            plan.status.append((t("batch.merged_into_album"), False))
            continue
        if not steps:
            reason = t("common.clause_sep").join(left_out) or t("batch.no_outputs_to_generate")
            plan.status.append((t("batch.skipped_prefix", reason=reason), True))
            continue
        plan.steps += [replace(s, title=f"{name} · {s.title}") for s in steps]
        plan.tracks += 1
        assert project.output_dir is not None
        plan.output_dirs.append(project.output_dir)
        text = t("batch.tasks_count", count=len(steps))
        if left_out:
            text += t("batch.not_generating_suffix", notes=t("common.list_sep").join(left_out))
        plan.status.append((text, bool(left_out)))

    if album_project is not None:
        for panel, tracks in made.items():
            closing = panel.album_plan(album_ctx, tracks)
            plan.steps += [replace(s, title=f"{album_prefix} · {s.title}") for s in closing]
            if closing and album_project.output_dir not in plan.output_dirs:
                assert album_project.output_dir is not None
                plan.output_dirs.append(album_project.output_dir)
    plan.output_dirs = list(dict.fromkeys(plan.output_dirs))
    plan.shares = _summarize_availability(panels, assessed, is_album)
    return plan


def batch_shares(
    panels: Sequence[OutputPlanner],
    projects: list[Project],
    *,
    is_album: bool,
) -> dict[int, tuple[str, bool]]:
    """Summarize partial availability across batch tracks for each enabled output panel."""
    problems = [
        output_problems(build_context(panels, p, batch=True, album=is_album), panels)
        for p in projects
        if p.info is not None and not p.probing
    ]
    return _summarize_availability(panels, problems, is_album)


def _summarize_availability(
    panels: Sequence[OutputPlanner],
    problems: Sequence[dict[int, str]],
    is_album: bool,
) -> dict[int, tuple[str, bool]]:
    missing: dict[int, Counter[str]] = {}
    for track in problems:
        for index, reason in track.items():
            if not (is_album and panels[index].per_album):
                missing.setdefault(index, Counter())[reason] += 1
    shares: dict[int, tuple[str, bool]] = {}
    for index, reasons in missing.items():
        ok = len(problems) - sum(reasons.values())
        why = t("common.clause_sep").join(
            t("batch.shares_track_reason", count=count, reason=reason)
            for reason, count in reasons.items()
        )
        shares[index] = (t("batch.shares_status", ok=ok, total=len(problems), why=why), ok == 0)
    return shares

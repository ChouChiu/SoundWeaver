"""Batch & single-track planning helpers separated from the main window."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from soundweaver.core.i18n import t
from soundweaver.core.project import PlanContext, Project
from soundweaver.core.task import Step
from soundweaver.core.ttml import without_number
from soundweaver.ui.feature import FeaturePanel


@dataclass(slots=True)
class BatchPlan:
    """Every track of a batch planned with the current settings."""

    steps: list[Step] = field(default_factory=list)
    status: list[tuple[str, bool]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    output_dirs: list[Path] = field(default_factory=list)
    inputs: set[Path] = field(default_factory=set)
    tracks: int = 0


def build_context(
    panels: list[FeaturePanel],
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


def panel_why_not(ctx: PlanContext, panel: FeaturePanel) -> str | None:
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


def dir_problem(out: Path | None) -> str | None:
    """Validate that `out` exists or has a writable parent directory."""
    if out is None:
        return t("error.invalid_output_dir")
    if out.exists() and not out.is_dir():
        return t("error.output_not_dir")
    existing = next((p for p in (out, *out.parents) if p.exists()), None)
    if existing is None or not os.access(existing, os.W_OK):
        return t("error.output_no_write")
    return None


def find_target_clash(inputs: set[Path], steps: list[Step]) -> str | None:
    """Detect output targets that collide with an input file or with another step."""
    targets = [target.resolve() for step in steps for target in step.targets]
    resolved = {p.resolve() for p in inputs}
    if hit := next((target for target in targets if target in resolved), None):
        return t("error.clash_input", name=hit.name)
    if twice := [target for target, n in Counter(targets).items() if n > 1]:
        return t("error.clash_steps", name=twice[0].name)
    return None


def plan_track(
    panels: list[FeaturePanel],
    project: Project,
    album: bool,
) -> tuple[dict[FeaturePanel, list[Step]], list[str]]:
    """Compute one batch track's runnable steps per panel and any skipped-output reasons."""
    ctx = build_context(panels, project, batch=True, album=album)
    steps: dict[FeaturePanel, list[Step]] = {}
    left_out: list[str] = []
    for panel in [p for p in panels if p.is_active(ctx)]:
        if album and panel.per_album:
            continue
        if panel.enabled and (reason := panel_why_not(ctx, panel)):
            left_out.append(t("common.with_note", text=panel.title, note=reason))
            continue
        steps[panel] = panel.plan(ctx)
    return steps, left_out


def build_batch_plan(
    panels: list[FeaturePanel],
    projects: list[Project],
    album_project: Project | None,
    *,
    is_album: bool,
) -> BatchPlan:
    """Build the full step plan, per-track statuses, and album-closing steps for a batch."""
    plan = BatchPlan()
    album_prefix = t("batch.album_prefix")
    made: dict[FeaturePanel, list[tuple[Project, list[Step]]]] = {}

    if album_project:
        ctx = build_context(panels, album_project, batch=True)
        for panel in panels:
            if not (panel.enabled and panel.per_album):
                continue
            if problem := panel.problem(ctx):
                plan.skipped.append(
                    t("common.labeled", label=f"{album_prefix} · {panel.title}", text=problem)
                )
                continue
            steps = panel.plan(ctx)
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
        if d_prob := dir_problem(project.output_dir):
            plan.status.append((t("batch.skipped_prefix", reason=d_prob), True))
            continue
        by_panel, left_out = plan_track(panels, project, is_album)
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

    if album_project:
        ctx = build_context(panels, album_project, batch=True)
        for panel, tracks in made.items():
            closing = panel.album_plan(ctx, tracks)
            plan.steps += [replace(s, title=f"{album_prefix} · {s.title}") for s in closing]
            if closing and album_project.output_dir not in plan.output_dirs:
                assert album_project.output_dir is not None
                plan.output_dirs.append(album_project.output_dir)
    return plan


def batch_shares(
    panels: list[FeaturePanel],
    projects: list[Project],
    *,
    is_album: bool,
) -> dict[int, tuple[str, bool]]:
    """Summarize partial availability across batch tracks for each enabled output panel."""
    ready_projects = [p for p in projects if p.info is not None]
    if not ready_projects:
        return {}
    shares: dict[int, tuple[str, bool]] = {}
    for index, panel in enumerate(panels):
        if not panel.enabled or (is_album and panel.per_album):
            continue
        missing: dict[str, int] = {}
        for project in ready_projects:
            ctx = build_context(panels, project, batch=True)
            if p := panel_why_not(ctx, panel):
                missing[p] = missing.get(p, 0) + 1
        if not missing:
            continue
        ok = len(ready_projects) - sum(missing.values())
        why = t("common.clause_sep").join(
            t("batch.shares_track_reason", count=n, reason=p) for p, n in missing.items()
        )
        shares[index] = (t("batch.shares_status", ok=ok, total=len(ready_projects), why=why), ok == 0)
    return shares

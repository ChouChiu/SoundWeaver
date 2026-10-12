"""Motion artwork: a short looping video used as the cover, as Apple Music ships them.

A motion cover is a video file without sound.  Everything that reads a cover
as a picture (the preview, the gradient colours, the background) takes its
first frame; the player video shows it moving, from stills extracted here
(see `renderer/src/player/motion.ts` for why not the video itself).
"""

from __future__ import annotations

import json
import shutil
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from dolbytool.core import ffmpeg
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import cache_dir, content_key, file_stamp

MOTION_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
MANIFEST = "manifest.json"
# extracted artworks kept around; each is some tens of MB
_KEEP = 6
# the cover never fills more than about half the frame's long side
_FRAME_SHARE = 0.5
_MIN_SIDE = 480


@lru_cache(maxsize=256)
def _is_motion(path: Path, _stamp: object) -> bool:
    try:
        streams = ffmpeg.probe(path).get("streams", [])
    except (ffmpeg.FFmpegError, OSError):
        return False
    kinds = {
        s.get("codec_type") for s in streams if not s.get("disposition", {}).get("attached_pic")
    }
    return "video" in kinds and "audio" not in kinds


def is_motion(path: Path | None) -> bool:
    """Whether `path` is a soundless video, i.e. motion artwork rather than a song."""
    if path is None or path.suffix.lower() not in MOTION_SUFFIXES or not path.is_file():
        return False
    return _is_motion(path, file_stamp(path))


def still(cover: Path) -> Path:
    """`cover` as a picture: the first frame of motion artwork, else the file itself."""
    if not is_motion(cover):
        return cover
    target = cache_dir("covers") / f"{cover.stem}_{content_key(cover)[:8]}_still.jpg"
    if not target.is_file():
        ffmpeg.run_ffmpeg(["-i", str(cover), "-an", "-frames:v", "1", "-q:v", "2", str(target)])
    return target


def _fps(path: Path) -> float:
    stream = next(s for s in ffmpeg.probe(path)["streams"] if s.get("codec_type") == "video")
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key, "0/0")
        if rate and not rate.startswith("0") and "/0" not in rate:
            return float(Fraction(rate))
    raise ffmpeg.FFmpegError("无法读取动态封面的帧率")


def side_for(width: int, height: int) -> int:
    """How large the stills need to be for a frame of this size."""
    return max(_MIN_SIDE, round(max(width, height) * _FRAME_SHARE / 2) * 2)


def frames(cover: Path, side: int, token: CancellationToken | None = None) -> Path:
    """`cover` as numbered JPEG stills no larger than `side`, with a manifest {fps, count}.

    Kept in the cache: the same artwork at the same size is extracted once.
    """
    root = cache_dir("motion")
    folder = root / f"{content_key(cover)[:16]}_{side}"
    if (folder / MANIFEST).is_file():
        folder.touch()
        return folder
    partial = root / f".{folder.name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    try:
        fps = _fps(cover)
        # Apple's artwork is fragmented MP4 whose timestamps restart per fragment:
        # number the frames instead, one "second" each, so every one is kept in order.
        fit = f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease"
        ffmpeg.run_ffmpeg(
            ["-i", str(cover), "-an", "-vf", f"setpts=N/TB,{fit}:flags=lanczos",
             "-fps_mode", "passthrough", "-q:v", "3", str(partial / "%05d.jpg")],
            token,
        )  # fmt: skip
        count = sum(1 for _ in partial.glob("*.jpg"))
        if not count:
            raise ffmpeg.FFmpegError("动态封面中没有可用的画面")
        (partial / MANIFEST).write_text(json.dumps({"fps": fps, "count": count}))
        partial.rename(folder)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _prune(root)
    return folder


def _prune(root: Path) -> None:
    done = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    done.sort(key=lambda p: p.stat().st_mtime)
    for stale in done[:-_KEEP]:
        shutil.rmtree(stale, ignore_errors=True)

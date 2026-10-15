/** Render a whole timeline, cut into segments rendered by parallel browsers and joined losslessly. */

import { execFileSync } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { concat, type Codec } from "./ffmpeg";
import { debug, emit, status } from "./log";
import { isAbort, Run } from "./run";
import { renderSegment, splitFrames } from "./segment";
import type { Stage } from "./stage";

// Each browser renders on roughly one core; keep segments long enough to be worth a launch,
// a page load and the preroll replayed before the first frame (6 s on the player page).
const MIN_SEGMENT_SECONDS = 10;

export interface Timeline {
	fps: number;
	crf: number;
	// VP9 with alpha unless a page draws an opaque frame
	codec?: Codec;
	size?: [number, number];
	// JPEG capture: faster, slightly softer (see captureFormat)
	fast?: boolean;
	// first frame on the page's timeline, for a render that starts mid-song
	startFrame?: number;
	out: string;
	seconds: number;
	// upper bound on parallel browsers
	jobs: number;
	// video memory one more segment takes, in MiB, when the browsers draw on the GPU
	vramPerJob?: number;
}

// left for the desktop and whatever else draws on the card meanwhile
const VRAM_MARGIN_MIB = 1024;

/** Free memory on the first NVIDIA card, in MiB; null without one (or without nvidia-smi). */
function freeVram(): number | null {
	try {
		const out = execFileSync("nvidia-smi", ["--query-gpu=memory.free", "--format=csv,noheader,nounits"], {
			encoding: "utf8",
			timeout: 5000,
			stdio: ["ignore", "pipe", "ignore"],
		});
		const free = Number.parseInt(out.split("\n")[0], 10);
		return Number.isFinite(free) ? free : null;
	} catch {
		return null;
	}
}

/** `first` is already open (it usually told us the length); `open` launches the others. */
export async function renderTimeline(
	run: Run,
	first: Stage,
	open: () => Promise<Stage>,
	{ fps, crf, codec = "vp9", size, fast, out, seconds, jobs: maxJobs, startFrame = 0, vramPerJob }: Timeline,
): Promise<void> {
	const total = Math.max(1, Math.round(seconds * fps));
	let jobs = Math.max(1, Math.min(maxJobs, Math.floor(seconds / MIN_SEGMENT_SECONDS)));
	// Every browser draws on the card, and an NVENC encoder opens a CUDA context there
	// too; past what it holds, the encoders fail with CUDA_ERROR_OUT_OF_MEMORY.  `first`
	// is already open, so what is free now pays for the others.
	const free = jobs > 1 && vramPerJob ? freeVram() : null;
	if (free !== null && vramPerJob) {
		const fit = Math.max(1, 1 + Math.floor((free - VRAM_MARGIN_MIB) / vramPerJob));
		debug(`vram: ${free} MiB free, ${vramPerJob} MiB a segment, room for ${fit}`);
		if (fit < jobs) {
			status({ key: "vram_jobs", jobs, fit });
			jobs = fit;
		}
	}
	// Left alone, every encoder starts a thread per core, each holding frames of its
	// own: eight parallel x264 at 1080p took over 9 GB.  Share the cores out instead.
	const threads = jobs > 1 ? Math.max(2, Math.ceil(os.availableParallelism() / jobs)) : undefined;

	let done = 0;
	const onFrame = () => {
		done++;
		if (done % 10 === 0 || done === total) emit({ type: "progress", frame: done, total });
	};

	if (jobs === 1) {
		await renderSegment(run, first, { fps, crf, codec, size, fast, threads }, { from: startFrame, to: startFrame + total, out }, onFrame);
		return;
	}

	status({ key: "parallel_jobs", jobs });
	const tmp = await mkdtemp(path.join(path.dirname(out), ".render-parts-"));
	run.tempDirs.add(tmp);
	const ext = path.extname(out);
	const segments = splitFrames(total, jobs, (i) => path.join(tmp, `part${i}${ext}`), startFrame);
	// wait for every segment, so no browser is still launching when we clean up
	const results = await Promise.allSettled(
		segments.map(async (segment, i) => {
			try {
				const stage = i === 0 ? first : await open();
				await renderSegment(run, stage, { fps, crf, codec, size, fast, threads }, segment, onFrame);
				await stage.close();
				debug(`segment ${i}: browser closed`);
			} catch (error) {
				run.aborted = true;
				throw error;
			}
		}),
	);
	// report the segment that failed first, not the ones it stopped
	const failed = results.find(
		(r): r is PromiseRejectedResult => r.status === "rejected" && !isAbort(r.reason),
	);
	if (failed) throw failed.reason;
	status({ key: "concat_segments" });
	await concat(run, segments.map((s) => s.out), out, tmp, codec);
	debug("concatenated");
}

/** Run `main` under a fresh Run: cancel on signals, clean up, exit non-zero on failure. */
export function start(main: (run: Run) => Promise<void>): void {
	const run = new Run();
	process.on("SIGTERM", () => run.cancel());
	process.on("SIGINT", () => run.cancel());
	main(run)
		.finally(() => run.cleanup())
		.catch((error) => {
			// a cancel exits from its own handler once the browsers are closed
			if (run.cancelling) return;
			console.error(error instanceof Error ? error.message : String(error));
			process.exit(1);
		});
}

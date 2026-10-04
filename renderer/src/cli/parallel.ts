/** Render a whole timeline, cut into segments rendered by parallel browsers and joined losslessly. */

import { mkdtemp } from "node:fs/promises";
import path from "node:path";
import { concat, type Codec } from "./ffmpeg";
import { debug, emit, status } from "./log";
import { isAbort, Run } from "./run";
import { renderSegment, splitFrames } from "./segment";
import type { Stage } from "./stage";

// Each browser renders on roughly one core; keep segments long enough to be worth a launch.
const MIN_SEGMENT_SECONDS = 10;

export interface Timeline {
	fps: number;
	crf: number;
	// VP9 with alpha unless a page draws an opaque frame
	codec?: Codec;
	size?: [number, number];
	// first frame on the page's timeline, for a render that starts mid-song
	startFrame?: number;
	out: string;
	seconds: number;
	// upper bound on parallel browsers
	jobs: number;
}

/** `first` is already open (it usually told us the length); `open` launches the others. */
export async function renderTimeline(
	run: Run,
	first: Stage,
	open: () => Promise<Stage>,
	{ fps, crf, codec = "vp9", size, out, seconds, jobs: maxJobs, startFrame = 0 }: Timeline,
): Promise<void> {
	const total = Math.max(1, Math.round(seconds * fps));
	const jobs = Math.max(1, Math.min(maxJobs, Math.floor(seconds / MIN_SEGMENT_SECONDS)));

	let done = 0;
	const onFrame = () => {
		done++;
		if (done % 10 === 0 || done === total) emit({ type: "progress", frame: done, total });
	};

	if (jobs === 1) {
		await renderSegment(run, first, { fps, crf, codec, size }, { from: startFrame, to: startFrame + total, out }, onFrame);
		return;
	}

	status(`分 ${jobs} 段并行渲染`);
	const tmp = await mkdtemp(path.join(path.dirname(out), ".render-parts-"));
	run.tempDirs.add(tmp);
	const ext = path.extname(out);
	const segments = splitFrames(total, jobs, (i) => path.join(tmp, `part${i}${ext}`), startFrame);
	// wait for every segment, so no browser is still launching when we clean up
	const results = await Promise.allSettled(
		segments.map(async (segment, i) => {
			try {
				const stage = i === 0 ? first : await open();
				await renderSegment(run, stage, { fps, crf, codec, size }, segment, onFrame);
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
	status("拼接分段");
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

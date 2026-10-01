/** Render a frame range of the timeline into one WebM. */

import { encoder } from "./ffmpeg";
import { debug } from "./log";
import type { Run } from "./run";
import type { Stage } from "./stage";

export interface Segment {
	from: number;
	to: number;
	out: string;
}

/**
 * Render frames [from, to) into `out`.  The stage first replays, without
 * screenshots, from where the lines on screen at `from` began fading in, so a
 * segment starts in exactly the state a single continuous run would be in.
 */
export async function renderSegment(
	run: Run,
	stage: Stage,
	fps: number,
	crf: number,
	{ from, to, out }: Segment,
	onFrame: () => void,
): Promise<void> {
	const at = (frame: number) => (frame * 1000) / fps;
	const preroll = Math.floor(((await stage.prerollStart(at(from))) * fps) / 1000);
	debug(`segment ${from}-${to}: preroll from ${preroll}`);
	for (let frame = Math.max(0, preroll); frame < from; frame++) {
		if ((await stage.seek(at(frame))) > 0) await stage.frame(false);
	}
	const { stdin, done } = encoder(run, fps, crf, out);
	// surface an encoder failure on the next frame instead of as an unhandled rejection
	let failure: unknown = null;
	done.catch((error) => {
		failure = error;
	});
	let previous: Buffer | undefined;
	// With no line on screen every frame is the same empty picture: capture it once,
	// then skip the browser for the intro, gaps and outro.
	let blank: Buffer | undefined;
	for (let frame = from; frame < to; frame++) {
		if (failure) throw failure;
		run.throwIfAborted();
		const visible = await stage.seek(at(frame));
		if (visible === 0 && blank) {
			previous = blank;
		} else {
			// No damage means no new screenshot: the picture is unchanged, repeat the last one.
			previous = (await stage.frame(true)) ?? previous ?? (await stage.frame(true));
			if (!previous) throw new Error(`第 ${frame} 帧截图失败`);
			if (visible === 0) blank = previous;
		}
		if (!stdin.write(previous)) {
			// a dead encoder never drains; its exit rejects `done` instead
			await Promise.race([new Promise((r) => stdin.once("drain", r)), done]);
		}
		onFrame();
	}
	stdin.end();
	await done;
	debug(`segment ${from}-${to}: done`);
}

/** Split `total` frames into `jobs` nearly equal consecutive ranges. */
export function splitFrames(total: number, jobs: number, out: (i: number) => string): Segment[] {
	const bounds = Array.from({ length: jobs + 1 }, (_, i) => Math.round((total * i) / jobs));
	return bounds.slice(0, -1).map((from, i) => ({ from, to: bounds[i + 1], out: out(i) }));
}

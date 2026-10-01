/** ffmpeg children: the VP9 + alpha encoder fed PNG frames, and the lossless segment concat. */

import { spawn } from "node:child_process";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import type { Writable } from "node:stream";
import type { Run } from "./run";

const QUIET = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"];

/** Spawn ffmpeg under `run`; `done` rejects with its stderr on a non-zero exit. */
function ffmpeg(run: Run, args: string[], stdin: "pipe" | "ignore"): { stdin: Writable | null; done: Promise<void> } {
	const child = spawn("ffmpeg", [...QUIET, ...args], { stdio: [stdin, "ignore", "pipe"] });
	run.children.add(child);
	let stderr = "";
	child.stderr!.on("data", (d) => {
		stderr += d;
	});
	// EPIPE from a dead encoder; its exit status is what `done` reports
	child.stdin?.on("error", () => {});
	const done = new Promise<void>((resolve, reject) =>
		child.on("close", (code) => {
			run.children.delete(child);
			if (code === 0) resolve();
			else reject(new Error(stderr.trim() || `ffmpeg exited with ${code}`));
		}),
	);
	return { stdin: child.stdin, done };
}

export function encoder(run: Run, fps: number, crf: number, out: string): { stdin: Writable; done: Promise<void> } {
	const { stdin, done } = ffmpeg(
		run,
		[
			"-f", "image2pipe", "-c:v", "png", "-framerate", String(fps), "-i", "pipe:0",
			"-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", String(crf),
			// realtime/6: ~1.7x faster than good/2 here and no worse on flat text, files a bit larger
			"-row-mt", "1", "-auto-alt-ref", "0", "-deadline", "realtime", "-cpu-used", "6",
			"-metadata:s:v:0", "alpha_mode=1",
			out,
		],
		"pipe",
	); // prettier-ignore
	return { stdin: stdin!, done };
}

export async function concat(run: Run, parts: string[], out: string, dir: string): Promise<void> {
	const list = path.join(dir, "parts.txt");
	await writeFile(list, parts.map((p) => `file '${p.replaceAll("'", "'\\''")}'`).join("\n"));
	// stream copy keeps the VP9 alpha (Matroska BlockAdditional) untouched
	await ffmpeg(
		run,
		["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", "-metadata:s:v:0", "alpha_mode=1", out],
		"ignore",
	).done;
}

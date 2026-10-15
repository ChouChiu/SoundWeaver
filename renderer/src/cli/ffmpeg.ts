/** ffmpeg children: the encoder fed captured frames, the lossless segment concat and the audio mux. */

import { spawn } from "node:child_process";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import type { Writable } from "node:stream";
import type { Run } from "./run";

const QUIET = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"];

// vp9 keeps the alpha channel (WebM); the others are opaque MP4
export const CODECS = ["vp9", "libx264", "h264_nvenc", "hevc_nvenc"] as const;
export type Codec = (typeof CODECS)[number];

/**
 * How frames come from the browser.  PNG is lossless; `fast` takes JPEG at
 * quality 90 for an opaque video instead, about half the capture time (the
 * zlib pass on a blurred full-frame background dominates a frame) at a slight
 * softening of fine detail: SSIM 0.9972 against the PNG capture after a CRF 18
 * encode of a 720p player clip, 0.9977 at quality 95.  Alpha always needs PNG.
 */
export function captureFormat({ codec, fast }: Encoding): "png" | "jpeg" {
	return fast && codec !== "vp9" ? "jpeg" : "png";
}

export interface Encoding {
	fps: number;
	// CRF for the software encoders, CQ for NVENC
	crf: number;
	codec: Codec;
	// exact output size, when the screenshots may be a pixel off it (a fractional device scale)
	size?: [number, number];
	// JPEG capture, see captureFormat
	fast?: boolean;
	// threads for this encoder; unset lets ffmpeg take every core
	threads?: number;
}

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
	const done = new Promise<void>((resolve, reject) => {
		// no ffmpeg on PATH, or no process to be had: there is no exit to wait for
		child.on("error", (error) => {
			run.children.delete(child);
			reject(new Error(`无法启动 ffmpeg：${error.message}`));
		});
		child.on("close", (code) => {
			run.children.delete(child);
			if (code === 0) resolve();
			else reject(new Error(stderr.trim() || `ffmpeg exited with ${code}`));
		});
	});
	return { stdin: child.stdin, done };
}

function codecArgs({ codec, crf, fps, threads }: Encoding): string[] {
	const gop = String(Math.round(fps * 2));
	const limit = threads ? ["-threads", String(threads)] : [];
	switch (codec) {
		case "vp9":
			return [
				"-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", String(crf),
				// realtime/6: ~1.7x faster than good/2 here and no worse on flat text, files a bit
				// larger; screen tuning and tile columns suit large flat areas and spread rows over cores
				"-row-mt", "1", "-auto-alt-ref", "0", "-deadline", "realtime", "-cpu-used", "6",
				"-tune-content", "screen", "-tile-columns", "2",
				"-metadata:s:v:0", "alpha_mode=1", ...limit,
			]; // prettier-ignore
		case "libx264":
			// faster rather than medium: less CPU per frame for files somewhat larger at the same CRF
			return [
				"-c:v", "libx264", "-preset", "faster", "-crf", String(crf), "-pix_fmt", "yuv420p", "-g", gop,
				...limit,
			]; // prettier-ignore
		case "h264_nvenc":
		case "hevc_nvenc":
			return [
				// p4 is the balanced NVENC preset; without a maxrate NVENC caps the bitrate (about
				// 35 Mbps at 1080p60), and a CQ below ~10 stops buying anything
				"-c:v", codec, "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", String(crf), "-b:v", "0",
				"-maxrate", "500M", "-bufsize", "1000M",
				"-spatial-aq", "1", "-pix_fmt", "yuv420p", "-g", gop,
				...(codec === "hevc_nvenc" ? ["-tag:v", "hvc1"] : []),
			]; // prettier-ignore
	}
}

export function encoder(run: Run, encoding: Encoding, out: string): { stdin: Writable; done: Promise<void> } {
	const jpeg = captureFormat(encoding) === "jpeg";
	const filters = [
		...(encoding.size ? [`scale=${encoding.size[0]}:${encoding.size[1]}:flags=lanczos`] : []),
		// JPEG decodes to full range; video players expect limited range and would show it off
		...(jpeg ? ["scale=in_range=pc:out_range=tv", "setparams=range=tv"] : []),
	];
	const { stdin, done } = ffmpeg(
		run,
		[
			"-f", "image2pipe", "-c:v", jpeg ? "mjpeg" : "png",
			"-framerate", String(encoding.fps),
			// the picture decoder too: by default it opens a frame thread per core
			...(encoding.threads ? ["-threads", String(encoding.threads)] : []),
			"-i", "pipe:0",
			...(filters.length ? ["-vf", filters.join(",")] : []),
			...codecArgs(encoding), out,
		],
		"pipe",
	);
	return { stdin: stdin!, done };
}

/** Muxer flags for `out`: MP4 puts its index up front so playback starts before the download ends. */
function containerArgs(out: string): string[] {
	return [".mp4", ".mov", ".m4v"].includes(path.extname(out).toLowerCase()) ? ["-movflags", "+faststart"] : [];
}

export async function concat(run: Run, parts: string[], out: string, dir: string, codec: Codec = "vp9"): Promise<void> {
	const list = path.join(dir, "parts.txt");
	await writeFile(list, parts.map((p) => `file '${p.replaceAll("'", "'\\''")}'`).join("\n"));
	// stream copy keeps the VP9 alpha (Matroska BlockAdditional) untouched
	const tail = codec === "vp9" ? ["-metadata:s:v:0", "alpha_mode=1"] : containerArgs(out);
	await ffmpeg(run, ["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", ...tail, out], "ignore").done;
}

export type AudioMode = "none" | "encode" | "copy";

/** Put the first audio stream of `audio` next to the finished picture, cut to the picture's length. */
export async function muxAudio(
	run: Run,
	video: string,
	audio: string,
	mode: AudioMode,
	out: string,
	// seconds into the song where the picture starts
	start = 0,
): Promise<void> {
	const webm = path.extname(out).toLowerCase() === ".webm";
	// WebM only holds Opus / Vorbis, so a copy there would fail on most sources;
	// Matroska takes anything as is, PCM stems and E-AC-3 JOC included
	const codec = mode === "copy" && !webm ? ["-c:a", "copy"] : webm ? ["-c:a", "libopus", "-b:a", "256k"] : ["-c:a", "aac", "-b:a", "320k"];
	const tail = webm ? ["-metadata:s:v:0", "alpha_mode=1"] : containerArgs(out);
	await ffmpeg(
		run,
		["-i", video, ...(start > 0 ? ["-ss", start.toFixed(3)] : []), "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", ...codec, "-shortest", ...tail, out],
		"ignore",
	).done;
}

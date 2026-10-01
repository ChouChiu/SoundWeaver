/** Command line → typed render options. */

import os from "node:os";
import path from "node:path";
import { parseArgs } from "node:util";
import type { StageOptions } from "../shared/api";

export interface RenderOptions {
	ttml: string;
	out: string;
	width: number;
	height: number;
	fps: number;
	stage: StageOptions;
	// seconds; undefined means until the last line has faded out
	duration?: number;
	crf: number;
	// parallel browsers
	jobs: number;
}

// measured on 20 threads: 8 browsers best, more start fighting over the CPU
export const defaultJobs = () => Math.max(1, Math.floor(os.availableParallelism() / 2.5));

export function parseOptions(argv = process.argv.slice(2)): RenderOptions {
	const { values } = parseArgs({
		args: argv,
		options: {
			ttml: { type: "string" },
			out: { type: "string" },
			width: { type: "string", default: "1920" },
			height: { type: "string", default: "240" },
			fps: { type: "string", default: "60" },
			"font-size": { type: "string", default: "64" },
			font: { type: "string", default: "sans-serif" },
			fade: { type: "string", default: "200" },
			// translation opacity in percent
			"sub-opacity": { type: "string", default: "70" },
			duration: { type: "string" },
			crf: { type: "string", default: "30" },
			// parallel browsers; default one per 2.5 cores, fewer for short songs
			jobs: { type: "string" },
		},
	});
	if (!values.ttml || !values.out) {
		console.error("usage: render.mjs --ttml <file> --out <file.webm> [options]");
		process.exit(2);
	}
	return {
		ttml: path.resolve(values.ttml),
		out: path.resolve(values.out),
		width: number("width", values.width, 2),
		height: number("height", values.height, 2),
		fps: number("fps", values.fps, 1),
		stage: {
			fontFamily: values.font!,
			fontSize: number("font-size", values["font-size"], 1),
			fadeMs: number("fade", values.fade, 0),
			subOpacity: number("sub-opacity", values["sub-opacity"], 0, 100) / 100,
		},
		duration: values.duration === undefined ? undefined : number("duration", values.duration, 0.001),
		crf: number("crf", values.crf, 0, 63),
		jobs: values.jobs === undefined ? defaultJobs() : Math.floor(number("jobs", values.jobs, 1)),
	};
}

/** Parse a numeric flag, rejecting anything outside [min, max] instead of rendering with NaN. */
export function number(name: string, text: string | undefined, min: number, max = Infinity): number {
	const value = Number(text);
	if (!Number.isFinite(value) || value < min || value > max) {
		console.error(`invalid --${name}: ${text}`);
		process.exit(2);
	}
	return value;
}

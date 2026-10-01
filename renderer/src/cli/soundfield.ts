/**
 * Offline sound field renderer: an object trace (JSON from the Python side) in,
 * transparent VP9 WebM of the room and its moving objects out.
 *
 * Same frame-by-frame machinery as the lyric renderer.  WebGL goes through
 * ANGLE on Vulkan: on the GPU it is several times faster than SwiftShader and
 * still draws the same pixels on every run (ANGLE on GL-EGL does not); without
 * a usable GPU Chrome falls back to SwiftShader, slower but also deterministic.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import type { SoundfieldOptions, SoundfieldScene, ViewPreset } from "../shared/api";
import { headlessShell } from "./chrome";
import { debug, status } from "./log";
import { defaultJobs, number } from "./options";
import { renderTimeline, start } from "./parallel";
import { Stage, type StageSettings } from "./stage";

const VIEWS: ViewPreset[] = ["iso", "top", "angle"];
const WEBGL_ARGS = [
	"--use-angle=vulkan",
	"--enable-features=Vulkan",
	"--enable-gpu",
	"--ignore-gpu-blocklist",
	"--enable-unsafe-swiftshader",
];

function parseOptions(argv = process.argv.slice(2)) {
	const { values } = parseArgs({
		args: argv,
		options: {
			scene: { type: "string" },
			out: { type: "string" },
			head: { type: "string" },
			width: { type: "string", default: "1920" },
			height: { type: "string", default: "1080" },
			fps: { type: "string", default: "60" },
			view: { type: "string", default: "iso" },
			"object-scale": { type: "string", default: "1.3" },
			"head-scale": { type: "string", default: "2" },
			"gate-db": { type: "string", default: "-90" },
			fade: { type: "string", default: "180" },
			dim: { type: "string", default: "0.25" },
			duration: { type: "string" },
			crf: { type: "string", default: "30" },
			jobs: { type: "string" },
		},
	});
	if (!values.scene || !values.out) {
		console.error("usage: soundfield.mjs --scene <trace.json> --out <file.webm> [options]");
		process.exit(2);
	}
	const view = values.view as ViewPreset;
	if (!VIEWS.includes(view)) {
		console.error(`invalid --view: ${values.view}`);
		process.exit(2);
	}
	const options: SoundfieldOptions = {
		view,
		objectScale: number("object-scale", values["object-scale"], 0.01),
		headScale: number("head-scale", values["head-scale"], 0.01),
		gateDb: number("gate-db", values["gate-db"], -200, 0),
		fadeMs: number("fade", values.fade, 0),
		dim: number("dim", values.dim, 0, 1),
	};
	return {
		scene: path.resolve(values.scene),
		out: path.resolve(values.out),
		head: values.head ? path.resolve(values.head) : null,
		width: number("width", values.width, 2),
		height: number("height", values.height, 2),
		fps: number("fps", values.fps, 1),
		options,
		duration: values.duration === undefined ? undefined : number("duration", values.duration, 0.001),
		crf: number("crf", values.crf, 0, 63),
		jobs: values.jobs === undefined ? defaultJobs() : Math.floor(number("jobs", values.jobs, 1)),
	};
}

start(async (run) => {
	const opts = parseOptions();
	const scene = JSON.parse(await readFile(opts.scene, "utf8")) as SoundfieldScene;
	const glb = opts.head ? (await readFile(opts.head)).toString("base64") : null;
	const settings: StageSettings = {
		executablePath: await headlessShell(),
		html: "soundfield.html",
		width: opts.width,
		height: opts.height,
		fps: opts.fps,
		args: WEBGL_ARGS,
		setup: async (stage) => {
			const count = await stage.pump(
				stage.page.evaluate((s, o, g) => window.soundfield.load(s, o, g), scene, opts.options, glb),
			);
			if (!count) throw new Error("全景声码流中没有动态对象");
			debug(`soundfield loaded, ${count} objects, WebGL on ${await stage.page.evaluate(() => window.soundfield.gpu())}`);
		},
	};

	status("启动浏览器");
	const first = await Stage.open(run, settings);
	const seconds = opts.duration ?? (await first.endTime()) / 1000;
	await renderTimeline(run, first, () => Stage.open(run, settings), { ...opts, seconds });
});

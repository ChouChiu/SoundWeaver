/**
 * Offline lyric renderer: TTML in, transparent VP9 WebM out.
 *
 * chrome-headless-shell runs in deterministic mode, so the compositor only
 * produces a frame when we call HeadlessExperimental.beginFrame, and every
 * clock the page sees (rAF, Web Animations, CSS transitions) advances by
 * exactly one frame interval per call.  Frames come back as PNG with alpha and
 * go straight into ffmpeg's stdin.  Long songs are cut into segments rendered
 * by parallel browsers and joined without re-encoding.
 *
 * stdout carries one JSON object per line for the Python side, see log.ts.
 */

import { readFile } from "node:fs/promises";
import type { StageOptions } from "../shared/api";
import { headlessShell } from "./chrome";
import { debug, status } from "./log";
import { parseOptions } from "./options";
import { renderTimeline, start } from "./parallel";
import { Stage, type StageSettings } from "./stage";

/** Load the lyrics, wait for fonts and layout, then park every player at its line. */
async function setup(stage: Stage, ttml: string, options: StageOptions): Promise<void> {
	const count = await stage.page.evaluate((text, opts) => window.amllRender.load(text, opts), ttml, options);
	if (!count) throw new Error("TTML 中没有可显示的歌词行");
	await stage.pump(stage.page.evaluate(() => document.fonts.ready.then(() => true)));
	// let ResizeObserver report the final line sizes, then park the springs
	for (let i = 0; i < 5; i++) await stage.frame(false);
	await stage.page.evaluate(() => window.amllRender.settle());
	for (let i = 0; i < 5; i++) await stage.frame(false);
	debug("lyrics loaded");
}

start(async (run) => {
	const opts = parseOptions();
	const ttml = await readFile(opts.ttml, "utf8");
	const settings: StageSettings = {
		executablePath: await headlessShell(),
		html: "index.html",
		width: opts.width,
		height: opts.height,
		fps: opts.fps,
		setup: (stage) => setup(stage, ttml, opts.stage),
	};

	status("启动浏览器");
	const first = await Stage.open(run, settings);
	const seconds = opts.duration ?? (await first.endTime()) / 1000 + 0.5;
	await renderTimeline(run, first, () => Stage.open(run, settings), { ...opts, seconds });
});

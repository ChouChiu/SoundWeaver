/** One headless browser holding a render page, advanced one BeginFrame at a time. */

import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer, { type Browser, type CDPSession, type Page } from "puppeteer-core";
import { BROWSER_ARGS } from "./chrome";
import { debug } from "./log";
import type { Run } from "./run";

// the bundle sits next to the built page in dist/
const here = path.dirname(fileURLToPath(import.meta.url));
// quality of the opaque capture fed to the encoder; see the SSIM note in ffmpeg.ts
const JPEG_QUALITY = 90;

export interface StageSettings {
	executablePath: string;
	// page file in dist/
	html: string;
	width: number;
	height: number;
	fps: number;
	// device pixel ratio: the page lays out at width / scale CSS pixels and is drawn at full size
	scale?: number;
	// browser flags on top of the deterministic ones
	args?: string[];
	// load the content once the page has navigated
	setup: (stage: Stage) => Promise<void>;
}

export class Stage {
	private frameTime: number;
	// the first uncaught error of the page: whatever it draws after that cannot be trusted
	private pageError: string | null = null;
	// when the page shows anything, sorted and merged; null when it cannot say
	private windows: [number, number][] | null = null;

	private constructor(
		private readonly run: Run,
		private readonly browser: Browser,
		readonly page: Page,
		private readonly cdp: CDPSession,
		private readonly interval: number,
	) {
		// frameTimeTicks is on Chrome's monotonic clock (CLOCK_MONOTONIC on Linux);
		// values behind it are ignored and the frame falls back to a 60 Hz step.
		this.frameTime = Number(process.hrtime.bigint() / 1_000_000n);
	}

	static async open(run: Run, settings: StageSettings): Promise<Stage> {
		const { fps } = settings;
		const scale = settings.scale ?? 1;
		// window and target sizes are CSS pixels; the screenshot comes back in device pixels
		const width = Math.round(settings.width / scale);
		const height = Math.round(settings.height / scale);
		const browser = await puppeteer.launch({
			executablePath: settings.executablePath,
			headless: "shell",
			defaultViewport: null,
			args: [
				`--window-size=${width},${height}`,
				...BROWSER_ARGS.filter((a) => !a.startsWith("--force-device-scale-factor")),
				`--force-device-scale-factor=${scale}`,
				...(settings.args ?? []),
			],
		});
		run.browsers.add(browser);
		debug(`browser ${browser.process()?.pid} launched`);
		try {
			// Begin-frame control has to be requested when the target is created.
			const cdpBrowser = await browser.target().createCDPSession();
			const { targetId } = (await cdpBrowser.send("Target.createTarget", {
				url: "about:blank",
				width,
				height,
				enableBeginFrameControl: true,
			})) as { targetId: string };
			// puppeteer keeps the CDP target id private; there is no public accessor
			const target = await browser.waitForTarget(
				(t) => (t as unknown as { _targetId: string })._targetId === targetId,
			);
			const page = (await target.page())!;
			const cdp = await page.createCDPSession();
			await cdp.send("Emulation.setDefaultBackgroundColorOverride", {
				color: { r: 0, g: 0, b: 0, a: 0 },
			});
			const stage = new Stage(run, browser, page, cdp, 1000 / fps);
			page.on("pageerror", (error) => stage.noteError(error));
			page.on("console", (message) => debug(`console ${message.type()}: ${message.text()}`));
			debug(`browser ${browser.process()?.pid} target ready`);
			await stage.navigate(settings.html);
			stage.throwIfBroken();
			try {
				await settings.setup(stage);
				const windows = await stage.page.evaluate(() => window.renderPage.visibleWindows?.());
				stage.windows = windows ? mergeWindows(windows) : null;
			} catch (error) {
				// the page's own error says more than the check that tripped over it
				stage.throwIfBroken();
				throw error;
			}
			stage.throwIfBroken();
			debug(`browser ${browser.process()?.pid} loaded`);
			return stage;
		} catch (error) {
			await browser.close();
			run.browsers.delete(browser);
			throw error;
		}
	}

	private async navigate(html: string): Promise<void> {
		// Navigation and font loading need frames to make progress.
		const url = pathToFileURL(path.join(here, html)).href;
		await this.pump(this.page.goto(url, { waitUntil: "load" }), true);
		debug("page navigated");
	}

	/**
	 * Keep issuing frames until `task` settles: under begin-frame control the page
	 * does not advance on its own, so waiting on it without frames can deadlock.
	 */
	async pump<T>(task: Promise<T>, tolerateFrameErrors = false): Promise<T> {
		let settled = false;
		const watched = task.finally(() => {
			settled = true;
		});
		watched.catch(() => {});
		for (let i = 0; !settled || i < 10; i++) {
			const frame = this.frame(false);
			if (tolerateFrameErrors) {
				// before the first navigation commits a frame may fail or never be answered
				frame.catch(() => {});
				await Promise.race([frame, new Promise((r) => setTimeout(r, 500))]).catch(() => {});
			} else {
				await frame;
			}
			await new Promise((r) => setTimeout(r, 5));
		}
		return watched;
	}

	endTime(): Promise<number> {
		return this.page.evaluate(() => window.renderPage.endTime());
	}

	prerollStart(ms: number): Promise<number> {
		return this.page.evaluate((t) => window.renderPage.prerollStart(t), ms);
	}

	/** Show the page at `ms`, and wait for whatever the page still loads for it (a motion artwork still). */
	async seek(ms: number): Promise<number> {
		this.throwIfBroken();
		const visible = await this.page.evaluate(async (t) => {
			const visible = window.renderPage.seek(t);
			await window.renderPage.ready?.();
			return visible;
		}, ms);
		this.throwIfBroken();
		return visible;
	}

	/**
	 * Whether the page may show anything at `ms`.  Outside the windows the page
	 * reported every frame is the empty picture, so a caller holding one can
	 * skip the browser; a page that reports none may show something anywhere.
	 */
	isVisible(ms: number): boolean {
		const windows = this.windows;
		if (windows === null) return true;
		// the last window starting at or before `ms`; they are merged, so it is the only candidate
		let lo = 0;
		let hi = windows.length;
		while (lo < hi) {
			const mid = (lo + hi) >> 1;
			if (windows[mid][0] <= ms) lo = mid + 1;
			else hi = mid;
		}
		return lo > 0 && ms <= windows[lo - 1][1];
	}

	private noteError(error: unknown): void {
		const message = error instanceof Error ? error.message || error.name : String(error);
		debug(`page error: ${message}`);
		this.pageError ??= message;
	}

	/**
	 * Fail on an uncaught page error.  A page that threw may keep answering with
	 * blank or half-built frames (React unmounts the whole player on a render
	 * error), which would otherwise be encoded as a black video.
	 */
	throwIfBroken(): void {
		if (this.pageError !== null) throw new Error(`渲染页面出错：${this.pageError}`);
	}

	/** Advance one frame interval; with a `screenshot` format, return the picture (none when nothing changed). */
	async frame(screenshot: "png" | "jpeg" | false): Promise<Buffer | undefined> {
		this.frameTime += this.interval;
		const result = (await this.cdp.send("HeadlessExperimental.beginFrame", {
			frameTimeTicks: this.frameTime,
			interval: this.interval,
			noDisplayUpdates: false,
			// optimizeForSpeed only lowers the zlib effort, the PNG stays lossless
			...(screenshot === "png" ? { screenshot: { format: "png" as const, optimizeForSpeed: true } } : {}),
			...(screenshot === "jpeg" ? { screenshot: { format: "jpeg" as const, quality: JPEG_QUALITY } } : {}),
		})) as { hasDamage: boolean; screenshotData?: string };
		return result.screenshotData ? Buffer.from(result.screenshotData, "base64") : undefined;
	}

	async close(): Promise<void> {
		await this.browser.close();
		this.run.browsers.delete(this.browser);
	}
}

/** `windows` sorted by start, overlapping and touching ones joined. */
function mergeWindows(windows: [number, number][]): [number, number][] {
	const merged: [number, number][] = [];
	for (const [from, until] of [...windows].sort((a, b) => a[0] - b[0])) {
		const last = merged.at(-1);
		if (last && from <= last[1]) last[1] = Math.max(last[1], until);
		else merged.push([from, until]);
	}
	return merged;
}

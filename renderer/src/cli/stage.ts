/** One headless browser holding a render page, advanced one BeginFrame at a time. */

import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer, { type Browser, type CDPSession, type Page } from "puppeteer-core";
import { BROWSER_ARGS } from "./chrome";
import { debug } from "./log";
import type { Run } from "./run";

// the bundle sits next to the built page in dist/
const here = path.dirname(fileURLToPath(import.meta.url));

export interface StageSettings {
	executablePath: string;
	// page file in dist/
	html: string;
	width: number;
	height: number;
	fps: number;
	// browser flags on top of the deterministic ones
	args?: string[];
	// load the content once the page has navigated
	setup: (stage: Stage) => Promise<void>;
}

export class Stage {
	private frameTime: number;

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
		const { width, height, fps } = settings;
		const browser = await puppeteer.launch({
			executablePath: settings.executablePath,
			headless: "shell",
			defaultViewport: null,
			args: [`--window-size=${width},${height}`, ...BROWSER_ARGS, ...(settings.args ?? [])],
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
			debug(`browser ${browser.process()?.pid} target ready`);
			await stage.navigate(settings.html);
			await settings.setup(stage);
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

	seek(ms: number): Promise<number> {
		return this.page.evaluate((t) => window.renderPage.seek(t), ms);
	}

	/** Advance one frame interval; with `screenshot`, return the PNG (none when nothing changed). */
	async frame(screenshot: boolean): Promise<Buffer | undefined> {
		this.frameTime += this.interval;
		const result = (await this.cdp.send("HeadlessExperimental.beginFrame", {
			frameTimeTicks: this.frameTime,
			interval: this.interval,
			noDisplayUpdates: false,
			// optimizeForSpeed only lowers the zlib effort, the PNG stays lossless
			...(screenshot ? { screenshot: { format: "png" as const, optimizeForSpeed: true } } : {}),
		})) as { hasDamage: boolean; screenshotData?: string };
		return result.screenshotData ? Buffer.from(result.screenshotData, "base64") : undefined;
	}

	async close(): Promise<void> {
		await this.browser.close();
		this.run.browsers.delete(this.browser);
	}
}

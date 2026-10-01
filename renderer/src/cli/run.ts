/**
 * Everything a run has started, so a failure or a cancel can tear it all down:
 * child processes, browsers, and the directory of segment files.
 */

import type { ChildProcess } from "node:child_process";
import { rmSync } from "node:fs";
import { rm } from "node:fs/promises";
import type { Browser } from "puppeteer-core";

export const ABORTED = "aborted";

export class Run {
	readonly children = new Set<ChildProcess>();
	// every launched browser, registered before it loads so a cancel can close it
	readonly browsers = new Set<Browser>();
	// set when one segment fails or the run is cancelled; the other segments stop at their next frame
	aborted = false;
	cancelling = false;
	// segment directory of a parallel run, removed on exit and on cancel
	partsDir: string | null = null;

	throwIfAborted(): void {
		if (this.aborted) throw new Error(ABORTED);
	}

	closeBrowsers(): Promise<unknown> {
		return Promise.allSettled([...this.browsers].map((b) => b.close()));
	}

	/** Normal end of a run, successful or not. */
	async cleanup(): Promise<void> {
		await this.closeBrowsers();
		if (this.partsDir) await rm(this.partsDir, { recursive: true, force: true });
	}

	/** SIGTERM / SIGINT: kill everything and exit once the browsers are closed. */
	cancel(): void {
		this.aborted = true;
		this.cancelling = true;
		for (const child of this.children) child.kill("SIGKILL");
		this.closeBrowsers().finally(() => {
			if (this.partsDir) rmSync(this.partsDir, { recursive: true, force: true });
			process.exit(130);
		});
	}
}

export function isAbort(error: unknown): boolean {
	return error instanceof Error && error.message === ABORTED;
}

/**
 * Everything a run has started, so a failure or a cancel can tear it all down:
 * child processes, browsers, and the scratch directories (segment files, the
 * picture-only video before the audio goes in).
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
	// scratch directories, removed on exit and on cancel
	readonly tempDirs = new Set<string>();

	throwIfAborted(): void {
		if (this.aborted) throw new Error(ABORTED);
	}

	closeBrowsers(): Promise<unknown> {
		return Promise.allSettled([...this.browsers].map((b) => b.close()));
	}

	/** Normal end of a run, successful or not. */
	async cleanup(): Promise<void> {
		await this.closeBrowsers();
		await Promise.all([...this.tempDirs].map((dir) => rm(dir, { recursive: true, force: true })));
	}

	/** SIGTERM / SIGINT: kill everything and exit once the browsers are closed. */
	cancel(): void {
		this.aborted = true;
		this.cancelling = true;
		for (const child of this.children) child.kill("SIGKILL");
		this.closeBrowsers().finally(() => {
			for (const dir of this.tempDirs) rmSync(dir, { recursive: true, force: true });
			process.exit(130);
		});
	}
}

export function isAbort(error: unknown): boolean {
	return error instanceof Error && error.message === ABORTED;
}

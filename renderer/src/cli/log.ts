/** stdout carries one JSON object per line for the Python side; stderr takes debug timings. */

const started = performance.now();

export function debug(message: string): void {
	if (process.env.DOLBYTOOL_DEBUG) console.error(`[${((performance.now() - started) / 1000).toFixed(2)}s] ${message}`);
}

export type Status =
	| { key: "download_chrome"; build: string }
	| { key: "download_chrome_progress"; percent: number }
	| { key: "launch_browser" }
	| { key: "vram_jobs"; jobs: number; fit: number }
	| { key: "parallel_jobs"; jobs: number }
	| { key: "concat_segments" }
	| { key: "analyse_audio" }
	| { key: "mux_audio" };

/** A status names its text by key (with its values) for the Python side to put in the user's language. */
export type Event =
	| ({ type: "status" } & Status)
	| { type: "progress"; frame: number; total: number }
	| { type: "error"; key: string; [value: string]: string | number };

export function emit(event: Event): void {
	process.stdout.write(`${JSON.stringify(event)}\n`);
}

export function status(event: Status): void {
	emit({ type: "status", ...event });
}

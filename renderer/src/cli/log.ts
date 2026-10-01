/** stdout carries one JSON object per line for the Python side; stderr takes debug timings. */

const started = performance.now();

export function debug(message: string): void {
	if (process.env.DOLBYTOOL_DEBUG) console.error(`[${((performance.now() - started) / 1000).toFixed(2)}s] ${message}`);
}

export type Event = { type: "status"; message: string } | { type: "progress"; frame: number; total: number };

export function emit(event: Event): void {
	process.stdout.write(`${JSON.stringify(event)}\n`);
}

export function status(message: string): void {
	emit({ type: "status", message });
}

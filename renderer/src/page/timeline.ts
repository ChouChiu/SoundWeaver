/**
 * When each lyric line is on screen, and how opaque, as pure functions of time.
 * No DOM here: the stage applies what these decide.
 */

import type { LyricLine } from "@applemusic-like-lyrics/core";

// A finished line stays up this long when the next one is not due yet.
export const HOLD_MS = 1000;

export interface LineWindow {
	// on screen, fades included
	showFrom: number;
	showUntil: number;
	// full opacity between these two
	solidFrom: number;
	solidUntil: number;
}

/** Main lyric and translation only: drop background vocals, romanisation and duet sides. */
export function cleanLines(lines: LyricLine[]): LyricLine[] {
	return lines
		.filter((line) => !line.isBG && line.words.some((w) => w.word.trim()))
		.map((line) => ({
			...line,
			words: line.words.map((word) => ({ ...word, romanWord: "" })),
			romanLyric: "",
			isDuet: false,
			isBG: false,
		}))
		.sort((a, b) => a.startTime - b.startTime);
}

/**
 * Out, then in: a line is gone by the time the next one starts fading in, and
 * the next is fully shown on its first word, even if this one runs long.
 */
export function lineWindows(lines: LyricLine[], fade: number): LineWindow[] {
	return lines.map((line, i) => {
		const next = lines[i + 1];
		const held = line.endTime + HOLD_MS;
		const solidUntil = Math.max(line.startTime, next ? Math.min(held, next.startTime - 2 * fade) : held);
		return {
			showFrom: line.startTime - fade,
			showUntil: solidUntil + fade,
			solidFrom: line.startTime,
			solidUntil,
		};
	});
}

export function opacityAt(w: LineWindow, time: number): number {
	if (time < w.showFrom || time > w.showUntil) return 0;
	if (time < w.solidFrom) return (time - w.showFrom) / (w.solidFrom - w.showFrom);
	if (time > w.solidUntil) return (w.showUntil - time) / (w.showUntil - w.solidUntil);
	return 1;
}

/** Earliest fade-in among the lines on screen at `time`, so a replay from there reproduces it. */
export function prerollStart(windows: LineWindow[], time: number): number {
	let start = time;
	for (const w of windows) {
		if (time >= w.showFrom && time <= w.showUntil) start = Math.min(start, w.showFrom);
	}
	return start;
}

export function endTime(windows: LineWindow[]): number {
	return windows.reduce((end, w) => Math.max(end, w.showUntil), 0);
}

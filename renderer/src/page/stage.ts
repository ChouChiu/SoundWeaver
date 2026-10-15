/**
 * Single-line lyric stage driven frame by frame from the CLI.
 *
 * Every lyric line gets its own AMLL player holding only that line, all stacked
 * in the centre of the stage.  AMLL draws the per-word highlight; which line is
 * shown, and the cross-fade between neighbours, comes from timeline.ts.
 */

import { type LyricLine, LyricPlayer } from "@applemusic-like-lyrics/core";
import { parseTTML } from "@applemusic-like-lyrics/ttml";
import { withoutCredits } from "../shared/credits";
import type { AmllRender, RenderPage, StageOptions } from "../shared/api";
import * as timeline from "./timeline";

interface Slot extends timeline.LineWindow {
	line: LyricLine;
	wrapper: HTMLDivElement;
	player: LyricPlayer;
	// AMLL treats the line as over once its last word ends, whatever the <p> says
	lastWordEnd: number;
	lastTime: number | null;
}

function makePlayer(line: LyricLine, parent: HTMLElement): LyricPlayer {
	const player = new LyricPlayer();
	// attach first: AMLL measures the line while building it
	parent.appendChild(player.getElement());
	player.setEnableAutoSeekDetection(false);
	player.setEnableBlur(false);
	player.setEnableScale(false);
	player.setAlignAnchor("center");
	player.setAlignPosition(0.5);
	player.setLyricLines([line], line.startTime - 1);
	// Stay paused and seek every frame: AMLL then pins each word animation's
	// currentTime to the lyric time instead of letting the browser clock run it.
	player.pause();
	return player;
}

/** Run a player's springs to rest, so its line appears in place instead of sliding in. */
function rest(player: LyricPlayer, steps: number): void {
	for (let i = 0; i < steps; i++) player.update(100);
}

export class Stage implements AmllRender, RenderPage {
	private slots: Slot[] = [];
	private end = 0;

	constructor(
		private readonly root: HTMLElement,
		// Always dirty some style, so every BeginFrame runs a full main frame.  Kept on
		// its own element: set on the stage, the inherited property would restyle every player.
		private readonly tick: HTMLElement,
	) {}

	load(ttml: string, options: StageOptions): number {
		this.root.style.setProperty("--lyric-font-family", options.fontFamily);
		this.root.style.setProperty("--amll-lp-font-size", `${options.fontSize}px`);
		this.root.style.setProperty("--sub-opacity", String(options.subOpacity));
		this.root.replaceChildren();
		for (const slot of this.slots) slot.player.dispose();

		const parsed = parseTTML(ttml);
		let lines = timeline.cleanLines(parsed.lines as LyricLine[]);
		if (options.filterCredits) {
			const meta = parsed.metadata as [string, string[]][];
			const names = meta.filter(([key]) => key === "musicName" || key === "artists").flatMap(([, v]) => v);
			lines = withoutCredits(lines, names);
		}
		const windows = timeline.lineWindows(lines, options.fadeMs);
		this.slots = lines.map((line, i) => {
			const wrapper = document.createElement("div");
			wrapper.className = "slot";
			wrapper.style.visibility = "hidden";
			this.root.appendChild(wrapper);
			return {
				...windows[i],
				line,
				wrapper,
				player: makePlayer(line, wrapper),
				lastWordEnd: Math.min(line.endTime, Math.max(...line.words.map((w) => w.endTime))),
				lastTime: null,
			};
		});
		this.end = timeline.endTime(windows);
		return lines.length;
	}

	settle(): void {
		for (const slot of this.slots) {
			slot.player.setCurrentTime(slot.line.startTime, true);
			rest(slot.player, 50);
		}
	}

	prerollStart(time: number): number {
		return timeline.prerollStart(this.slots, time);
	}

	endTime(): number {
		return this.end;
	}

	visibleWindows(): [number, number][] {
		return this.slots.map((s) => [s.showFrom, s.showUntil]);
	}

	seek(time: number): number {
		let visible = 0;
		this.tick.style.setProperty("--frame-time", String(time));
		for (const slot of this.slots) {
			const opacity = timeline.opacityAt(slot, time);
			if (opacity <= 0) {
				if (slot.lastTime !== null) {
					slot.wrapper.style.visibility = "hidden";
					slot.lastTime = null;
				}
				continue;
			}
			if (slot.lastTime === null) slot.wrapper.style.visibility = "";
			// AMLL lays out and dims a line differently before its start and after its
			// end; keep it in its active state for the whole time it is on screen.
			slot.player.setCurrentTime(Math.min(Math.max(time, slot.line.startTime), slot.lastWordEnd - 1), true);
			if (slot.lastTime === null) {
				// the seek above restarts the springs; run them to rest so the line appears in place
				rest(slot.player, 30);
			} else {
				slot.player.update(time - slot.lastTime);
			}
			slot.lastTime = time;
			slot.wrapper.style.opacity = opacity.toFixed(4);
			visible++;
		}
		return visible;
	}
}

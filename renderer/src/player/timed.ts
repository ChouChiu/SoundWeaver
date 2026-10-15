/**
 * AMLL classes handed to PrebuiltLyricPlayer through its implementation atoms,
 * changed only where a rendered video needs it.
 *
 * Backgrounds: AMLL's renderers accumulate their own clock from frame to
 * frame, so a segment rendered by another browser, starting mid-song, would
 * pick up the flow somewhere else.  These draw each frame as a function of the
 * song position instead.  Their layout is rolled from Math.random once the
 * cover has loaded, which happens at a different point of the page's random
 * sequence in every browser; here each roll gets the same sequence of its own,
 * and the page waits for the cover's colours before its first frame.
 *
 * Lyrics: the player keeps its springs, but auto seek detection is off (it
 * judges by the wall clock, which a slow render keeps outrunning), and the
 * instance is kept so a segment can jump to its start.
 */

import { IsolationRenderer, LyricPlayer, MeshGradientRenderer } from "@applemusic-like-lyrics/core";
import { random, randomFrom, song } from "./clock";
import { KeyedError } from "../shared/errors";

/** The parts of AMLL's renderers the frame loop below touches; private in the typings. */
interface RendererInternals {
	setAlbum(source: unknown, isVideo?: boolean): Promise<void>;
	tickHandle: number;
	paused: boolean;
	_disposed: boolean;
	contextLost: boolean;
	flowSpeed: number;
	onTickBinded: (time: number) => void;
	onRedraw(time: number, delta: number): unknown;
	requestTick(): void;
	setLowFreqVolume(volume: number): void;
}

/** The parts of AMLL's lyric player the frame loop below touches; private in the typings. */
interface LyricInternals {
	currentLyricGroups: ({ enable(time: number, shouldPlay: boolean): void } | undefined)[];
	timelineController: { getSnapshot(): { highlightedGroups: Set<number> } };
}

/**
 * Pin the word highlight of every line being sung to `time`.  While playing,
 * AMLL leaves those animations to the browser clock from the moment a line
 * starts, which lands a frame either way from run to run; re-enabling the line
 * paused at the song time puts every word exactly where the lyric says.
 */
export function pinWords(time: number): void {
	const lyrics = live.lyrics as unknown as LyricInternals | null;
	if (!lyrics) return;
	for (const index of lyrics.timelineController.getSnapshot().highlightedGroups) {
		lyrics.currentLyricGroups[index]?.enable(time, false);
	}
}

export const live: {
	lyrics: LyricPlayer | null;
	background: RendererInternals | null;
	// the cover the background is loading, settled once its colours are taken
	album: Promise<void> | null;
	// low band at a song position, when the background should pulse with it
	level: ((time: number) => number) | null;
} = { lyrics: null, background: null, album: null, level: null };

/** Run `roll` with Math.random restarted at the page's seed, whenever it is called. */
function seeded<A extends unknown[], R>(roll: (...args: A) => R): (...args: A) => R {
	return (...args) => {
		const shared = Math.random;
		Math.random = randomFrom(random.seed);
		try {
			return roll(...args);
		} finally {
			Math.random = shared;
		}
	};
}

/** Make the renderer's random layout and its cover loading reproducible; see the top. */
function settleAlbums(renderer: RendererInternals, rolls: string[]): void {
	const methods = renderer as unknown as Record<string, (...args: unknown[]) => unknown>;
	for (const name of rolls) methods[name] = seeded(methods[name].bind(renderer));
	const setAlbum = renderer.setAlbum.bind(renderer);
	renderer.setAlbum = (source, isVideo) => {
		const loading = setAlbum(source, isVideo);
		live.album = loading;
		return loading;
	};
}

/** Wait until the background has its cover's colours; call while frames are being pumped. */
export async function albumReady(): Promise<void> {
	// the background asks for the cover from an effect, a frame or so after it mounts
	for (let i = 0; i < 30 && live.background && !live.album; i++) {
		await new Promise((resolve) => requestAnimationFrame(resolve));
	}
	await live.album;
}

/** Throw if the background's WebGL context is gone: its frames would come out blank. */
export function checkBackground(): void {
	if (live.background?.contextLost) {
		throw new KeyedError("webgl_lost");
	}
}

/** Swap the renderer's frame callback for one that draws the song position. */
function driveBySong(renderer: RendererInternals): void {
	let last: number | null = null;
	renderer.onTickBinded = () => {
		renderer.tickHandle = 0;
		if (renderer.paused || renderer._disposed || renderer.contextLost) return;
		const time = song.time;
		const delta = last === null ? 0 : Math.max(0, time - last);
		last = time;
		if (live.level) renderer.setLowFreqVolume(live.level(time));
		renderer.onRedraw(time * renderer.flowSpeed, delta);
		renderer.requestTick();
	};
	live.background = renderer;
}

/** Finish the cover fade-in and the palette transition, so frame 0 shows the final colours. */
export function settleBackground(): void {
	for (let i = 0; i < 10; i++) live.background?.onRedraw(song.time * (live.background.flowSpeed ?? 1), 1000);
}

export class TimedMeshRenderer extends MeshGradientRenderer {
	constructor(canvas: HTMLCanvasElement) {
		super(canvas);
		settleAlbums(this as unknown as RendererInternals, ["createMeshState"]);
		driveBySong(this as unknown as RendererInternals);
	}
}

export class TimedIsolationRenderer extends IsolationRenderer {
	constructor(canvas: HTMLCanvasElement) {
		super(canvas);
		settleAlbums(this as unknown as RendererInternals, ["rollRandomParameters"]);
		driveBySong(this as unknown as RendererInternals);
	}
}

export class TimedLyricPlayer extends LyricPlayer {
	constructor() {
		super();
		this.setEnableAutoSeekDetection(false);
		live.lyrics = this;
	}
}

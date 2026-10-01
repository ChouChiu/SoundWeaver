/**
 * The contract between the pages (run in the browser) and the CLIs that drive
 * them: a page puts these objects on `window`, the CLI calls them through
 * `page.evaluate`.
 */

/** What every page offers the frame-by-frame driver. */
export interface RenderPage {
	/** Where to start replaying so the state at `ms` matches a run from the beginning. */
	prerollStart(ms: number): number;
	/** Show the page at `ms`; returns 0 when the frame is known to be empty. */
	seek(ms: number): number;
	/** When the content ends. */
	endTime(): number;
}

export interface StageOptions {
	fontFamily: string;
	fontSize: number;
	fadeMs: number;
	// translation line opacity, 0..1
	subOpacity: number;
}

export interface AmllRender {
	/** Build one player per lyric line; returns how many lines are shown. */
	load(ttml: string, options: StageOptions): number;
	/** Park every player's springs at its line once fonts and layout are ready. */
	settle(): void;
}

export type ViewPreset = "iso" | "top" | "angle";

export interface SoundfieldOptions {
	view: ViewPreset;
	objectScale: number;
	headScale: number;
	// objects quieter than this (dBFS) fade out
	gateDb: number;
	fadeMs: number;
	// opacity of an object whose sound is outside the video's track, 0..1
	dim: number;
}

/** One key: start s, ramp s, x, y, z (TS 103 420 room coordinates), active 0/1. */
export type ObjectKey = [number, number, number, number, number, number];

/** The trace the Python side writes, see dolbytool.core.atmos.trace. */
export interface SoundfieldScene {
	sampleRate: number;
	levelStart: number;
	levelStep: number;
	duration: number;
	// trackLevels: the level counting only the video's track, when it is not the whole mix
	objects: { id: number; keys: ObjectKey[]; levels: number[]; trackLevels?: number[] }[];
}

export interface SoundfieldRender {
	/** Build the room, the head (GLB bytes as base64, or the built-in silhouette) and the objects. */
	load(scene: SoundfieldScene, options: SoundfieldOptions, glb: string | null): Promise<number>;
	/** The WebGL renderer string, to tell the GPU from SwiftShader in debug output. */
	gpu(): string;
}

declare global {
	interface Window {
		renderPage: RenderPage;
		amllRender: AmllRender;
		soundfield: SoundfieldRender;
	}
}

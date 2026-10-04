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

export type BackgroundKind = "mesh" | "isolation" | "solid" | "none";
export type ControlsKind = "controls" | "fft" | "none";
export type LyricSize = "tiny" | "extra-small" | "small" | "medium" | "large" | "extra-large" | "huge";

/**
 * The full player page: AMLL's PrebuiltLyricPlayer (react-full), the AMLL
 * Player layout with artwork, song info, progress, controls and lyrics.
 * Landscape frames get the horizontal layout, portrait ones the vertical.
 */
export interface PlayerOptions {
	background: BackgroundKind;
	// mesh / isolation flow speed, 1 is AMLL's default
	flowSpeed: number;
	// background canvas resolution relative to the frame
	renderScale: number;
	// solid background, any CSS colour
	color: string;
	// seeds every random choice of the page, so parallel segments draw the same
	seed: number;
	// isolation only: brightness waves
	lightWave: boolean;
	fontFamily: string;
	fontWeight: number;
	// em
	letterSpacing: number;
	lyricSize: LyricSize;
	// width of the soft edge of the word highlight, in multiples of the line height
	wordFadeWidth: number;
	blur: boolean;
	scale: boolean;
	spring: boolean;
	translation: boolean;
	romanization: boolean;
	swapTransRoman: boolean;
	backgroundVocals: boolean;
	showName: boolean;
	showArtists: boolean;
	showAlbum: boolean;
	controls: ControlsKind;
	showVolume: boolean;
	// volume slider position, 0..1
	volume: number;
	// lyrics / AirPlay / playlist toggles under the controls
	showBottomControls: boolean;
	// time left instead of the total under the progress bar
	showRemaining: boolean;
	// quality badge under the progress bar, empty for none
	qualityTag: string;
	dolbyAtmos: boolean;
	title: string;
	artists: string[];
	album: string;
	// song length for the progress bar, ms
	durationMs: number;
	// even out the lengths of a wrapped line's rows instead of filling the first
	balance: boolean;
	// landscape: empty space right of the lyrics, percent of the column; negative keeps AMLL's 15 % / 8 %
	lyricPadding: number;
}

/**
 * Per-frame audio analysis, one entry every `step` ms: the low band 0..1 for
 * the mesh background's pulse, and spectrum bands for the FFT controls.
 */
export interface AudioFrames {
	step: number;
	low: number[] | null;
	fft: number[][] | null;
}

export interface PlayerRender {
	/** Build the page; `cover` is a data: URL or null.  Returns how many lyric lines are shown. */
	load(ttml: string, options: PlayerOptions, cover: string | null, audio: AudioFrames | null): Promise<number>;
	/** Park the lyric springs and finish the background fade-in once fonts and layout are ready. */
	settle(): void;
	/** The WebGL renderer string, or an empty string without a WebGL background. */
	gpu(): string;
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
		player: PlayerRender;
		soundfield: SoundfieldRender;
	}
}

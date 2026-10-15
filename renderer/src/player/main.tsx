/**
 * Browser entry: AMLL's PrebuiltLyricPlayer, fed from a jotai store the CLI
 * writes to through window.player / window.renderPage.
 *
 * Each frame sets the song position with flushSync, so React has rendered and
 * the lyric player has the new time before the CLI asks for the frame.  The
 * lyric player's own requestAnimationFrame loop then advances its springs by
 * the frame interval.  A jump (the first frame of a segment, or a replay) is
 * a seek, after which the springs are run to rest.
 *
 * The page plays a queue of songs back to back, an album say: at each song's
 * start the song info, progress and lyrics change over as in the player,
 * while the background and the motion artwork run on through the whole queue.
 */

import "./clock";
import "@applemusic-like-lyrics/core/style.css";
import "@applemusic-like-lyrics/react-full/style.css";
import "./player.css";

import type { LyricLine } from "@applemusic-like-lyrics/core";
import {
	AudioQualityType,
	CSS_BACKGROUND_RENDERER_ID,
	cssBackgroundPropertyAtom,
	enableLyricLineBlurEffectAtom,
	enableLyricLineScaleEffectAtom,
	enableLyricLineSpringAnimationAtom,
	enableLyricRomanLineAtom,
	enableLyricSwapTransRomanLineAtom,
	enableLyricTranslationLineAtom,
	fftDataAtom,
	hideLyricViewAtom,
	isLyricPageOpenedAtom,
	isolationRendererOptionsAtom,
	LyricSizePreset,
	lyricBackgroundFPSAtom,
	lyricBackgroundRendererAtom,
	lyricBackgroundRenderScaleAtom,
	lyricBackgroundStaticModeAtom,
	lyricFontFamilyAtom,
	lyricFontWeightAtom,
	lyricLetterSpacingAtom,
	lyricPlayerImplementationAtom,
	lyricSizePresetAtom,
	lyricWordFadeWidthAtom,
	musicAlbumNameAtom,
	musicArtistsAtom,
	musicCoverAtom,
	musicDurationAtom,
	musicLyricLinesAtom,
	musicNameAtom,
	musicPlayingAtom,
	musicPlayingPositionAtom,
	musicQualityAtom,
	musicQualityTagAtom,
	musicVolumeAtom,
	PlayerControlsType,
	PrebuiltLyricPlayer,
	playerControlsTypeAtom,
	showBottomControlAtom,
	showMusicAlbumAtom,
	showMusicArtistsAtom,
	showMusicNameAtom,
	showRemainingTimeAtom,
	showVolumeControlAtom,
} from "@applemusic-like-lyrics/react-full";
import { parseTTML } from "@applemusic-like-lyrics/ttml";
import { createStore, Provider } from "jotai";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import type { AudioFrames, PlayerOptions, PlayerTrack } from "../shared/api";
import { withoutCredits } from "../shared/credits";
import { seedRandom, song } from "./clock";
import {
	albumReady,
	checkBackground,
	live,
	pinWords,
	settleBackground,
	TimedIsolationRenderer,
	TimedLyricPlayer,
	TimedMeshRenderer,
} from "./timed";
import { motionReady, seekMotion, setMotion } from "./motion";
import { KeyedError } from "../shared/errors";

// A segment replays this much before its first frame, so the scroll springs and
// the spectrum are where a run from the start would have them.
const PREROLL_MS = 6000;

const store = createStore();
const tick = document.getElementById("tick")!;
let lastTime: number | null = null;
let end = 0;
let frames: AudioFrames | null = null;

/** A song of the queue, ready to show; times in ms on the queue's clock. */
interface Song {
	lines: LyricLine[];
	name: string;
	artists: string[];
	album: string;
	durationMs: number;
	start: number;
}

let songs: Song[] = [];
let current = -1;

/** The first non-empty value of a TTML metadata key such as musicName or artists. */
function metadata(entries: [string, string[]][], key: string): string[] {
	return entries.find(([k]) => k === key)?.[1]?.filter((v) => v.trim()) ?? [];
}

function at<T>(values: T[], time: number): T | undefined {
	const i = Math.min(values.length - 1, Math.max(0, Math.round(time / frames!.step)));
	return values[i];
}

// Longest side the background canvas is drawn at.  Chrome hands WebGL a smaller
// buffer than a very large canvas asks for, and AMLL would then draw only its
// corner; the background is a soft blur, so stretching a 4K one costs nothing.
const BACKGROUND_MAX_SIDE = 4096;

/** The renderer's canvas scale, lowered when the frame would make that canvas too large. */
function backgroundScale(scale: number): number {
	const side = Math.max(window.innerWidth, window.innerHeight) * window.devicePixelRatio * scale;
	return side > BACKGROUND_MAX_SIDE ? (scale * BACKGROUND_MAX_SIDE) / side : scale;
}

/** The song of the queue playing at `time`. */
function songAt(time: number): number {
	let i = songs.length - 1;
	while (i > 0 && time < songs[i].start) i--;
	return i;
}

/** Put song `index` on the player: its info, length and lyrics. */
function show(index: number): void {
	const next = songs[index];
	const set = store.set;
	flushSync(() => {
		set(musicNameAtom, next.name);
		set(musicArtistsAtom, next.artists.map((name, i) => ({ name, id: String(i) })));
		set(musicAlbumNameAtom, next.album);
		set(musicDurationAtom, next.durationMs);
		set(musicPlayingPositionAtom, 0);
		// an instrumental: AMLL's lyric-less layout, the cover on its own
		set(hideLyricViewAtom, next.lines.length === 0);
		set(musicLyricLinesAtom, next.lines);
	});
	current = index;
}

/** Every atom the prebuilt player reads that stays for the whole queue; storage-backed ones included. */
function configure(options: PlayerOptions, cover: string | null): void {
	const set = store.set;
	const background =
		options.background === "mesh"
			? TimedMeshRenderer
			: options.background === "isolation"
				? TimedIsolationRenderer
				: CSS_BACKGROUND_RENDERER_ID;
	set(lyricBackgroundRendererAtom, { renderer: background } as never);
	set(cssBackgroundPropertyAtom, options.background === "none" ? "transparent" : options.color);
	set(isolationRendererOptionsAtom, (prev) => ({ ...prev, lightWave: options.lightWave }));
	set(lyricBackgroundRenderScaleAtom, backgroundScale(options.renderScale));
	// the renderer draws on every frame anyway; no frame skipping of its own
	set(lyricBackgroundFPSAtom, 1000);
	set(lyricBackgroundStaticModeAtom, false);
	set(lyricPlayerImplementationAtom, { lyricPlayer: TimedLyricPlayer });

	// the page's own sans-serif stack stays last, for glyphs the chosen fonts lack
	set(lyricFontFamilyAtom, options.fontFamily ? `${options.fontFamily}, sans-serif` : "");
	set(lyricFontWeightAtom, options.fontWeight);
	set(lyricLetterSpacingAtom, options.letterSpacing ? `${options.letterSpacing}em` : "normal");
	set(lyricSizePresetAtom, options.lyricSize as LyricSizePreset);
	set(lyricWordFadeWidthAtom, options.wordFadeWidth);
	set(enableLyricLineBlurEffectAtom, options.blur);
	set(enableLyricLineScaleEffectAtom, options.scale);
	set(enableLyricLineSpringAnimationAtom, options.spring);
	set(enableLyricTranslationLineAtom, options.translation);
	set(enableLyricRomanLineAtom, options.romanization);
	set(enableLyricSwapTransRomanLineAtom, options.swapTransRoman);

	set(showMusicNameAtom, options.showName);
	set(showMusicArtistsAtom, options.showArtists);
	set(showMusicAlbumAtom, options.showAlbum);
	set(playerControlsTypeAtom, options.controls as PlayerControlsType);
	set(showVolumeControlAtom, options.showVolume);
	set(musicVolumeAtom, options.volume);
	set(showBottomControlAtom, options.showBottomControls);
	set(showRemainingTimeAtom, options.showRemaining);
	set(isLyricPageOpenedAtom, true);
	set(musicCoverAtom, cover ?? "");
	set(musicPlayingAtom, true);
	if (options.qualityTag) {
		set(musicQualityAtom, (prev) => ({
			...prev,
			type: options.dolbyAtmos ? AudioQualityType.DolbyAtmos : AudioQualityType.Lossless,
		}));
		set(musicQualityTagAtom, { tagIcon: true, tagText: options.qualityTag, isDolbyAtmos: options.dolbyAtmos });
	} else {
		set(musicQualityTagAtom, null);
	}
}

/** The line an instrumental shows in place of lyrics, lit from its start to its end. */
function instrumentalLine(text: string, durationMs: number): LyricLine {
	const end = Math.max(durationMs, 1000);
	return {
		// AMLL ends a line with its last word: a trailing blank keeps it lit to the end
		words: [
			{ word: text, startTime: 0, endTime: 1000 },
			{ word: " ", startTime: 1000, endTime: end },
		],
		translatedLyric: "",
		romanLyric: "",
		startTime: 0,
		endTime: end,
		isBG: false,
		isDuet: false,
	} as LyricLine;
}

/** The queue's songs, laid end to end. */
function queue(tracks: PlayerTrack[], options: PlayerOptions): Song[] {
	let start = 0;
	return tracks.map((track, index) => {
		const parsed = track.ttml === null ? { lines: [], metadata: [] } : parseTTML(track.ttml);
		const meta = parsed.metadata as [string, string[]][];
		let lines = (parsed.lines as LyricLine[]).filter(
			(line) => (options.backgroundVocals || !line.isBG) && line.words.some((w) => w.word.trim()),
		);
		if (track.ttml !== null && !lines.length) throw new KeyedError("no_lyric_lines_track", { track: index + 1 });
		const name = track.title || metadata(meta, "musicName")[0] || "";
		const artists = track.artists.length ? track.artists : metadata(meta, "artists");
		if (options.filterCredits) lines = withoutCredits(lines, [name, ...metadata(meta, "musicName"), ...artists]);
		if (track.ttml === null && options.instrumentalText.trim()) {
			lines = [instrumentalLine(options.instrumentalText.trim(), track.durationMs)];
		}
		const lastLine = Math.max(0, ...lines.map((l) => l.endTime));
		const durationMs = track.durationMs || lastLine + 3000;
		const next: Song = {
			lines,
			name,
			artists,
			album: track.album || metadata(meta, "album")[0] || "",
			durationMs,
			start,
		};
		start += durationMs;
		return next;
	});
}

/** Seek the lyrics to `time` and let the springs settle there. */
function jump(time: number): void {
	const lyrics = live.lyrics;
	if (!lyrics) return;
	lyrics.setCurrentTime(time, true);
	for (let i = 0; i < 60; i++) lyrics.update(100);
}

window.player = {
	async load(tracks, options, cover, motion, audio) {
		seedRandom(options.seed);
		frames = audio;
		live.level = audio?.low ? (time) => at(audio.low!, time) ?? 0 : null;
		if (options.background === "none") document.body.classList.add("transparent");
		document.body.classList.toggle("balance", options.balance);
		if (options.lyricPadding >= 0) document.body.style.setProperty("--player-lyric-padding", `${options.lyricPadding}%`);
		songs = queue(tracks, options);
		configure(options, cover);
		setMotion(cover ? motion : null);
		const root = createRoot(document.getElementById("root")!);
		flushSync(() =>
			root.render(
				<Provider store={store}>
					<PrebuiltLyricPlayer style={{ width: "100%", height: "100%" }} />
				</Provider>,
			),
		);
		show(0);
		// a render error unmounts everything and is only reported, never thrown here
		if (!document.getElementById("root")!.childElementCount) {
			throw new KeyedError("player_blank");
		}
		// the cover has to decode before the background can take its colours
		if (cover) {
			const image = new Image();
			image.src = cover;
			await image.decode();
		}
		await albumReady();
		const last = songs[songs.length - 1];
		const lastLine = Math.max(0, ...last.lines.map((l) => l.endTime));
		end = last.start + Math.max(last.durationMs, lastLine + 3000);
		return songs.reduce((sum, next) => sum + next.lines.length, 0);
	},
	settle() {
		settleBackground();
		jump(0);
		lastTime = 0;
	},
	gpu() {
		if (!live.background) return "";
		const gl = document.createElement("canvas").getContext("webgl");
		const info = gl?.getExtension("WEBGL_debug_renderer_info");
		return (info && gl?.getParameter(info.UNMASKED_RENDERER_WEBGL)) || "WebGL";
	},
};

window.renderPage = {
	prerollStart: (time) => Math.max(0, time - PREROLL_MS),
	endTime: () => end,
	seek(time) {
		checkBackground();
		// Always dirty some style, so every BeginFrame runs a full main frame.  Kept on
		// its own element: set on the root, the inherited property would restyle everything.
		tick.style.setProperty("--frame-time", String(time));
		// the background, the artwork and the audio run on the queue's clock, the rest on the song's
		song.time = time;
		seekMotion(time);
		const index = songAt(time);
		const changed = index !== current;
		if (changed) show(index);
		const local = time - songs[index].start;
		const interval = lastTime === null ? 0 : time - lastTime;
		flushSync(() => {
			store.set(musicPlayingPositionAtom, Math.min(local, songs[index].durationMs));
			if (frames?.fft) store.set(fftDataAtom, at(frames.fft, time) ?? []);
		});
		// anything but one step forward is a jump: the first frame of a segment, a replay,
		// or the next song, whose lyrics start parked at its beginning
		if (changed || lastTime === null || interval <= 0 || interval > 250) jump(local);
		pinWords(local);
		lastTime = time;
		// never empty: the background and the controls are on every frame
		return 1;
	},
	ready: motionReady,
};

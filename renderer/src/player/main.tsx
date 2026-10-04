/**
 * Browser entry: AMLL's PrebuiltLyricPlayer, fed from a jotai store the CLI
 * writes to through window.player / window.renderPage.
 *
 * Each frame sets the song position with flushSync, so React has rendered and
 * the lyric player has the new time before the CLI asks for the frame.  The
 * lyric player's own requestAnimationFrame loop then advances its springs by
 * the frame interval.  A jump (the first frame of a segment, or a replay) is
 * a seek, after which the springs are run to rest.
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
import type { AudioFrames, PlayerOptions } from "../shared/api";
import { seedRandom, song } from "./clock";
import { live, pinWords, settleBackground, TimedIsolationRenderer, TimedLyricPlayer, TimedMeshRenderer } from "./timed";

// A segment replays this much before its first frame, so the scroll springs and
// the spectrum are where a run from the start would have them.
const PREROLL_MS = 6000;

const store = createStore();
const tick = document.getElementById("tick")!;
let lastTime: number | null = null;
let end = 0;
let frames: AudioFrames | null = null;

/** The first non-empty value of a TTML metadata key such as musicName or artists. */
function metadata(entries: [string, string[]][], key: string): string[] {
	return entries.find(([k]) => k === key)?.[1]?.filter((v) => v.trim()) ?? [];
}

function at<T>(values: T[], time: number): T | undefined {
	const i = Math.min(values.length - 1, Math.max(0, Math.round(time / frames!.step)));
	return values[i];
}

/** Every atom the prebuilt player reads, from the options; storage-backed ones included. */
function configure(options: PlayerOptions, cover: string | null, lines: LyricLine[], meta: [string, string[]][]): void {
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
	set(lyricBackgroundRenderScaleAtom, options.renderScale);
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
	set(hideLyricViewAtom, false);
	set(isLyricPageOpenedAtom, true);

	const artists = options.artists.length ? options.artists : metadata(meta, "artists");
	set(musicNameAtom, options.title || metadata(meta, "musicName")[0] || "");
	set(musicArtistsAtom, artists.map((name, i) => ({ name, id: String(i) })));
	set(musicAlbumNameAtom, options.album || metadata(meta, "album")[0] || "");
	set(musicCoverAtom, cover ?? "");
	set(musicDurationAtom, options.durationMs);
	set(musicPlayingAtom, true);
	set(musicPlayingPositionAtom, 0);
	if (options.qualityTag) {
		set(musicQualityAtom, (prev) => ({
			...prev,
			type: options.dolbyAtmos ? AudioQualityType.DolbyAtmos : AudioQualityType.Lossless,
		}));
		set(musicQualityTagAtom, { tagIcon: true, tagText: options.qualityTag, isDolbyAtmos: options.dolbyAtmos });
	} else {
		set(musicQualityTagAtom, null);
	}
	set(musicLyricLinesAtom, lines);
}

/** Seek the lyrics to `time` and let the springs settle there. */
function jump(time: number): void {
	const lyrics = live.lyrics;
	if (!lyrics) return;
	lyrics.setCurrentTime(time, true);
	for (let i = 0; i < 60; i++) lyrics.update(100);
}

window.player = {
	async load(ttml, options, cover, audio) {
		seedRandom(options.seed);
		frames = audio;
		live.level = audio?.low ? (time) => at(audio.low!, time) ?? 0 : null;
		if (options.background === "none") document.body.classList.add("transparent");
		document.body.classList.toggle("balance", options.balance);
		if (options.lyricPadding >= 0) document.body.style.setProperty("--player-lyric-padding", `${options.lyricPadding}%`);
		const parsed = parseTTML(ttml);
		const lines = (parsed.lines as LyricLine[]).filter(
			(line) => (options.backgroundVocals || !line.isBG) && line.words.some((w) => w.word.trim()),
		);
		configure(options, cover, lines, parsed.metadata as [string, string[]][]);
		const root = createRoot(document.getElementById("root")!);
		flushSync(() =>
			root.render(
				<Provider store={store}>
					<PrebuiltLyricPlayer style={{ width: "100%", height: "100%" }} />
				</Provider>,
			),
		);
		// the cover has to decode before the background can take its colours
		if (cover) {
			const image = new Image();
			image.src = cover;
			await image.decode();
		}
		const lastLine = Math.max(0, ...lines.map((l) => l.endTime));
		end = Math.max(options.durationMs, lastLine + 3000);
		return lines.length;
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
		// Always dirty some style, so every BeginFrame runs a full main frame.  Kept on
		// its own element: set on the root, the inherited property would restyle everything.
		tick.style.setProperty("--frame-time", String(time));
		song.time = time;
		const interval = lastTime === null ? 0 : time - lastTime;
		flushSync(() => {
			store.set(musicPlayingPositionAtom, Math.min(time, store.get(musicDurationAtom) || time));
			if (frames?.fft) store.set(fftDataAtom, at(frames.fft, time) ?? []);
		});
		// anything but one step forward is a jump: the first frame of a segment, or a replay
		if (lastTime === null || interval <= 0 || interval > 250) jump(time);
		pinWords(time);
		lastTime = time;
		// never empty: the background and the controls are on every frame
		return 1;
	},
};

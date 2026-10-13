/**
 * Offline player-page renderer: TTML, cover and song info in, a video of the
 * whole lyrics view out, after the Apple Music / am-hook full-screen player.
 * Without a TTML (an instrumental) the cover stands alone for --duration.
 *
 * --playlist plays several songs back to back as the player's queue would, an
 * album say: a JSON array of {ttml, title, artists, album, duration}, ttml a
 * path or null, duration in seconds; --audio is then the songs' audio joined.
 *
 * Same frame-by-frame machinery as the lyric renderer.  The browser runs
 * ANGLE on Vulkan like the sound field renderer, whatever the background.
 * The frame is opaque unless the background is "none", so it is encoded to
 * H.264 / HEVC by default, in whatever container --out names; the audio can be muxed in at the end.
 */

import { mkdtemp, readFile, rename } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import type { AudioFrames, BackgroundKind, ControlsKind, LyricSize, MotionFrames, PlayerOptions, PlayerTrack } from "../shared/api";
import { analyse } from "./analysis";
import { headlessShell } from "./chrome";
import { type AudioMode, CODECS, type Codec, muxAudio } from "./ffmpeg";
import { debug, status } from "./log";
import { defaultJobs, number } from "./options";
import { renderTimeline, start } from "./parallel";
import { Stage, type StageSettings } from "./stage";

const BACKGROUNDS: BackgroundKind[] = ["mesh", "isolation", "solid", "none"];
const CONTROLS: ControlsKind[] = ["controls", "fft", "none"];
const SIZES: LyricSize[] = ["tiny", "extra-small", "small", "medium", "large", "extra-large", "huge"];
const AUDIO_MODES: AudioMode[] = ["none", "encode", "copy"];
const WEBGL_ARGS = [
	"--use-angle=vulkan",
	"--enable-features=Vulkan",
	"--enable-gpu",
	"--ignore-gpu-blocklist",
	"--enable-unsafe-swiftshader",
];
const MIME: Record<string, string> = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".webp": "image/webp",
	".bmp": "image/bmp",
};

function choice<T extends string>(name: string, value: string | undefined, allowed: readonly T[]): T {
	if (!allowed.includes(value as T)) {
		console.error(`invalid --${name}: ${value}`);
		process.exit(2);
	}
	return value as T;
}

function parseOptions(argv = process.argv.slice(2)) {
	const { values } = parseArgs({
		args: argv,
		options: {
			ttml: { type: "string" },
			playlist: { type: "string" },
			out: { type: "string" },
			cover: { type: "string" },
			// motion artwork: a folder of numbered stills and a manifest.json {fps, count}
			"cover-frames": { type: "string" },
			audio: { type: "string" },
			"audio-mode": { type: "string", default: "none" },
			beat: { type: "boolean", default: false },
			width: { type: "string", default: "1920" },
			height: { type: "string", default: "1080" },
			fps: { type: "string", default: "60" },
			background: { type: "string", default: "mesh" },
			"flow-speed": { type: "string", default: "1" },
			"render-scale": { type: "string", default: "0.5" },
			color: { type: "string", default: "#111111" },
			seed: { type: "string", default: "1" },
			"light-wave": { type: "boolean", default: false },
			font: { type: "string", default: "" },
			"font-weight": { type: "string", default: "0" },
			"letter-spacing": { type: "string", default: "0" },
			"lyric-size": { type: "string", default: "medium" },
			"word-fade": { type: "string", default: "0.5" },
			"no-blur": { type: "boolean", default: false },
			"no-scale": { type: "boolean", default: false },
			"no-spring": { type: "boolean", default: false },
			"no-translation": { type: "boolean", default: false },
			romanization: { type: "boolean", default: false },
			"swap-trans-roman": { type: "boolean", default: false },
			"no-bg-vocals": { type: "boolean", default: false },
			"no-name": { type: "boolean", default: false },
			"no-artists": { type: "boolean", default: false },
			album: { type: "string", default: "" },
			"show-album": { type: "boolean", default: false },
			controls: { type: "string", default: "controls" },
			volume: { type: "string" },
			"bottom-controls": { type: "boolean", default: false },
			remaining: { type: "boolean", default: false },
			quality: { type: "string", default: "" },
			atmos: { type: "boolean", default: false },
			title: { type: "string", default: "" },
			// repeat for several artists
			artist: { type: "string", multiple: true, default: [] },
			"song-length": { type: "string" },
			duration: { type: "string" },
			// seconds into the song to start at, for a preview
			start: { type: "string", default: "0" },
			// device pixel ratio of the page; 0 picks one from the frame size
			"ui-scale": { type: "string", default: "0" },
			"no-balance": { type: "boolean", default: false },
			// JPEG capture: about 1.5x faster, slightly softer detail
			"fast-capture": { type: "boolean", default: false },
			// draw the page at twice the size and scale it down: smoother text edges, about 2-3x slower
			supersample: { type: "boolean", default: false },
			// keep the title and credit lines around the lyrics
			"keep-credits": { type: "boolean", default: false },
			// shown in place of lyrics for a song without any
			"instrumental-text": { type: "string", default: "" },
			// percent of the lyric column left empty on its right; -1 keeps AMLL's
			"lyric-padding": { type: "string", default: "-1" },
			codec: { type: "string", default: "libx264" },
			crf: { type: "string", default: "18" },
			jobs: { type: "string" },
		},
	});
	if (!values.out) {
		console.error("usage: player.mjs [--ttml <file>] --out <file.mkv|mp4|webm> [options]");
		process.exit(2);
	}
	const background = choice("background", values.background, BACKGROUNDS);
	const codec = choice("codec", values.codec, CODECS);
	if (background === "none" && codec !== "vp9") {
		console.error("--background none needs --codec vp9 (the only one with alpha)");
		process.exit(2);
	}
	if (!values.ttml && !values.playlist && values.duration === undefined) {
		console.error("without --ttml or --playlist the video's length has to be given by --duration");
		process.exit(2);
	}
	const controls = choice("controls", values.controls, CONTROLS);
	const duration = values.duration === undefined ? undefined : number("duration", values.duration, 0.001);
	// the progress bar runs over the song; the render may stop earlier
	const songLength = values["song-length"] === undefined ? duration : number("song-length", values["song-length"], 0.001);
	const options: PlayerOptions = {
		background,
		flowSpeed: number("flow-speed", values["flow-speed"], 0),
		// the background is a blur either way: supersampling leaves its pixel count alone
		renderScale: number("render-scale", values["render-scale"], 0.05, 2) / (values.supersample ? 2 : 1),
		color: values.color!,
		seed: Math.floor(number("seed", values.seed, 0)),
		lightWave: values["light-wave"]!,
		fontFamily: values.font!,
		fontWeight: number("font-weight", values["font-weight"], 0, 1000),
		letterSpacing: number("letter-spacing", values["letter-spacing"], -1, 1),
		lyricSize: choice("lyric-size", values["lyric-size"], SIZES),
		wordFadeWidth: number("word-fade", values["word-fade"], 0.0001, 10),
		blur: !values["no-blur"],
		scale: !values["no-scale"],
		spring: !values["no-spring"],
		translation: !values["no-translation"],
		romanization: values.romanization!,
		swapTransRoman: values["swap-trans-roman"]!,
		backgroundVocals: !values["no-bg-vocals"],
		showName: !values["no-name"],
		showArtists: !values["no-artists"],
		showAlbum: values["show-album"]!,
		controls,
		showVolume: values.volume !== undefined,
		volume: values.volume === undefined ? 1 : number("volume", values.volume, 0, 100) / 100,
		showBottomControls: values["bottom-controls"]!,
		showRemaining: values.remaining!,
		qualityTag: values.quality!,
		dolbyAtmos: values.atmos!,
		balance: !values["no-balance"],
		lyricPadding: number("lyric-padding", values["lyric-padding"], -1, 50),
		instrumentalText: values["instrumental-text"]!,
		filterCredits: !values["keep-credits"],
	};
	const audioMode = choice("audio-mode", values["audio-mode"], AUDIO_MODES);
	if ((audioMode !== "none" || values.beat || controls === "fft") && !values.audio) {
		console.error("--audio-mode, --beat and --controls fft need --audio");
		process.exit(2);
	}
	const single: PlaylistEntry = {
		ttml: values.ttml ?? null,
		title: values.title!,
		artists: values.artist as string[],
		album: values.album!,
		duration: songLength ?? 0,
	};
	return {
		single,
		playlist: values.playlist ? path.resolve(values.playlist) : null,
		out: path.resolve(values.out),
		cover: values.cover ? path.resolve(values.cover) : null,
		coverFrames: values["cover-frames"] ? path.resolve(values["cover-frames"]) : null,
		audio: values.audio ? path.resolve(values.audio) : null,
		audioMode,
		beat: values.beat!,
		width: number("width", values.width, 2),
		height: number("height", values.height, 2),
		fps: number("fps", values.fps, 1),
		options,
		duration,
		codec: codec as Codec,
		start: number("start", values.start, 0),
		uiScale: number("ui-scale", values["ui-scale"], 0, 10),
		crf: number("crf", values.crf, 0, 63),
		fastCapture: values["fast-capture"]!,
		supersample: values.supersample ? 2 : 1,
		jobs: values.jobs === undefined ? defaultJobs() : Math.floor(number("jobs", values.jobs, 1)),
	};
}

/** A song as --playlist lists it; the single-song flags make one too. */
interface PlaylistEntry {
	ttml: string | null;
	title: string;
	artists: string[];
	album: string;
	// seconds, 0 for until the lyrics end
	duration: number;
}

async function tracks(entries: PlaylistEntry[]): Promise<PlayerTrack[]> {
	if (!entries.length) throw new Error("歌单为空");
	return Promise.all(
		entries.map(async (entry) => ({
			ttml: entry.ttml ? await readFile(path.resolve(entry.ttml), "utf8") : null,
			title: entry.title,
			artists: entry.artists,
			album: entry.album,
			durationMs: entry.duration * 1000,
		})),
	);
}

async function motionFrames(folder: string): Promise<MotionFrames> {
	const manifest = JSON.parse(await readFile(path.join(folder, "manifest.json"), "utf8"));
	return { base: pathToFileURL(folder).href, fps: Number(manifest.fps), count: Number(manifest.count) };
}

async function dataUrl(file: string): Promise<string> {
	const mime = MIME[path.extname(file).toLowerCase()] ?? "image/png";
	return `data:${mime};base64,${(await readFile(file)).toString("base64")}`;
}

/**
 * The page's device pixel ratio.  AMLL Player's layouts are sized for a desktop
 * window (landscape) and a phone (portrait) in CSS pixels, so the page is laid
 * out at that size and drawn at the frame size: 1920×1080 is a 1080 px tall
 * window at 1×, 1080×1920 a 360×640 phone at 3×.
 */
/**
 * Video memory a segment takes, in MiB, measured on an RTX 3070 Ti at 1080p: a browser
 * with the mesh background about 250 (450 supersampled), an NVENC encoder about 270.
 * Rounded up; both grow with the pixels each draws.
 */
function vramPerJob(width: number, height: number, supersample: number, codec: Codec): number {
	const frames = (width * height) / (1920 * 1080);
	const browser = 150 + 150 * frames * supersample ** 2;
	const encoder = codec.endsWith("_nvenc") ? 300 * frames : 0;
	return Math.ceil(browser + encoder);
}

function uiScale(width: number, height: number, manual: number): number {
	if (manual > 0) return manual;
	if (height <= width) return height / 1080;
	// a phone about 400 CSS px wide; prefer a width whose height comes out whole
	let best = 400;
	for (let css = 360; css <= 440; css++) {
		if (Number.isInteger((height * css) / width) && Math.abs(css - 400) < Math.abs(best - 400)) best = css;
	}
	return width / best;
}

start(async (run) => {
	const opts = parseOptions();
	const entries: PlaylistEntry[] = opts.playlist
		? JSON.parse(await readFile(opts.playlist, "utf8"))
		: [opts.single];
	const queue = await tracks(entries);
	const cover = opts.cover ? await dataUrl(opts.cover) : null;
	// the stills go by file URL: as data URLs they would be copied into every browser whole
	const motion = opts.coverFrames ? await motionFrames(opts.coverFrames) : null;
	const want = { low: opts.beat && opts.options.background === "mesh", fft: opts.options.controls === "fft" };
	let audio: AudioFrames | null = null;
	if (opts.audio && (want.low || want.fft)) {
		status("分析音频");
		audio = await analyse(run, opts.audio, 1000 / opts.fps, want);
		debug(`audio analysed: ${(audio.low ?? audio.fft)?.length} frames`);
	}
	const settings: StageSettings = {
		executablePath: await headlessShell(),
		html: "player.html",
		scale: uiScale(opts.width, opts.height, opts.uiScale) * opts.supersample,
		width: opts.width * opts.supersample,
		height: opts.height * opts.supersample,
		fps: opts.fps,
		// the GPU composites every background: in software the blurred lyric lines
		// alone make a frame about 1.7x slower
		args: WEBGL_ARGS,
		setup: async (stage) => {
			const count = await stage.pump(
				stage.page.evaluate(
					(t, o, c, m, a) => window.player.load(t, o, c, m, a),
					queue, opts.options, cover, motion, audio,
				),
			);
			await stage.pump(stage.page.evaluate(() => document.fonts.ready.then(() => true)));
			// let ResizeObserver report the final sizes, then park the springs and the background
			for (let i = 0; i < 5; i++) await stage.frame(false);
			await stage.page.evaluate(() => window.player.settle());
			for (let i = 0; i < 5; i++) await stage.frame(false);
			const gpu = await stage.page.evaluate(() => window.player.gpu());
			debug(`player loaded, ${count} lines${gpu ? `, WebGL on ${gpu}` : ""}`);
		},
	};

	status("启动浏览器");
	const first = await Stage.open(run, settings);
	const startFrame = Math.round(opts.start * opts.fps);
	const seconds = opts.duration ?? Math.max(0.1, (await first.endTime()) / 1000 - opts.start);
	const scaled = settings.scale !== 1 || opts.supersample !== 1;
	debug(`ui scale ${settings.scale}`);
	const encoding = {
		fps: opts.fps,
		crf: opts.crf,
		codec: opts.codec,
		jobs: opts.jobs,
		vramPerJob: vramPerJob(opts.width, opts.height, opts.supersample, opts.codec),
		seconds,
		startFrame,
		size: scaled ? ([opts.width, opts.height] as [number, number]) : undefined,
		fast: opts.fastCapture,
	};
	if (opts.audioMode === "none" || !opts.audio) {
		await renderTimeline(run, first, () => Stage.open(run, settings), { ...encoding, out: opts.out });
		return;
	}
	// picture first, beside the output so the final rename stays on one file system
	const tmp = await mkdtemp(path.join(path.dirname(opts.out), ".render-video-"));
	run.tempDirs.add(tmp);
	const picture = path.join(tmp, `picture${path.extname(opts.out)}`);
	await renderTimeline(run, first, () => Stage.open(run, settings), { ...encoding, out: picture });
	status("混入音频");
	const muxed = path.join(tmp, `muxed${path.extname(opts.out)}`);
	await muxAudio(run, picture, opts.audio, opts.audioMode, muxed, startFrame / opts.fps);
	await rename(muxed, opts.out);
	debug("audio muxed");
});

/**
 * Per-frame audio analysis for the player page: the low band that makes the
 * mesh background pulse, and the spectrum the FFT controls draw.  The song is
 * decoded once to mono float PCM; every value depends only on the samples
 * around its frame, so parallel segments get the same numbers.
 */

import { spawn } from "node:child_process";
import type { AudioFrames } from "../shared/api";
import type { Run } from "./run";

// 2000 Hz is the top of AMLL's default spectrum range, so 8 kHz covers it
const RATE = 8000;
const FFT_SIZE = 1024;
// AMLL's default range for the visualiser and the background pulse
const FFT_LOW = 80;
const FFT_HIGH = 2000;
const BANDS = 64;
// 50–120 Hz, the band AMLL asks for the background pulse
const BEAT_LOW = 50;
const BEAT_HIGH = 120;

async function decode(run: Run, audio: string): Promise<Float32Array> {
	const child = spawn(
		"ffmpeg",
		["-hide_banner", "-nostdin", "-loglevel", "error", "-i", audio, "-vn", "-ac", "1", "-ar", String(RATE), "-f", "f32le", "pipe:1"],
		{ stdio: ["ignore", "pipe", "pipe"] },
	);
	run.children.add(child);
	const chunks: Buffer[] = [];
	let stderr = "";
	child.stdout.on("data", (d: Buffer) => chunks.push(d));
	child.stderr.on("data", (d) => {
		stderr += d;
	});
	await new Promise<void>((resolve, reject) =>
		child.on("close", (code) => {
			run.children.delete(child);
			if (code === 0) resolve();
			else reject(new Error(stderr.trim() || `ffmpeg exited with ${code}`));
		}),
	);
	const raw = Buffer.concat(chunks);
	// copy: the concatenated buffer may not be 4-byte aligned
	return new Float32Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength));
}

/** In-place radix-2 FFT of real input; returns magnitudes of the first n/2 bins. */
function magnitudes(input: Float32Array): Float32Array {
	const n = input.length;
	const re = Float64Array.from(input);
	const im = new Float64Array(n);
	for (let i = 1, j = 0; i < n; i++) {
		let bit = n >> 1;
		for (; j & bit; bit >>= 1) j ^= bit;
		j ^= bit;
		if (i < j) {
			[re[i], re[j]] = [re[j], re[i]];
			[im[i], im[j]] = [im[j], im[i]];
		}
	}
	for (let len = 2; len <= n; len <<= 1) {
		const angle = (-2 * Math.PI) / len;
		const wr = Math.cos(angle);
		const wi = Math.sin(angle);
		for (let i = 0; i < n; i += len) {
			let cr = 1;
			let ci = 0;
			for (let k = 0; k < len / 2; k++) {
				const ar = re[i + k + len / 2] * cr - im[i + k + len / 2] * ci;
				const ai = re[i + k + len / 2] * ci + im[i + k + len / 2] * cr;
				re[i + k + len / 2] = re[i + k] - ar;
				im[i + k + len / 2] = im[i + k] - ai;
				re[i + k] += ar;
				im[i + k] += ai;
				[cr, ci] = [cr * wr - ci * wi, cr * wi + ci * wr];
			}
		}
	}
	const out = new Float32Array(n / 2);
	for (let i = 0; i < n / 2; i++) out[i] = Math.hypot(re[i], im[i]) / n;
	return out;
}

/** Log-spaced band edges between FFT_LOW and FFT_HIGH, as bin indices. */
function bandEdges(): number[] {
	const hz = (b: number) => FFT_LOW * (FFT_HIGH / FFT_LOW) ** (b / BANDS);
	return Array.from({ length: BANDS + 1 }, (_, b) => Math.max(1, Math.round((hz(b) * FFT_SIZE) / RATE)));
}

export async function analyse(run: Run, audio: string, step: number, want: { low: boolean; fft: boolean }): Promise<AudioFrames> {
	const samples = await decode(run, audio);
	const frames = Math.ceil(samples.length / ((RATE * step) / 1000));
	const window = Float32Array.from({ length: FFT_SIZE }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
	const edges = bandEdges();
	const beatFrom = Math.round((BEAT_LOW * FFT_SIZE) / RATE);
	const beatTo = Math.max(beatFrom + 1, Math.round((BEAT_HIGH * FFT_SIZE) / RATE));
	const lowDb: number[] = [];
	const fft: number[][] = [];
	const block = new Float32Array(FFT_SIZE);
	for (let f = 0; f < frames; f++) {
		// the window ends at the frame, like an analyser reading what has just played
		const end = Math.round((f * step * RATE) / 1000);
		for (let i = 0; i < FFT_SIZE; i++) {
			const s = end - FFT_SIZE + i;
			block[i] = (s >= 0 && s < samples.length ? samples[s] : 0) * window[i];
		}
		const mag = magnitudes(block);
		if (want.low) {
			let sum = 0;
			for (let i = beatFrom; i < beatTo; i++) sum += mag[i] * mag[i];
			lowDb.push(10 * Math.log10(sum / (beatTo - beatFrom) + 1e-12));
		}
		if (want.fft) {
			// dB mapped like an AnalyserNode's byte data: -100 dB → 0, -30 dB → 255
			fft.push(
				Array.from({ length: BANDS }, (_, b) => {
					let peak = 0;
					for (let i = edges[b]; i <= Math.max(edges[b], edges[b + 1] - 1); i++) peak = Math.max(peak, mag[i]);
					const db = 20 * Math.log10(peak + 1e-12);
					return Math.round(Math.min(255, Math.max(0, ((db + 100) / 70) * 255)));
				}),
			);
		}
	}
	return { step, low: want.low ? smoothLow(lowDb, step) : null, fft: want.fft ? fft : null };
}

/**
 * 0..1 from 30 dB below the loudest window to the top, then a fast attack and
 * a slow release so the background swells on hits instead of flickering.
 */
function smoothLow(db: number[], step: number): number[] {
	const peak = Math.max(-120, ...db);
	const attack = 1 - Math.exp(-step / 30);
	const release = 1 - Math.exp(-step / 250);
	let level = 0;
	return db.map((d) => {
		const target = Math.min(1, Math.max(0, (d - (peak - 30)) / 30)) ** 2;
		level += (target - level) * (target > level ? attack : release);
		return Number(level.toFixed(4));
	});
}

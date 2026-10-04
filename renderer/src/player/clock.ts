/**
 * The page's sense of time, made reproducible.
 *
 * performance.now() is wall-clock time, so anything animating by it (framer
 * motion, parts of AMLL) would land on different frames in every run.  Here it
 * returns the timestamp of the current BeginFrame instead, which advances by
 * exactly one frame interval per frame.  Math.random gets a fixed seed, so the
 * background picks the same layout in every browser of a parallel render.
 */

const realNow = performance.now.bind(performance);
let frameNow = realNow();

// registered before anything else, so it runs first in every frame
const onFrame = (time: number) => {
	frameNow = time;
	requestAnimationFrame(onFrame);
};
requestAnimationFrame(onFrame);
performance.now = () => frameNow;

/** Song position of the frame being drawn, ms; set by seek() before the frame. */
export const song = { time: 0 };

/** mulberry32: small, fast and the same sequence everywhere for one seed. */
export function seedRandom(seed: number): void {
	let state = seed >>> 0;
	Math.random = () => {
		state = (state + 0x6d2b79f5) >>> 0;
		let t = state;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

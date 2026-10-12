/**
 * Motion artwork, put on the song's clock.
 *
 * The browser's own video playback cannot be driven frame by frame here: under
 * begin-frame control a media seek never completes.  So the CLI hands over the
 * artwork as numbered stills, and each frame the cover shows the still for the
 * song position, looping.  The still is fetched and decoded before the frame is
 * drawn, so every run and every parallel segment shows the same picture.
 */

import type { MotionFrames } from "../shared/api";

let motion: MotionFrames | null = null;
let shown = -1;
let pending: Promise<void> | null = null;

export function setMotion(frames: MotionFrames | null): void {
	motion = frames && frames.count > 0 ? frames : null;
	shown = -1;
}

function stillUrl(index: number): string {
	return `${motion!.base}/${String(index + 1).padStart(5, "0")}.jpg`;
}

/** Start loading the still for song position `ms`; `motionReady` resolves once it shows. */
export function seekMotion(ms: number): void {
	if (!motion) return;
	const index = Math.floor((ms / 1000) * motion.fps) % motion.count;
	if (index === shown) return;
	shown = index;
	const url = stillUrl(index);
	const image = new Image();
	image.src = url;
	pending = image
		.decode()
		.catch(() => {})
		.then(() => {
			// AMLL draws an image cover as the background of these (one per layout)
			for (const element of document.querySelectorAll<HTMLElement>('[class*="coverInner"]')) {
				if (element.style.backgroundImage) element.style.backgroundImage = `url("${url}")`;
			}
		});
}

export async function motionReady(): Promise<boolean> {
	if (pending) {
		await pending;
		pending = null;
	}
	return true;
}

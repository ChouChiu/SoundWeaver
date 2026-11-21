/**
 * Shared spatial visualization constants and helpers for Soundfield and Live Monitor.
 */

import * as THREE from "three";

export const SPATIAL_TINT = 0x5c7a99;

export const FLOOR_DB = -60;
export const TOP_DB = -10;

export const RAINBOW = [
	0xff4d4d, 0xff884d, 0xffc24d, 0xe6ff4d, 0x88ff4d,
	0x4dff79, 0x4dffcc, 0x4dd2ff, 0x4d88ff, 0x5c4dff,
	0xa64dff, 0xf04dff, 0xff4da6, 0xff6b8b, 0xffd166,
];

export type LayerBand = "upper" | "middle" | "lower";

/** Normalize dBFS onto 0..1 across `[floorDb, topDb]`. */
export function dbToNorm(db: number, floorDb = FLOOR_DB, topDb = TOP_DB): number {
	return Math.max(0, Math.min(1, (db - floorDb) / (topDb - floorDb)));
}

export function makeTextTexture(text: string, style: "outlined" | "compact" = "outlined"): THREE.CanvasTexture {
	const c = document.createElement("canvas");
	c.width = 96;
	c.height = 64;
	const ctx = c.getContext("2d")!;
	ctx.textAlign = "center";
	if (style === "compact") {
		ctx.font = "32px system-ui, sans-serif";
		ctx.fillStyle = "#e0e2e6";
		ctx.textBaseline = "alphabetic";
		const { actualBoundingBoxAscent, actualBoundingBoxDescent } = ctx.measureText(text);
		const baseline = (c.height + actualBoundingBoxAscent - actualBoundingBoxDescent) / 2;
		ctx.fillText(text, c.width / 2, baseline);
	} else {
		ctx.font = "bold 28px sans-serif";
		ctx.textBaseline = "middle";
		ctx.lineWidth = 5;
		ctx.strokeStyle = "rgba(0,0,0,0.88)";
		ctx.strokeText(text, c.width / 2, c.height / 2);
		ctx.fillStyle = "#ffffff";
		ctx.fillText(text, c.width / 2, c.height / 2);
	}
	const tex = new THREE.CanvasTexture(c);
	tex.colorSpace = THREE.SRGBColorSpace;
	return tex;
}

/** Crisp outlined sprite for 3D labels (object IDs, speaker names). */
export function makeTextSprite(text: string, height: number): THREE.Sprite {
	const tex = makeTextTexture(text);
	const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
		map: tex, transparent: true, depthTest: false,
	}));
	sprite.scale.set(height * tex.image.width / tex.image.height, height, 1);
	return sprite;
}

/**
 * Where each object is and how visible it is at any time, as pure functions of
 * the trace: every frame can be computed on its own, so segments need no preroll.
 */

import type { ObjectKey } from "../shared/api";

export type Vec3 = [number, number, number];

export interface Path {
	starts: number[];
	ramps: number[];
	// where each key's glide begins: the position its predecessor had reached by then
	from: Vec3[];
	to: Vec3[];
	active: boolean[];
}

/** Index of the last element <= t in ascending `values`, -1 if none. */
function lastAtOrBefore(values: number[], t: number): number {
	let lo = 0;
	let hi = values.length - 1;
	let found = -1;
	while (lo <= hi) {
		const mid = (lo + hi) >> 1;
		if (values[mid] <= t) {
			found = mid;
			lo = mid + 1;
		} else {
			hi = mid - 1;
		}
	}
	return found;
}

function glide(path: Path, i: number, t: number): Vec3 {
	const ramp = path.ramps[i];
	const f = ramp > 0 ? Math.min(1, Math.max(0, (t - path.starts[i]) / ramp)) : 1;
	const [a, b] = [path.from[i], path.to[i]];
	return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

export function buildPath(keys: ObjectKey[]): Path {
	const path: Path = { starts: [], ramps: [], from: [], to: [], active: [] };
	for (const [start, ramp, x, y, z, active] of keys) {
		const i = path.starts.length;
		const to: Vec3 = [x, y, z];
		path.from.push(i === 0 ? to : glide(path, i - 1, start));
		path.starts.push(start);
		path.ramps.push(ramp);
		path.to.push(to);
		path.active.push(active === 1);
	}
	return path;
}

/** TS 103 420 room position at `t` seconds. */
export function positionAt(path: Path, t: number): Vec3 {
	if (!path.starts.length) return [0.5, 0.5, 0];
	return glide(path, Math.max(0, lastAtOrBefore(path.starts, t)), t);
}

export function activeAt(path: Path, t: number): boolean {
	const i = lastAtOrBefore(path.starts, t);
	return i >= 0 ? path.active[i] : path.active.length > 0 && path.active[0];
}

export interface Visibility {
	start: number;
	step: number;
	// 0..1 at each level step
	values: Float32Array;
}

/**
 * Walk the levels in order once: shown while louder than the gate and active,
 * each change faded in or out at a constant rate.
 */
export function buildVisibility(
	path: Path,
	levels: number[],
	start: number,
	step: number,
	gateDb: number,
	fadeSeconds: number,
): Visibility {
	const values = new Float32Array(levels.length);
	const rate = fadeSeconds > 0 ? step / fadeSeconds : 1;
	let v = -1;
	for (let i = 0; i < levels.length; i++) {
		const target = levels[i] >= gateDb && activeAt(path, start + i * step) ? 1 : 0;
		v = v < 0 ? target : v < target ? Math.min(target, v + rate) : Math.max(target, v - rate);
		values[i] = v;
	}
	return { start, step, values };
}

export function visibilityAt(vis: Visibility, t: number): number {
	const n = vis.values.length;
	if (!n) return 0;
	// values[i] is reached at the end of its step
	const f = (t - vis.start) / vis.step - 1;
	if (f <= 0) return vis.values[0];
	if (f >= n - 1) return vis.values[n - 1];
	const i = Math.floor(f);
	return vis.values[i] + (vis.values[i + 1] - vis.values[i]) * (f - i);
}

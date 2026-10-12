/**
 * Lines with a width.  WebGL's own lines are one pixel at any frame size, too
 * thin to see in an 8K or 16K video; these are drawn as quads, `width` pixels
 * at 1080p and scaled with the frame.
 */

import * as THREE from "three";
import { LineMaterial, type LineMaterialParameters } from "three/examples/jsm/lines/LineMaterial.js";
import { LineSegments2 } from "three/examples/jsm/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/examples/jsm/lines/LineSegmentsGeometry.js";

const materials: LineMaterial[] = [];
let pixelScale = 1;

/** Line widths follow the frame's shorter side, as a 1080p frame has them. */
export function setFrameSize(width: number, height: number): void {
	pixelScale = Math.min(width, height) / 1080;
}

/** The size of the canvas WebGL draws into (a tile when the frame is tiled); widths are in its pixels. */
export function setLineResolution(width: number, height: number): void {
	for (const m of materials) m.resolution.set(width, height);
}

export function lineMaterial(params: LineMaterialParameters & { linewidth: number }): LineMaterial {
	const material = new LineMaterial({ ...params, linewidth: params.linewidth * pixelScale, worldUnits: false });
	materials.push(material);
	return material;
}

/** Pairs of points, [x0, y0, z0, x1, y1, z1, ...], as separate segments. */
export function segments(points: number[], material: LineMaterial): LineSegments2 {
	const geometry = new LineSegmentsGeometry();
	geometry.setPositions(points);
	const lines = new LineSegments2(geometry, material);
	lines.computeLineDistances();
	return lines;
}

/** Every edge of `geometry` (a box), as the pairs `segments` wants. */
export function edgePoints(geometry: THREE.BufferGeometry): number[] {
	return Array.from(new THREE.EdgesGeometry(geometry).getAttribute("position").array);
}

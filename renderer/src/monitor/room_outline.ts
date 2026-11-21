import * as THREE from "three";
import { LineMaterial } from "three/examples/jsm/lines/LineMaterial.js";
import { LineSegments2 } from "three/examples/jsm/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/examples/jsm/lines/LineSegmentsGeometry.js";

export function makeRoomOutline(
	boxEdges: THREE.EdgesGeometry,
	gridPoints: number[],
	half: { x: number; y: number; z: number },
) {
	const points = Float32Array.from(boxEdges.getAttribute("position").array);
	const masks: number[] = [];
	for (let offset = 0; offset < points.length; offset += 6) {
		let adjacentFaces = 0;
		for (let axis = 0; axis < 3; axis++) {
			const a = points[offset + axis];
			if (a === points[offset + 3 + axis]) adjacentFaces |= 1 << (2 * axis + Number(a > 0));
		}
		masks.push(adjacentFaces);
	}

	const geometry = new LineSegmentsGeometry().setPositions(points.slice());
	const positions = geometry.getAttribute("instanceStart") as THREE.InterleavedBufferAttribute;
	positions.data.setUsage(THREE.DynamicDrawUsage);
	const edges = new LineSegments2(geometry, new LineMaterial({
		color: 0x687989, linewidth: 1.25, transparent: true, opacity: 0.75,
		alphaToCoverage: true, depthWrite: false,
	}));
	edges.renderOrder = 1;
	const grid = new LineSegments2(new LineSegmentsGeometry().setPositions(gridPoints), new LineMaterial({
		color: 0x586b80, linewidth: 0.9, transparent: true, opacity: 0.65,
		alphaToCoverage: true, depthWrite: false,
	}));
	const extents = [half.x, half.y, half.z];
	const axes = ["x", "y", "z"] as const;
	let previousMask = -1;

	// A seam belongs in the outline when either adjacent inward face can be seen.
	function update(eye: THREE.Vector3, showGrid: boolean): void {
		let visibleFaces = 0;
		for (const [axis, name] of axes.entries()) {
			if (eye[name] > -extents[axis]) visibleFaces |= 1 << (2 * axis);
			if (eye[name] < extents[axis]) visibleFaces |= 1 << (2 * axis + 1);
		}
		grid.visible = showGrid && eye.y > -half.y;
		if (visibleFaces === previousMask) return;
		previousMask = visibleFaces;

		let count = 0;
		for (const [edge, mask] of masks.entries()) {
			if ((mask & visibleFaces) === 0) continue;
			positions.data.array.set(points.subarray(edge * 6, edge * 6 + 6), count * 6);
			count++;
		}
		geometry.instanceCount = count;
		positions.data.needsUpdate = true;
	}

	return { edges, grid, update };
}

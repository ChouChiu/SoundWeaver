/**
 * The room, the listener and the camera, after the Atmos object viewer this
 * video follows: a translucent box with a floor grid and a front arrow, the
 * listener standing on the floor facing -Z, objects as spheres.
 *
 * Room coordinates (TS 103 420): x 0..1 left to right, y 0..1 front to back,
 * z 0 at the listener's plane and 1 at the ceiling.  They map onto the scene as
 * X = (2x - 1), Z = (2y - 1), Y = z, each scaled to the room, so the front is -Z
 * and a floor-level object sits at half height as in the ADM convention.
 */

import * as THREE from "three";
import type { ViewPreset } from "../shared/api";
import type { Vec3 } from "./timeline";

// full size 5.8 x 3.1 x 5.8
export const ROOM_HALF = { x: 2.9, y: 1.55, z: 2.9 };
// room coordinates +-1 fill this much of the room; the walls are drawn slightly larger
const POS_FILL = 0.975;
const ROOM_VISUAL_SCALE = 1.04;
export const TINT = 0x5c7a99;
const OBJECT_RADIUS = 0.075;

export function toScene([x, y, z]: Vec3): Vec3 {
	return [
		(2 * x - 1) * ROOM_HALF.x * POS_FILL,
		z * ROOM_HALF.y * POS_FILL,
		(2 * y - 1) * ROOM_HALF.z * POS_FILL,
	];
}

function floorGrid(halfX: number, halfZ: number, divX: number, divZ: number): THREE.LineSegments {
	const pts: number[] = [];
	for (let i = 0; i <= divX; i++) {
		const x = -halfX + (2 * halfX * i) / divX;
		pts.push(x, 0, -halfZ, x, 0, halfZ);
	}
	for (let j = 0; j <= divZ; j++) {
		const z = -halfZ + (2 * halfZ * j) / divZ;
		pts.push(-halfX, 0, z, halfX, 0, z);
	}
	const geo = new THREE.BufferGeometry();
	geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
	return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x5a88b8, transparent: true, opacity: 0.5 }));
}

/** Arrow on the floor pointing to the front, the absolute reference whatever the head looks like. */
function frontMarker(): THREE.Mesh {
	const shape = new THREE.Shape();
	shape.moveTo(0, 0.28);
	shape.lineTo(-0.11, 0.02);
	shape.lineTo(-0.04, 0.02);
	shape.lineTo(-0.04, -0.16);
	shape.lineTo(0.04, -0.16);
	shape.lineTo(0.04, 0.02);
	shape.lineTo(0.11, 0.02);
	shape.closePath();
	const mat = new THREE.MeshStandardMaterial({
		color: 0x6ab0ff,
		emissive: 0x3a70c0,
		emissiveIntensity: 0.7,
		roughness: 0.4,
		metalness: 0.1,
		transparent: true,
		opacity: 0.9,
		side: THREE.DoubleSide,
	});
	const marker = new THREE.Mesh(new THREE.ShapeGeometry(shape), mat);
	marker.rotation.x = -Math.PI / 2;
	return marker;
}

function room(): THREE.Group {
	const group = new THREE.Group();
	const half = ROOM_HALF;
	const box = new THREE.BoxGeometry(
		half.x * 2 * ROOM_VISUAL_SCALE,
		half.y * 2 * ROOM_VISUAL_SCALE,
		half.z * 2 * ROOM_VISUAL_SCALE,
	);
	// unlit, so every wall keeps the same brightness in perspective
	group.add(
		new THREE.Mesh(
			box,
			new THREE.MeshBasicMaterial({
				color: 0x2a4058,
				transparent: true,
				opacity: 0.55,
				side: THREE.BackSide,
				depthWrite: false,
			}),
		),
	);
	const edges = new THREE.LineSegments(
		new THREE.EdgesGeometry(box),
		new THREE.LineBasicMaterial({ color: 0x7eb4d8, transparent: true, opacity: 0.9, depthTest: false }),
	);
	edges.renderOrder = 3;
	group.add(edges);
	const grid = floorGrid(half.x, half.z, 6, 4);
	grid.position.y = -half.y + 0.002;
	group.add(grid);
	const arrow = frontMarker();
	arrow.position.set(0, -half.y + 0.003, -half.z * 0.36);
	group.add(arrow);
	return group;
}

function lights(scene: THREE.Scene): void {
	scene.add(new THREE.AmbientLight(0xd0dce8, 0.8));
	scene.add(new THREE.HemisphereLight(0xe8f2ff, 0x283848, 0.7));
	for (const [color, intensity, x, y, z] of [
		[0xffffff, 1.3, 2.2, 3.8, 3.5],
		[0xb0c8e0, 0.55, -2.8, 2.0, -2.2],
		[0xc8dcff, 0.45, 0, 5, -1],
	]) {
		const light = new THREE.DirectionalLight(color, intensity);
		light.position.set(x, y, z);
		scene.add(light);
	}
}

/**
 * Stand `head` on the floor at the centre, scaled by `scaleFor` its largest
 * dimension.  Its own orientation is kept: turn it to face -Z first.
 */
export function placeHead(head: THREE.Object3D, scaleFor: (size: number) => number): THREE.Group {
	head.updateMatrixWorld(true);
	const box = new THREE.Box3().setFromObject(head);
	const size = box.getSize(new THREE.Vector3());
	const centre = box.getCenter(new THREE.Vector3());
	head.position.sub(new THREE.Vector3(centre.x, box.min.y, centre.z));
	const pivot = new THREE.Group();
	pivot.add(head);
	pivot.scale.setScalar(scaleFor(Math.max(size.x, size.y, size.z) || 1));
	pivot.position.set(0, -ROOM_HALF.y, 0.05);
	return pivot;
}

export function tint(root: THREE.Object3D): void {
	root.traverse((o) => {
		const mesh = o as THREE.Mesh;
		if (!mesh.isMesh) return;
		for (const m of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
			const std = m as THREE.MeshStandardMaterial;
			if (std.color) std.color.setHex(TINT);
			m.needsUpdate = true;
		}
	});
}

/** Placeholder listener: head, nose towards the front, neck and shoulders. */
export function silhouette(): THREE.Group {
	const group = new THREE.Group();
	const mat = new THREE.MeshStandardMaterial({ color: TINT, roughness: 0.55, metalness: 0.05 });
	const head = new THREE.Mesh(new THREE.SphereGeometry(0.19, 24, 24), mat);
	head.position.y = 0.46;
	group.add(head);
	const noseMat = mat.clone();
	noseMat.emissive = new THREE.Color(0x2a4058);
	noseMat.emissiveIntensity = 0.25;
	const nose = new THREE.Mesh(new THREE.ConeGeometry(0.045, 0.1, 12), noseMat);
	nose.rotation.x = -Math.PI / 2;
	nose.position.set(0, 0.44, -0.185);
	group.add(nose);
	const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.085, 0.12, 16), mat);
	neck.position.y = 0.28;
	group.add(neck);
	const torso = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.42, 0.4, 24), mat);
	torso.position.y = 0.02;
	group.add(torso);
	return group;
}

export function objectMesh(): THREE.Mesh {
	const mesh = new THREE.Mesh(
		new THREE.SphereGeometry(OBJECT_RADIUS, 28, 28),
		new THREE.MeshStandardMaterial({ color: TINT, roughness: 0.4, metalness: 0.1, transparent: true }),
	);
	mesh.renderOrder = 5;
	return mesh;
}

// camera distance and aim, as in the viewer's default zoom
const DISTANCE = 3.6 * 2.8;
const TARGET = new THREE.Vector3(0, -0.15, 0);
const deg = (d: number) => (d * Math.PI) / 180;
// theta: azimuth around Y from +Z, phi: angle down from straight above
const VIEWS: Record<ViewPreset, { theta: number; phi: number; fov: number; up: Vec3 }> = {
	// standard isometric: from (1, 1, 1)
	iso: { theta: Math.PI / 4, phi: Math.atan(Math.SQRT2), fov: 42, up: [0, 1, 0] },
	// nearly overhead, front at the top of the picture
	top: { theta: 0, phi: deg(90 - 72), fov: 48, up: [0, 0, -1] },
	// low from the far side
	angle: { theta: deg(301), phi: deg(90 - 17.5), fov: 42, up: [0, 1, 0] },
};

export function camera(view: ViewPreset, aspect: number): THREE.PerspectiveCamera {
	const { theta, phi, fov, up } = VIEWS[view];
	const cam = new THREE.PerspectiveCamera(fov, aspect, 0.1, 200);
	cam.position.set(
		TARGET.x + DISTANCE * Math.sin(phi) * Math.sin(theta),
		TARGET.y + DISTANCE * Math.cos(phi),
		TARGET.z + DISTANCE * Math.sin(phi) * Math.cos(theta),
	);
	cam.up.set(...up);
	cam.lookAt(TARGET);
	cam.updateProjectionMatrix();
	return cam;
}

export function world(): THREE.Scene {
	const scene = new THREE.Scene();
	lights(scene);
	scene.add(room());
	return scene;
}

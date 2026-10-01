/**
 * Browser entry of the sound field page: a WebGL canvas the size of the window
 * on a transparent background, drawn synchronously on every seek.
 */

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import type { SoundfieldOptions, SoundfieldScene } from "../shared/api";
import { camera, objectMesh, placeHead, ROOM_HALF, silhouette, tint, toScene, world } from "./scene";
import * as timeline from "./timeline";

// a loaded head is fitted to this share of the room height, then scaled by the option
const HEAD_HEIGHT = 0.15;
// the placeholder silhouette is modelled in metres and only scaled
const SILHOUETTE_SCALE = 0.7;

interface Tracked {
	mesh: THREE.Mesh;
	path: timeline.Path;
	vis: timeline.Visibility;
	// in the video's track, 0..1; absent when the video shows the whole mix
	inTrack: timeline.Visibility | null;
}

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x000000, 0);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.35;
document.body.appendChild(renderer.domElement);

const scene = world();
let cam = camera("iso", window.innerWidth / window.innerHeight);
let tracked: Tracked[] = [];
let objectScale = 1;
let dim = 1;
let duration = 0;

function parseGlb(base64: string): Promise<THREE.Object3D> {
	const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
	return new Promise((resolve, reject) =>
		new GLTFLoader().parse(bytes.buffer, "", (gltf) => resolve(gltf.scene), (e) => reject(new Error(`头模无法解析：${e.message}`))),
	);
}

async function load(data: SoundfieldScene, options: SoundfieldOptions, glb: string | null): Promise<number> {
	cam = camera(options.view, window.innerWidth / window.innerHeight);
	objectScale = options.objectScale;
	dim = options.dim;
	duration = data.duration;

	let head: THREE.Object3D;
	let scaleFor: (size: number) => number;
	if (glb) {
		head = await parseGlb(glb);
		// imported heads usually face +Z, the front here is -Z
		head.rotation.y = Math.PI;
		scaleFor = (size) => ((ROOM_HALF.y * 2 * HEAD_HEIGHT) / size) * options.headScale;
	} else {
		head = silhouette();
		scaleFor = () => SILHOUETTE_SCALE * options.headScale;
	}
	tint(head);
	scene.add(placeHead(head, scaleFor));

	tracked = data.objects.map((o) => {
		const path = timeline.buildPath(o.keys);
		const mesh = objectMesh();
		scene.add(mesh);
		const visibility = (levels: number[]) =>
			timeline.buildVisibility(path, levels, data.levelStart, data.levelStep, options.gateDb, options.fadeMs / 1000);
		return {
			mesh,
			path,
			vis: visibility(o.levels),
			inTrack: o.trackLevels ? visibility(o.trackLevels) : null,
		};
	});
	return tracked.length;
}

function seek(ms: number): number {
	const t = ms / 1000;
	for (const { mesh, path, vis, inTrack } of tracked) {
		mesh.position.set(...toScene(timeline.positionAt(path, t)));
		const v = timeline.visibilityAt(vis, t);
		// outside the track the object stays where it is, only dimmed
		const focus = inTrack ? dim + (1 - dim) * timeline.visibilityAt(inTrack, t) : 1;
		mesh.scale.setScalar(objectScale * (0.4 + 0.6 * v));
		mesh.visible = v > 0.02;
		const mat = mesh.material as THREE.MeshStandardMaterial;
		mat.opacity = v * focus;
		mat.depthWrite = mat.opacity >= 0.95;
	}
	renderer.render(scene, cam);
	// the room is always in the picture
	return 1;
}

function gpu(): string {
	const gl = renderer.getContext();
	const info = gl.getExtension("WEBGL_debug_renderer_info");
	return String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
}

window.soundfield = { load, gpu };
window.renderPage = {
	prerollStart: (ms) => ms,
	seek,
	endTime: () => duration * 1000,
};

/**
 * Browser entry of the sound field page: the room drawn on a transparent
 * background the size of the window, synchronously on every seek.
 */

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import type { SoundfieldOptions, SoundfieldScene } from "../shared/api";
import { setFrameSize, setLineResolution } from "./lines";
import { ObjectView } from "./objects";
import { camera, placeHead, ROOM_HALF, silhouette, tint, world } from "./scene";
import * as timeline from "./timeline";
import { KeyedError } from "../shared/errors";

// a loaded head is fitted to this share of the room height, then scaled by the option
const HEAD_HEIGHT = 0.15;
// the placeholder silhouette is modelled in metres and only scaled
const SILHOUETTE_SCALE = 0.7;

// Frames up to 8K UHD draw straight into one canvas.  Beyond that Chrome may
// hand WebGL a smaller drawing buffer than the canvas (it halves multisampled
// buffers past about 10K wide), and three.js would draw only its top-left part;
// so large frames are drawn in tiles of at most TILE pixels, each a slice of the
// same camera view, and put together on a 2D canvas the size of the frame.
const DIRECT_MAX = { width: 7680, height: 4320 };
const TILE = 4096;

const frame = { width: window.innerWidth, height: window.innerHeight };
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setClearColor(0x000000, 0);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.35;

interface Tiling {
	cols: number;
	rows: number;
	width: number;
	height: number;
	context: CanvasRenderingContext2D;
}

function setUpSurface(): Tiling | null {
	if (frame.width <= DIRECT_MAX.width && frame.height <= DIRECT_MAX.height) {
		renderer.setSize(frame.width, frame.height);
		const gl = renderer.getContext();
		if (gl.drawingBufferWidth === frame.width && gl.drawingBufferHeight === frame.height) {
			document.body.appendChild(renderer.domElement);
			return null;
		}
	}
	const cols = Math.ceil(frame.width / TILE);
	const rows = Math.ceil(frame.height / TILE);
	const width = Math.ceil(frame.width / cols);
	const height = Math.ceil(frame.height / rows);
	renderer.setSize(width, height);
	const canvas = document.createElement("canvas");
	canvas.width = frame.width;
	canvas.height = frame.height;
	document.body.appendChild(canvas);
	return { cols, rows, width, height, context: canvas.getContext("2d")! };
}

const tiling = setUpSurface();
setFrameSize(frame.width, frame.height);

const scene = world();
let cam = camera("iso", window.innerWidth / window.innerHeight);
let objects: ObjectView[] = [];
let duration = 0;

function parseGlb(base64: string): Promise<THREE.Object3D> {
	const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
	return new Promise((resolve, reject) =>
		new GLTFLoader().parse(bytes.buffer, "", (gltf) => resolve(gltf.scene), (e) => reject(new KeyedError("head_model", { message: e.message }))),
	);
}

async function load(data: SoundfieldScene, options: SoundfieldOptions, glb: string | null): Promise<number> {
	cam = camera(options.view, window.innerWidth / window.innerHeight, options.zoom);
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

	objects = data.objects.map((o, index) => {
		const path = timeline.buildPath(o.keys);
		const visibility = (levels: number[]) =>
			timeline.buildVisibility(path, levels, data.levelStart, data.levelStep, options.gateDb, options.fadeMs / 1000);
		return new ObjectView(
			scene,
			{
				index,
				path,
				vis: visibility(o.levels),
				inTrack: o.trackLevels ? visibility(o.trackLevels) : null,
				levels: o.levels,
				levelStart: data.levelStart,
				levelStep: data.levelStep,
			},
			options,
		);
	});
	// line widths are in pixels of the canvas WebGL draws into: the tile when tiled
	setLineResolution(tiling?.width ?? frame.width, tiling?.height ?? frame.height);
	return objects.length;
}

function seek(ms: number): number {
	const t = ms / 1000;
	for (const object of objects) object.update(t);
	if (!tiling) {
		renderer.render(scene, cam);
	} else {
		const { cols, rows, width, height, context } = tiling;
		for (let row = 0; row < rows; row++) {
			for (let col = 0; col < cols; col++) {
				const x = col * width;
				const y = row * height;
				cam.setViewOffset(frame.width, frame.height, x, y, width, height);
				renderer.render(scene, cam);
				context.clearRect(x, y, width, height);
				context.drawImage(renderer.domElement, x, y);
			}
		}
		cam.clearViewOffset();
	}
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

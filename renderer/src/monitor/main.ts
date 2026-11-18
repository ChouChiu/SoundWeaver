/**
 * 1:1 WebGL port of Spatial Audio Tools' SpeakerRoomSceneView (Dolby Atmos Renderer style):
 * - Background: #1a191d (rgb 26, 25, 29)
 * - Room proportions: 3.0 x 2.0 x 4.0 (half: 1.5, 1.0, 2.0)
 * - Inward-facing solid studio walls, translucent ceiling (0.16), dark edges (#383838)
 * - Front cinema screen (#8d8e91 inward, 0.10 outward)
 * - Floor grid (#66829e) & listener silhouette (#526e8c)
 * - Blinn/Phong object spheres (specular #141414, shininess 24)
 */

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import type { ViewPreset } from "../shared/api";
import { dbToNorm, type LayerBand, makeTextSpriteTexture, RAINBOW } from "../shared/spatial";
import { placeHead, silhouette } from "../soundfield/scene";

// Dolby Atmos Renderer room proportions: 3.0 x 2.0 x 4.0 (half: 1.5, 1.0, 2.0)
// Logic Pro room proportions: 4.0 x 2.5 x 4.0
const HALF = { x: 1.5, y: 1.0, z: 2.0 };
const SPHERE_RADIUS = 1.65;
type MonitorStyle = "person" | "room" | "logic";
type MonitorMode = "room" | "spherical";

function roomToScene([x, y, z]: [number, number, number]): [number, number, number] {
	return [
		(2 * x - 1) * HALF.x * 0.94,
		-HALF.y + z * (HALF.y * 2) * 0.94 + 0.06,
		(2 * y - 1) * HALF.z * 0.94,
	];
}

function sphericalToScene([sx, sy, sz]: [number, number, number]): [number, number, number] {
	return [
		sx * SPHERE_RADIUS,
		(sz - 0.2) * SPHERE_RADIUS * 1.15 - 0.1,
		-sy * SPHERE_RADIUS,
	];
}

interface LiveObj {
	id: number;
	x: number;
	y: number;
	z: number;
	db: number;
	active: boolean;
}

interface LiveSpeaker {
	name: string;
	pos: [number, number, number];
	spherical?: [number, number, number];
	layer?: LayerBand;
	isLfe?: boolean;
}

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setClearColor(0x1a191d, 1);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1a191d);

// Studio lighting matching SceneKit Blinn shading
scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const keyLight = new THREE.DirectionalLight(0xffffff, 1.05);
keyLight.position.set(-2.5, 4.0, 3.5);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xc8d8ea, 0.45);
fillLight.position.set(2.8, 2.0, -2.5);
scene.add(fillLight);

function buildRendererRoom(): THREE.Group {
	const group = new THREE.Group();
	const w = HALF.x * 2;
	const h = HALF.y * 2;
	const d = HALF.z * 2;

	// Baked subtle studio illumination texture for walls & floor
	function wallTex(cTop: string, cBot: string): THREE.CanvasTexture {
		const c = document.createElement("canvas");
		c.width = 64;
		c.height = 64;
		const ctx = c.getContext("2d")!;
		const g = ctx.createLinearGradient(0, 0, 0, 64);
		g.addColorStop(0, cTop);
		g.addColorStop(1, cBot);
		ctx.fillStyle = g;
		ctx.fillRect(0, 0, 64, 64);
		const t = new THREE.CanvasTexture(c);
		t.colorSpace = THREE.SRGBColorSpace;
		return t;
	}

	function floorTex(): THREE.CanvasTexture {
		const c = document.createElement("canvas");
		c.width = 512;
		c.height = 512;
		const ctx = c.getContext("2d")!;
		ctx.fillStyle = "#202228";
		ctx.fillRect(0, 0, 512, 512);
		// Soft bounce illumination from the front cinema screen (top edge = -Z)
		const glow = ctx.createRadialGradient(256, 0, 10, 256, 120, 380);
		glow.addColorStop(0, "rgba(155, 165, 180, 0.20)");
		glow.addColorStop(0.55, "rgba(110, 125, 145, 0.08)");
		glow.addColorStop(1, "rgba(32, 34, 40, 0)");
		ctx.fillStyle = glow;
		ctx.fillRect(0, 0, 512, 512);
		const t = new THREE.CanvasTexture(c);
		t.colorSpace = THREE.SRGBColorSpace;
		return t;
	}

	const wallMat = new THREE.MeshBasicMaterial({
		map: wallTex("#2c2d34", "#212228"),
		side: THREE.FrontSide,
	});
	const floorMat = new THREE.MeshBasicMaterial({
		map: floorTex(),
		side: THREE.FrontSide,
	});

	// Floor (y = -HALF.y, facing +Y)
	const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), floorMat);
	floor.rotation.x = -Math.PI / 2;
	floor.position.y = -HALF.y;
	group.add(floor);

	// Rear wall (z = +HALF.z, facing -Z)
	const rear = new THREE.Mesh(new THREE.PlaneGeometry(w, h), wallMat);
	rear.position.z = HALF.z;
	rear.rotation.y = Math.PI;
	group.add(rear);

	// Front wall (z = -HALF.z, facing +Z)
	const front = new THREE.Mesh(new THREE.PlaneGeometry(w, h), wallMat);
	front.position.z = -HALF.z;
	group.add(front);

	// Left wall (x = -HALF.x, facing +X)
	const left = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMat);
	left.position.x = -HALF.x;
	left.rotation.y = Math.PI / 2;
	group.add(left);

	// Right wall (x = +HALF.x, facing -X)
	const right = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMat);
	right.position.x = HALF.x;
	right.rotation.y = -Math.PI / 2;
	group.add(right);

	// Ceiling: solid from inside (FrontSide facing -Y), 16% from outside (BackSide)
	const ceilIn = new THREE.Mesh(new THREE.PlaneGeometry(w, d), wallMat);
	ceilIn.position.y = HALF.y;
	ceilIn.rotation.x = Math.PI / 2;
	group.add(ceilIn);

	const ceilOut = new THREE.Mesh(
		new THREE.PlaneGeometry(w, d),
		new THREE.MeshBasicMaterial({ color: 0x2b2c32, transparent: true, opacity: 0.16, side: THREE.BackSide, depthWrite: false }),
	);
	ceilOut.position.y = HALF.y;
	ceilOut.rotation.x = Math.PI / 2;
	group.add(ceilOut);

	// Cinema screen on front wall (matching addRendererScreen)
	const screenIn = new THREE.Mesh(
		new THREE.PlaneGeometry(1.96, 1.09),
		new THREE.MeshBasicMaterial({ color: 0x8d8e91, side: THREE.FrontSide, depthWrite: false }),
	);
	screenIn.position.set(0.016, 0.012, -HALF.z + 0.015);
	group.add(screenIn);

	const screenOut = new THREE.Mesh(
		new THREE.PlaneGeometry(1.96, 1.09),
		new THREE.MeshBasicMaterial({ color: 0x8d8e91, transparent: true, opacity: 0.10, side: THREE.BackSide, depthWrite: false }),
	);
	screenOut.position.set(0.016, 0.012, -HALF.z + 0.015);
	group.add(screenOut);

	// Room edges (#383838 = calibratedWhite 0.22)
	const boxGeo = new THREE.BoxGeometry(w, h, d);
	const edges = new THREE.LineSegments(
		new THREE.EdgesGeometry(boxGeo),
		new THREE.LineBasicMaterial({ color: 0x383838 }),
	);
	group.add(edges);

	// Floor grid (#66829e = calibratedRed 0.40, green 0.51, blue 0.62)
	const gridPts: number[] = [];
	const gy = -HALF.y + 0.003;
	for (let i = 1; i < 4; i++) {
		const gx = -HALF.x + (w * i) / 4;
		gridPts.push(gx, gy, -HALF.z, gx, gy, HALF.z);
		const gz = -HALF.z + (d * i) / 4;
		gridPts.push(-HALF.x, gy, gz, HALF.x, gy, gz);
	}
	const gridGeo = new THREE.BufferGeometry();
	gridGeo.setAttribute("position", new THREE.Float32BufferAttribute(gridPts, 3));
	const floorGrid = new THREE.LineSegments(
		gridGeo,
		new THREE.LineBasicMaterial({ color: 0x66829e, transparent: true, opacity: 0.65 }),
	);
	floorGrid.name = "floorGrid";
	group.add(floorGrid);

	group.userData = { wallMat, floorMat, edges, floorGrid, screenIn, screenOut, ceilIn, ceilOut };
	return group;
}

const roomGroup = buildRendererRoom();
scene.add(roomGroup);

let activeMode: MonitorMode = "room";

function buildWalkMixDome(): THREE.Group {
	const dome = new THREE.Group();
	const ringMat = new THREE.LineBasicMaterial({ color: 0x4e7aa4, transparent: true, opacity: 0.42 });
	const eqMat = new THREE.LineBasicMaterial({ color: 0x76b0e0, transparent: true, opacity: 0.65 });
	const r = SPHERE_RADIUS * 0.92;
	const cy = -0.1;

	// Horizontal latitude bands: equator (0 deg), upper (+35 deg), lower (-15 deg)
	for (const elDeg of [-15, 0, 35]) {
		const el = (elDeg * Math.PI) / 180;
		const rr = Math.cos(el) * r;
		const yy = cy + Math.sin(el) * r * 1.05;
		const pts: THREE.Vector3[] = [];
		for (let i = 0; i <= 72; i++) {
			const a = (i / 72) * Math.PI * 2;
			pts.push(new THREE.Vector3(Math.cos(a) * rr, yy, Math.sin(a) * rr));
		}
		dome.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), elDeg === 0 ? eqMat : ringMat));
	}
	// Longitudinal meridians every 45 deg
	for (let m = 0; m < 4; m++) {
		const rot = (m * Math.PI) / 4;
		const pts: THREE.Vector3[] = [];
		for (let i = 0; i <= 64; i++) {
			const a = (i / 64) * Math.PI * 2;
			pts.push(
				new THREE.Vector3(
					Math.cos(rot) * Math.cos(a) * r,
					cy + Math.sin(a) * r * 1.05,
					Math.sin(rot) * Math.cos(a) * r,
				),
			);
		}
		dome.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), ringMat));
	}
	// Front azimuth indicator on floor/equator pointing toward -Z
	const arrowPts = [
		new THREE.Vector3(0, cy - r * 0.25, -r * 0.95),
		new THREE.Vector3(-0.14, cy - r * 0.25, -r * 0.72),
		new THREE.Vector3(0.14, cy - r * 0.25, -r * 0.72),
		new THREE.Vector3(0, cy - r * 0.25, -r * 0.95),
	];
	dome.add(
		new THREE.Line(
			new THREE.BufferGeometry().setFromPoints(arrowPts),
			new THREE.LineBasicMaterial({ color: 0x6ab0ff, transparent: true, opacity: 0.85 }),
		),
	);
	dome.visible = false;
	return dome;
}

const domeGroup = buildWalkMixDome();
scene.add(domeGroup);

function positionHead(pivot: THREE.Group): void {
	if (activeMode === "spherical") {
		pivot.position.set(0, -0.58, 0);
	} else {
		pivot.position.set(0, -HALF.y * (currentStyle === "logic" ? 2.5 / 2.0 : 1.0), 0);
	}
}

let currentStyle: MonitorStyle = "person";

// Load real FBHead.obj from Spatial Audio Tools (fallback to silhouette while loading)
const headMat = new THREE.MeshLambertMaterial({ color: 0x526e8c, side: THREE.DoubleSide });
const fallbackHead = silhouette();
fallbackHead.traverse((o) => {
	const m = (o as THREE.Mesh).material as THREE.Material | undefined;
	if (m) (o as THREE.Mesh).material = headMat;
});
let activeHead: THREE.Group = placeHead(fallbackHead, () => 0.72);
positionHead(activeHead);
scene.add(activeHead);

new OBJLoader().load(
	"./FBHead.obj",
	(obj) => {
		obj.traverse((child) => {
			if ((child as THREE.Mesh).isMesh) {
				(child as THREE.Mesh).material = headMat;
			}
		});
		obj.rotation.y = Math.PI;
		const loadedPivot = placeHead(obj, (size) => 0.72 / size);
		positionHead(loadedPivot);
		loadedPivot.visible = activeHead.visible;
		scene.remove(activeHead);
		activeHead = loadedPivot;
		scene.add(activeHead);
	},
	undefined,
	() => {},
);

const cam = new THREE.PerspectiveCamera(36, window.innerWidth / window.innerHeight, 0.1, 100);
const controls = new OrbitControls(cam, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, -0.1, 0);

function viewCameraPos(preset: ViewPreset): { pos: THREE.Vector3; up: THREE.Vector3 } {
	const dist = 6.8;
	const deg = (d: number) => (d * Math.PI) / 180;
	const map: Record<string, { yaw: number; pitch: number; up: THREE.Vector3 }> = {
		iso: { yaw: deg(-45), pitch: deg(18), up: new THREE.Vector3(0, 1, 0) },
		angle: { yaw: deg(-45), pitch: deg(18), up: new THREE.Vector3(0, 1, 0) },
		top: { yaw: 0, pitch: deg(86), up: new THREE.Vector3(0, 0, -1) },
		front: { yaw: 0, pitch: deg(10), up: new THREE.Vector3(0, 1, 0) },
		back: { yaw: Math.PI, pitch: deg(10), up: new THREE.Vector3(0, 1, 0) },
		left: { yaw: -Math.PI / 2, pitch: deg(10), up: new THREE.Vector3(0, 1, 0) },
		right: { yaw: Math.PI / 2, pitch: deg(10), up: new THREE.Vector3(0, 1, 0) },
	};
	const v = map[preset] ?? map.iso;
	const cp = Math.cos(v.pitch);
	const pos = new THREE.Vector3(
		Math.sin(v.yaw) * cp * dist,
		Math.sin(v.pitch) * dist - 0.1,
		Math.cos(v.yaw) * cp * dist,
	);
	return { pos, up: v.up };
}

const initView = viewCameraPos("iso");
cam.position.copy(initView.pos);
cam.up.copy(initView.up);
controls.update();

let camAnim: {
	startPos: THREE.Vector3;
	endPos: THREE.Vector3;
	startUp: THREE.Vector3;
	endUp: THREE.Vector3;
	t: number;
} | null = null;
controls.addEventListener("start", () => {
	camAnim = null;
});

interface ObjNode {
	sphere: THREE.Mesh;
	label: THREE.Sprite;
	drop: THREE.Line;
	ring: THREE.Mesh;
	color: THREE.Color;
}

interface SpkNode {
	mesh: THREE.Mesh;
	label: THREE.Sprite;
	spec: LiveSpeaker;
}

const objNodes = new Map<number, ObjNode>();
const spkNodes: SpkNode[] = [];
const spkGroup = new THREE.Group();
scene.add(spkGroup);

let showLabels = true;
let showDrop = false;
let showSpeakers = false;
let layerVisible: Record<LayerBand, boolean> = { upper: true, middle: true, lower: true };

function getOrCreateObj(id: number): ObjNode {
	const existing = objNodes.get(id);
	if (existing) return existing;

	const color = new THREE.Color(RAINBOW[id % RAINBOW.length]);
	// Blinn/Phong shading matching SpeakerRoomSceneView.configureObjectMaterial
	const sphere = new THREE.Mesh(
		new THREE.SphereGeometry(0.09, 28, 28),
		new THREE.MeshPhongMaterial({
			color,
			emissive: color,
			emissiveIntensity: 0.12,
			specular: 0x141414,
			shininess: 24,
			transparent: true,
		}),
	);
	sphere.renderOrder = 5;
	scene.add(sphere);

	const label = new THREE.Sprite(
		new THREE.SpriteMaterial({ map: makeTextSpriteTexture(String(id + 1)), transparent: true, depthTest: false }),
	);
	label.renderOrder = 10;
	label.scale.setScalar(0.32);
	scene.add(label);

	const dropGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
	const drop = new THREE.Line(
		dropGeo,
		new THREE.LineDashedMaterial({ color, dashSize: 0.08, gapSize: 0.06, transparent: true, opacity: 0.55 }),
	);
	drop.renderOrder = 2;
	scene.add(drop);

	const ring = new THREE.Mesh(
		new THREE.RingGeometry(0.055, 0.085, 28),
		new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.65, side: THREE.DoubleSide, depthWrite: false }),
	);
	ring.rotation.x = -Math.PI / 2;
	ring.renderOrder = 2;
	scene.add(ring);

	const node = { sphere, label, drop, ring, color };
	objNodes.set(id, node);
	return node;
}

const LAYER_COLORS: Record<LayerBand, number> = {
	upper: 0x6cc8ff,
	middle: 0x62b4f2,
	lower: 0x4b93c8,
};

function syncSpeakersVisibility(): void {
	const isSpherical = activeMode === "spherical";
	spkGroup.visible = isSpherical || showSpeakers;
	for (const node of spkNodes) {
		const band = node.spec.layer ?? "middle";
		const vis = isSpherical ? (!node.spec.isLfe && layerVisible[band]) : true;
		node.mesh.visible = vis;
		node.label.visible = vis && (isSpherical || showLabels);
	}
}

function setSpeakers(speakers: LiveSpeaker[]): void {
	spkGroup.clear();
	spkNodes.length = 0;
	const boxGeom = new THREE.BoxGeometry(0.13, 0.13, 0.13);
	const orbGeom = new THREE.SphereGeometry(0.085, 22, 22);
	const isSpherical = activeMode === "spherical";
	for (const s of speakers) {
		const band = s.layer ?? "middle";
		const mat = new THREE.MeshPhongMaterial({
			color: isSpherical ? LAYER_COLORS[band] : 0x6c8eb0,
			emissive: 0x1d344d,
			emissiveIntensity: 0.25,
			specular: 0x222222,
			shininess: 24,
			transparent: true,
			opacity: isSpherical ? 0.65 : 0.85,
		});
		const mesh = new THREE.Mesh(isSpherical ? orbGeom : boxGeom, mat);
		const [x, y, z] = isSpherical && s.spherical ? sphericalToScene(s.spherical) : roomToScene(s.pos);
		mesh.position.set(x, y, z);
		spkGroup.add(mesh);

		const label = new THREE.Sprite(
			new THREE.SpriteMaterial({ map: makeTextSpriteTexture(s.name), transparent: true, depthTest: false }),
		);
		label.position.set(x, y + 0.16, z);
		label.scale.set(0.36, 0.24, 1);
		label.renderOrder = 10;
		spkGroup.add(label);

		spkNodes.push({ mesh, label, spec: s });
	}
	syncSpeakersVisibility();
}

function updateFrame(objects: LiveObj[], chLevels: number[]): void {
	const isSpherical = activeMode === "spherical";
	for (let i = 0; i < spkNodes.length; i++) {
		const node = spkNodes[i];
		const db = chLevels[i] ?? -96;
		const norm = dbToNorm(db, -60, -5);
		const active = db > -60;
		const mat = node.mesh.material as THREE.MeshPhongMaterial;
		mat.emissiveIntensity = active ? 0.35 + 1.35 * norm : 0.12;
		mat.opacity = isSpherical ? (active ? 0.96 : 0.35) : 0.85;
		node.mesh.scale.setScalar(isSpherical && active ? 0.95 + 0.55 * norm : 1.0);
	}

	const seen = new Set<number>();
	const floor = -HALF.y + 0.004;
	for (const o of objects) {
		seen.add(o.id);
		const node = getOrCreateObj(o.id);
		const visible = o.active && o.db > -80;
		node.sphere.visible = visible;
		node.label.visible = visible && showLabels;
		node.drop.visible = visible && showDrop && !isSpherical;
		node.ring.visible = visible && showDrop && !isSpherical;
		if (!visible) continue;

		const [x, y, z] = roomToScene([o.x, o.y, o.z]);
		const norm = dbToNorm(o.db, -60, -5);
		const scale = 0.85 + 0.45 * norm;

		node.sphere.position.set(x, y, z);
		node.sphere.scale.setScalar(scale);
		const sm = node.sphere.material as THREE.MeshPhongMaterial;
		sm.emissiveIntensity = 0.08 + 0.65 * norm;

		node.label.position.set(x, y + 0.18 * scale, z);

		if (showDrop) {
			const posAttr = node.drop.geometry.getAttribute("position") as THREE.BufferAttribute;
			posAttr.setXYZ(0, x, y, z);
			posAttr.setXYZ(1, x, floor, z);
			posAttr.needsUpdate = true;
			node.drop.computeLineDistances();
			node.ring.position.set(x, floor, z);
		}
	}
	for (const [id, node] of objNodes) {
		if (!seen.has(id)) {
			node.sphere.visible = false;
			node.label.visible = false;
			node.drop.visible = false;
			node.ring.visible = false;
		}
	}
}

function setView(preset: ViewPreset): void {
	const target = viewCameraPos(preset);
	camAnim = {
		startPos: cam.position.clone(),
		endPos: target.pos,
		startUp: cam.up.clone(),
		endUp: target.up,
		t: 0,
	};
}

function setOptions(opts: { showLabels?: boolean; showDrop?: boolean; showSpeakers?: boolean }): void {
	if (opts.showLabels !== undefined) showLabels = opts.showLabels;
	if (opts.showDrop !== undefined) showDrop = opts.showDrop;
	if (opts.showSpeakers !== undefined) showSpeakers = opts.showSpeakers;
	syncSpeakersVisibility();
}

function setMode(mode: MonitorMode): void {
	activeMode = mode;
	roomGroup.visible = mode === "room";
	domeGroup.visible = mode === "spherical";
	positionHead(activeHead);
	if (spkNodes.length > 0) {
		setSpeakers(spkNodes.map((n) => n.spec));
	}
	syncSpeakersVisibility();
}

function setLayers(upper: boolean, middle: boolean, lower: boolean): void {
	layerVisible = { upper, middle, lower };
	syncSpeakersVisibility();
}

window.addEventListener("resize", () => {
	const w = window.innerWidth;
	const h = window.innerHeight;
	cam.aspect = w / h;
	cam.updateProjectionMatrix();
	renderer.setSize(w, h);
});

let lastTs = performance.now();
function animate(now: number): void {
	requestAnimationFrame(animate);
	const dt = Math.min(0.05, (now - lastTs) / 1000);
	lastTs = now;
	if (camAnim) {
		camAnim.t = Math.min(1, camAnim.t + dt * 4.5);
		const ease = 1 - (1 - camAnim.t) ** 3;
		cam.position.lerpVectors(camAnim.startPos, camAnim.endPos, ease);
		cam.up.lerpVectors(camAnim.startUp, camAnim.endUp, ease).normalize();
		controls.target.set(0, -0.1, 0);
		if (camAnim.t >= 1) camAnim = null;
	}
	controls.update();
	renderer.render(scene, cam);
}
requestAnimationFrame(animate);

function setStyle(style: MonitorStyle): void {
	const ud = roomGroup.userData as {
		wallMat: THREE.MeshBasicMaterial;
		floorMat: THREE.MeshBasicMaterial;
		edges: THREE.LineSegments;
		floorGrid: THREE.LineSegments;
		screenIn: THREE.Mesh;
		screenOut: THREE.Mesh;
		ceilIn: THREE.Mesh;
		ceilOut: THREE.Mesh;
	};
	currentStyle = style;
	const isLogic = style === "logic";
	roomGroup.scale.set(isLogic ? 4.0 / 3.0 : 1.0, isLogic ? 2.5 / 2.0 : 1.0, 1.0);
	positionHead(activeHead);
	activeHead.visible = activeMode === "spherical" || style !== "room";
	ud.floorGrid.visible = style !== "room";
	ud.screenIn.visible = !isLogic;
	ud.screenOut.visible = !isLogic;
	ud.ceilIn.visible = !isLogic;
	ud.wallMat.transparent = isLogic;
	ud.wallMat.opacity = isLogic ? 0.14 : 1.0;
	ud.wallMat.depthWrite = !isLogic;
	(ud.edges.material as THREE.LineBasicMaterial).color.setHex(isLogic ? 0x66829e : 0x383838);
}

(window as unknown as Record<string, unknown>).liveMonitor = {
	setSpeakers,
	updateFrame,
	setView,
	setOptions,
	setStyle,
	setMode,
	setLayers,
};

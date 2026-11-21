/** Live 3D playback monitor for studio, Logic Pro grid, and WalkMix views. */

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import type { ViewPreset } from "../shared/api";
import { dbToNorm, type LayerBand, makeTextSprite, makeTextTexture, RAINBOW } from "../shared/spatial";
import { placeHead, silhouette } from "../shared/listener";
import type { LiveObj, LiveSpeaker, MonitorMode, MonitorOptions, MonitorStyle } from "./protocol";
import { buildRendererRoom, buildWalkMixDome, HALF, LOGIC_HALF, SPHERE_RADIUS } from "./room";

const OBJECT_RADIUS = 0.09;
const LOGIC_OBJECT_RADIUS = 0.073;
const SIZE_HALO_RADIUS_GAIN = 3;
const STUDIO_EAR_HEIGHT = 0.5;
const STUDIO_SIGNAL_FLOOR_DB = -90;
const SELECTION_RADIUS = 0.132;
const ROOM_CLEARANCE = 0.004;
const SELECTED_OBJECT_COLOR = new THREE.Color(0xe0fbff);
const STUDIO_SELECTED_COLOR = new THREE.Color(0x56acff);
const STUDIO_IDLE_COLOR = new THREE.Color(0x89939d);
const STUDIO_QUIET_COLOR = new THREE.Color(0x21704c);
const STUDIO_LOUD_COLOR = new THREE.Color(0x24c775);
const studioSignalColor = new THREE.Color();
const LOGIC_OBJECT_COLOR = new THREE.Color(0x2965a6);
const LOGIC_RIM_COLOR = new THREE.Color(0x539beb);
let currentStyle: MonitorStyle = "person";
let activeMode: MonitorMode = "room";

function isLogicRoom(): boolean {
	return currentStyle === "logic" && activeMode === "room";
}

function activeHalf(): { x: number; y: number; z: number } {
	return isLogicRoom() ? LOGIC_HALF : HALF;
}

function roomToScene([x, y, z]: [number, number, number]): [number, number, number] {
	const h = activeHalf();
	const base = activeMode === "room" && currentStyle === "person" ? STUDIO_EAR_HEIGHT : 0.06;
	const top = 2 * h.y * 0.94 + 0.06;
	return [
		(2 * x - 1) * h.x * 0.94,
		-h.y + base + z * (top - base),
		(2 * y - 1) * h.z * 0.94,
	];
}

function sphericalToScene([sx, sy, sz]: [number, number, number]): [number, number, number] {
	return [
		sx * SPHERE_RADIUS,
		(sz - 0.2) * SPHERE_RADIUS * 1.15 - 0.1,
		-sy * SPHERE_RADIUS,
	];
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

const { group: roomGroup, outline: roomOutline, setStyle: styleRoom } = buildRendererRoom();
scene.add(roomGroup);

const domeGroup = buildWalkMixDome();
scene.add(domeGroup);

function positionHead(pivot: THREE.Group): void {
	if (activeMode === "spherical") {
		pivot.position.set(0, -0.58, 0);
	} else {
		pivot.position.set(0, -activeHalf().y, 0);
	}
}

// Keep the silhouette visible until the packaged listener mesh has loaded.
const headMat = new THREE.MeshLambertMaterial({ color: 0x526e8c, side: THREE.DoubleSide });

function styleHead(head: THREE.Object3D): void {
	head.traverse((child) => {
		if (child instanceof THREE.Mesh) child.material = headMat;
	});
}

const fallbackHead = silhouette();
styleHead(fallbackHead);
let activeHead = placeHead(fallbackHead, () => 0.72);
positionHead(activeHead);
scene.add(activeHead);

// The monitor is opened as a local file in Qt WebEngine, where Fetch cannot read the OBJ.
const headRequest = new XMLHttpRequest();
headRequest.open("GET", "./FBHead.obj");
headRequest.addEventListener("load", () => {
	if (headRequest.status !== 0 && headRequest.status !== 200) return;
	const model = new OBJLoader().parse(headRequest.responseText);
	styleHead(model);
	model.rotation.y = Math.PI;
	const pivot = placeHead(model, (size) => 0.72 / size);
	positionHead(pivot);
	pivot.visible = activeHead.visible;
	scene.remove(activeHead);
	activeHead = pivot;
	scene.add(activeHead);
});
headRequest.send();

const cam = new THREE.PerspectiveCamera(36, window.innerWidth / window.innerHeight, 0.1, 100);
const controls = new OrbitControls(cam, renderer.domElement);
const VIEW_TARGET = new THREE.Vector3(0, -0.1, 0);
const STUDIO_VIEW_DISTANCE = 6.8;
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.copy(VIEW_TARGET);

function viewDistance(
	mode: MonitorMode,
	logic: boolean,
	direction: THREE.Vector3,
	up: THREE.Vector3,
	aspect = cam.aspect,
): number {
	if (mode === "spherical") return STUDIO_VIEW_DISTANCE;
	const half = logic ? LOGIC_HALF : HALF;
	const right = new THREE.Vector3().crossVectors(up, direction).normalize();
	const vertical = new THREE.Vector3().crossVectors(direction, right);
	const halfHeight = Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)) * 0.9;
	const corner = new THREE.Vector3();
	let distance = logic ? 8.2 : STUDIO_VIEW_DISTANCE;
	// Keep each corner within both sides of the perspective frustum.
	for (const x of [-half.x, half.x]) {
		for (const y of [-half.y, half.y]) {
			for (const z of [-half.z, half.z]) {
				corner.set(x, y, z).sub(VIEW_TARGET);
				const depth = corner.dot(direction);
				const horizontal = Math.abs(corner.dot(right)) / (halfHeight * aspect);
				const height = Math.abs(corner.dot(vertical)) / halfHeight;
				distance = Math.max(distance, depth + horizontal, depth + height);
			}
		}
	}
	return distance;
}

function viewCameraPos(preset: ViewPreset): { pos: THREE.Vector3; up: THREE.Vector3 } {
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
	const direction = new THREE.Vector3(
		Math.sin(v.yaw) * cp,
		Math.sin(v.pitch),
		Math.cos(v.yaw) * cp,
	);
	const dist = viewDistance(activeMode, isLogicRoom(), direction, v.up);
	const pos = direction.multiplyScalar(dist).add(VIEW_TARGET);
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

function scaleView(scaleFor: (direction: THREE.Vector3, up: THREE.Vector3) => number): void {
	const scale = (position: THREE.Vector3, target: THREE.Vector3, up: THREE.Vector3) => {
		position.sub(target);
		position.multiplyScalar(scaleFor(position.clone().normalize(), up)).add(target);
	};
	scale(cam.position, controls.target, cam.up);
	if (camAnim) {
		scale(camAnim.startPos, controls.target, camAnim.startUp);
		scale(camAnim.endPos, VIEW_TARGET, camAnim.endUp);
	}
	controls.update();
}

function syncViewDistance(previousMode: MonitorMode, previousLogic: boolean): void {
	const isLogic = isLogicRoom();
	if (activeMode === previousMode && isLogic === previousLogic) return;
	scaleView((direction, up) =>
		viewDistance(activeMode, isLogic, direction, up)
		/ viewDistance(previousMode, previousLogic, direction, up));
}

interface ObjNode {
	sphere: THREE.Mesh<THREE.SphereGeometry, THREE.Material>;
	studioMaterial: THREE.MeshPhongMaterial;
	sizeHalo: THREE.Sprite;
	label: THREE.Sprite;
	labelMap: THREE.Texture | null;
	compactLabelMap: THREE.CanvasTexture;
	drop: THREE.Line;
	ring: THREE.Mesh;
	color: THREE.Color;
}

interface SpkNode {
	mesh: THREE.Mesh<THREE.BufferGeometry, THREE.MeshPhongMaterial>;
	label: THREE.Sprite;
	spec: LiveSpeaker;
}

const objNodes = new Map<number, ObjNode>();
let latestObjects: LiveObj[] = [];
let selectedObject: number | null = null;
const studioObjectGeometry = new THREE.SphereGeometry(OBJECT_RADIUS, 28, 28);
const studioIdleMaterial = new THREE.MeshBasicMaterial({
	color: STUDIO_IDLE_COLOR,
	transparent: true,
	opacity: 0.27,
	depthWrite: false,
});
const logicObjectGeometry = new THREE.SphereGeometry(LOGIC_OBJECT_RADIUS, 28, 28);
const logicObjectMaterial = new THREE.ShaderMaterial({
	transparent: true,
	uniforms: {
		coreColor: { value: LOGIC_OBJECT_COLOR },
		rimColor: { value: LOGIC_RIM_COLOR },
	},
	vertexShader: `
		varying vec3 viewNormal;
		varying vec3 toCamera;

		void main() {
			vec4 viewPosition = modelViewMatrix * vec4(position, 1.0);
			viewNormal = normalMatrix * normal;
			toCamera = -viewPosition.xyz;
			gl_Position = projectionMatrix * viewPosition;
		}
	`,
	fragmentShader: `
		uniform vec3 coreColor;
		uniform vec3 rimColor;
		varying vec3 viewNormal;
		varying vec3 toCamera;

		void main() {
			float facing = max(dot(normalize(viewNormal), normalize(toCamera)), 0.0);
			float radius = sqrt(max(0.0, 1.0 - facing * facing));
			float rim = smoothstep(0.46, 0.99, radius);
			gl_FragColor = vec4(mix(coreColor, rimColor, rim), 1.0);
			#include <colorspace_fragment>
		}
	`,
});
const logicSelectedMaterial = logicObjectMaterial.clone();
logicSelectedMaterial.uniforms.coreColor.value.setHex(0xe8f2ff);
logicSelectedMaterial.uniforms.rimColor.value.setHex(0xffffff);

function makeSizeHaloTexture(): THREE.CanvasTexture {
	const canvas = document.createElement("canvas");
	canvas.width = canvas.height = 128;
	const ctx = canvas.getContext("2d")!;
	ctx.beginPath();
	ctx.arc(64, 64, 62, 0, Math.PI * 2);
	ctx.fillStyle = "rgba(255,255,255,0.16)";
	ctx.fill();
	ctx.strokeStyle = "rgba(255,255,255,0.52)";
	ctx.lineWidth = 3;
	ctx.stroke();
	const texture = new THREE.CanvasTexture(canvas);
	texture.colorSpace = THREE.SRGBColorSpace;
	return texture;
}

const sizeHaloTexture = makeSizeHaloTexture();
const selectionHalo = new THREE.Mesh(
	new THREE.RingGeometry(0.119, SELECTION_RADIUS, 40),
	new THREE.MeshBasicMaterial({
		color: SELECTED_OBJECT_COLOR,
		side: THREE.DoubleSide,
		transparent: true,
		opacity: 0.92,
		depthWrite: false,
	}),
);
selectionHalo.visible = false;
selectionHalo.renderOrder = 8;
scene.add(selectionHalo);
const spkNodes: SpkNode[] = [];
const spkGroup = new THREE.Group();
scene.add(spkGroup);

let showLabels = true;
const seen = new Set<number>(); // scratch for updateObjects, cleared per call
let showDrop = false;
let showSpeakers = false;
let layerVisible: Record<LayerBand, boolean> = { upper: true, middle: true, lower: true };

function getOrCreateObj(id: number): ObjNode {
	const existing = objNodes.get(id);
	if (existing) return existing;

	const color = new THREE.Color(RAINBOW[id % RAINBOW.length]);
	const studioMaterial = new THREE.MeshPhongMaterial({
		color,
		emissive: color,
		emissiveIntensity: 0.12,
		specular: 0x141414,
		shininess: 24,
		transparent: true,
	});
	const sphere = new THREE.Mesh(studioObjectGeometry, studioMaterial);
	sphere.renderOrder = 5;
	scene.add(sphere);

	const sizeHalo = new THREE.Sprite(new THREE.SpriteMaterial({
		map: sizeHaloTexture, transparent: true, depthWrite: false,
	}));
	sizeHalo.renderOrder = 4;
	sizeHalo.visible = false;
	scene.add(sizeHalo);

	const label = makeTextSprite(String(id + 1), 0.32);
	const labelMap = label.material.map;
	const compactLabelMap = makeTextTexture(String(id + 1), "compact");
	label.renderOrder = 10;
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

	const node = { sphere, studioMaterial, sizeHalo, label, labelMap, compactLabelMap, drop, ring, color };
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
	const showSpeakerLabels = isSpherical || isLogicRoom() || showLabels;
	spkGroup.visible = isSpherical || showSpeakers;
	for (const node of spkNodes) {
		const vis = !isSpherical || layerVisible[node.spec.layer];
		node.mesh.visible = vis;
		node.label.visible = vis && showSpeakerLabels;
	}
	markDirty();
}

function positionSpeaker({ mesh, label, spec }: SpkNode): void {
	const [left, depth, height] = spec.position;
	const [x, y, z] = activeMode === "spherical"
		? sphericalToScene(spec.position)
		: roomToScene([left, depth, Math.max(0, height)]);
	mesh.position.set(x, y, z);
	label.position.set(x, y + 0.16, z);
}

const speakerBox = new THREE.BoxGeometry(0.13, 0.13, 0.13);
const speakerSphere = new THREE.SphereGeometry(0.085, 22, 22);

function setSpeakers(speakers: LiveSpeaker[]): void {
	for (const { mesh, label } of spkNodes) {
		mesh.material.dispose();
		label.material.map?.dispose();
		label.material.dispose();
	}
	spkGroup.clear();
	spkNodes.length = 0;
	const isSpherical = activeMode === "spherical";
	for (const s of speakers) {
		const mat = new THREE.MeshPhongMaterial({
			color: isSpherical ? LAYER_COLORS[s.layer] : 0x6c8eb0,
			emissive: 0x1d344d,
			emissiveIntensity: 0.25,
			specular: 0x222222,
			shininess: 24,
			transparent: true,
			opacity: isSpherical ? 0.65 : 0.85,
		});
		const mesh = new THREE.Mesh(isSpherical ? speakerSphere : speakerBox, mat);
		spkGroup.add(mesh);

		const label = makeTextSprite(s.name, 0.24);
		label.renderOrder = 10;
		spkGroup.add(label);

		const node = { mesh, label, spec: s };
		positionSpeaker(node);
		spkNodes.push(node);
	}
	syncSpeakersVisibility();
	markDirty();
}

function updateFrame(objects: LiveObj[], chLevels: number[]): void {
	const isSpherical = activeMode === "spherical";
	for (const node of spkNodes) {
		const db = chLevels[node.spec.channel] ?? -96;
		const norm = dbToNorm(db, -60, -5);
		const active = db > -60;
		const mat = node.mesh.material;
		mat.emissiveIntensity = active ? 0.35 + 1.35 * norm : 0.12;
		mat.opacity = isSpherical ? (active ? 0.96 : 0.35) : 0.85;
		node.mesh.scale.setScalar(isSpherical && active ? 0.95 + 0.55 * norm : 1.0);
	}

	latestObjects = objects;
	updateObjects();
}

function updateObjects(): void {
	markDirty();
	const isSpherical = activeMode === "spherical";
	const isLogic = isLogicRoom();
	const isStudio = !isLogic && !isSpherical;
	selectionHalo.visible = false;
	seen.clear();
	const half = activeHalf();
	const floor = -half.y + ROOM_CLEARANCE;
	for (const o of latestObjects) {
		seen.add(o.id);
		const node = getOrCreateObj(o.id);
		const selected = o.id === selectedObject;
		const audible = o.active && (isStudio ? o.db >= STUDIO_SIGNAL_FLOOR_DB : o.db > -80);
		const highlighted = selected || audible;
		node.sphere.visible = isStudio || highlighted;
		node.label.visible = !isLogic && (selected || ((isStudio || audible) && showLabels));
		node.drop.visible = node.ring.visible = highlighted && showDrop && !isSpherical;
		const size = THREE.MathUtils.clamp(o.size ?? 0, 0, 1);
		node.sizeHalo.visible = !isSpherical && node.sphere.visible && size > 0;
		if (!node.sphere.visible) continue;

		const norm = dbToNorm(o.db, isStudio ? STUDIO_SIGNAL_FLOOR_DB : -60, -5);
		const scale = isSpherical ? 0.85 + 0.45 * norm + (selected ? 0.12 : 0) : 1;
		let color = selected ? SELECTED_OBJECT_COLOR : isLogic ? LOGIC_RIM_COLOR : node.color;
		if (isStudio) {
			color = selected
				? STUDIO_SELECTED_COLOR
				: studioSignalColor.lerpColors(STUDIO_QUIET_COLOR, STUDIO_LOUD_COLOR, norm);
		}
		const shape = isLogic ? logicObjectGeometry : studioObjectGeometry;
		if (node.sphere.geometry !== shape) node.sphere.geometry = shape;
		const radius = node.sphere.geometry.parameters.radius * scale;

		const position = node.sphere.position;
		position.set(...roomToScene([o.x, o.y, o.z]));
		if (!isSpherical) {
			const inset = radius + ROOM_CLEARANCE;
			position.x = THREE.MathUtils.clamp(position.x, -half.x + inset, half.x - inset);
			position.y = THREE.MathUtils.clamp(position.y, -half.y + inset, half.y - inset);
			position.z = THREE.MathUtils.clamp(position.z, -half.z + inset, half.z - inset);
		}
		node.sphere.scale.setScalar(scale);
		if (node.sizeHalo.visible) {
			node.sizeHalo.position.copy(position);
			node.sizeHalo.scale.setScalar(2 * radius * (1 + SIZE_HALO_RADIUS_GAIN * size));
			node.sizeHalo.material.color.copy(highlighted ? color : STUDIO_IDLE_COLOR);
			node.sizeHalo.material.opacity = isLogic ? 0.65 : highlighted ? 1 : 0.45;
		}
		if (node.label.visible) {
			const labelHeight = isStudio ? 0.27 : 0.32;
			node.label.scale.set(labelHeight * 1.5, labelHeight, 1);
			const map = isSpherical ? node.labelMap : node.compactLabelMap;
			if (node.label.material.map !== map) node.label.material.map = map;
			node.label.material.opacity = highlighted ? 1 : 0.45;
		}
		if (isLogic) {
			node.sphere.material = selected ? logicSelectedMaterial : logicObjectMaterial;
		} else if (isStudio && !highlighted) {
			node.sphere.material = studioIdleMaterial;
		} else {
			node.sphere.material = node.studioMaterial;
			node.studioMaterial.color.copy(color);
			node.studioMaterial.emissive.copy(color);
			node.studioMaterial.emissiveIntensity = isStudio ? 0.24 : selected ? 0.65 : 0.08 + 0.65 * norm;
		}

		if (selected) {
			selectionHalo.visible = true;
			selectionHalo.material.color.copy(color);
			selectionHalo.position.copy(position);
			selectionHalo.scale.setScalar(radius / OBJECT_RADIUS);
		}

		if (node.drop.visible) {
			const { x, y, z } = position;
			(node.drop.material as THREE.LineDashedMaterial).color.copy(color);
			(node.ring.material as THREE.MeshBasicMaterial).color.copy(color);
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
			node.sizeHalo.visible = false;
			node.label.visible = false;
			node.drop.visible = false;
			node.ring.visible = false;
		}
	}
}

function setSelectedObject(id: number | null): void {
	selectedObject = id;
	updateObjects();
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

function setOptions(opts: Partial<MonitorOptions>): void {
	if (opts.showLabels !== undefined) showLabels = opts.showLabels;
	if (opts.showDrop !== undefined) showDrop = opts.showDrop;
	if (opts.showSpeakers !== undefined) showSpeakers = opts.showSpeakers;
	syncSpeakersVisibility();
	updateObjects();
}

function setMode(mode: MonitorMode): void {
	const previousMode = activeMode;
	const wasLogic = isLogicRoom();
	activeMode = mode;
	roomGroup.visible = mode === "room";
	domeGroup.visible = mode === "spherical";
	syncRoomStyle();
	syncViewDistance(previousMode, wasLogic);
	syncSpeakersVisibility();
	updateObjects();
}

function setLayers(upper: boolean, middle: boolean, lower: boolean): void {
	layerVisible = { upper, middle, lower };
	syncSpeakersVisibility();
}

window.addEventListener("resize", () => {
	const w = window.innerWidth;
	const h = window.innerHeight;
	const previousAspect = cam.aspect;
	cam.aspect = w / h;
	cam.updateProjectionMatrix();
	renderer.setSize(w, h);
	if (activeMode === "room") {
		const isLogic = isLogicRoom();
		scaleView((direction, up) =>
			viewDistance(activeMode, isLogic, direction, up)
			/ viewDistance(activeMode, isLogic, direction, up, previousAspect));
	}
	markDirty();
});

let lastTs = performance.now();
const screenDown = new THREE.Vector3();
const roomEye = new THREE.Vector3();
// Render on demand: telemetry, camera moves and style changes set the flag, and
// a paused monitor with a settled camera costs no GPU frames at all.
let needsRender = true;
function markDirty(): void {
	needsRender = true;
}
controls.addEventListener("change", markDirty);
function animate(now: number): void {
	requestAnimationFrame(animate);
	const dt = Math.min(0.05, (now - lastTs) / 1000);
	lastTs = now;
	if (camAnim) {
		camAnim.t = Math.min(1, camAnim.t + dt * 4.5);
		const ease = 1 - (1 - camAnim.t) ** 3;
		cam.position.lerpVectors(camAnim.startPos, camAnim.endPos, ease);
		cam.up.lerpVectors(camAnim.startUp, camAnim.endUp, ease).normalize();
		controls.target.copy(VIEW_TARGET);
		if (camAnim.t >= 1) camAnim = null;
		markDirty();
	}
	controls.update();
	if (activeMode === "room") {
		roomOutline.update(roomGroup.worldToLocal(roomEye.copy(cam.position)), isLogicRoom());
	}
	if (selectionHalo.visible) {
		selectionHalo.quaternion.copy(cam.quaternion);
		markDirty();
	}
	if (!needsRender) return;
	needsRender = false;
	// Move in camera space so the labels stay below their spheres in every view.
	screenDown.set(0, -1, 0).applyQuaternion(cam.quaternion);
	const labelGap = activeMode === "room" ? 0.26 : 0.32;
	for (const [id, { sphere, label }] of objNodes) {
		if (!label.visible) continue;
		const radius = id === selectedObject
			? SELECTION_RADIUS * selectionHalo.scale.x
			: sphere.geometry.parameters.radius * sphere.scale.x;
		const offset = radius + label.scale.y * labelGap;
		label.position.copy(sphere.position).addScaledVector(screenDown, offset);
	}
	renderer.render(scene, cam);
}
requestAnimationFrame(animate);

function syncRoomStyle(): void {
	const isLogic = isLogicRoom();
	const bgHex = isLogic ? 0x161e29 : 0x1a191d;
	(scene.background as THREE.Color).setHex(bgHex);
	renderer.setClearColor(bgHex, 1);
	headMat.color.setHex(isLogic ? 0x627789 : activeMode === "spherical" ? 0x526e8c : 0xb4b8bf);

	styleRoom(isLogic, currentStyle === "room");
	positionHead(activeHead);
	markDirty();
	activeHead.visible = activeMode === "spherical" || currentStyle !== "room";
}

function setStyle(style: MonitorStyle): void {
	const wasLogic = isLogicRoom();
	currentStyle = style;
	syncRoomStyle();
	syncViewDistance(activeMode, wasLogic);
	for (const node of spkNodes) positionSpeaker(node);
	syncSpeakersVisibility();
	updateObjects();
}

syncRoomStyle();

window.liveMonitor = {
	setSpeakers,
	updateFrame,
	setView,
	setOptions,
	setStyle,
	setSelectedObject,
	setMode,
	setLayers,
};

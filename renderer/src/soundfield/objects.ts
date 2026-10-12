/**
 * One Atmos object on screen: the sphere, a halo that swells with its level,
 * the path it has just travelled, and a drop line to the floor.  Everything is
 * set from the song time alone, so a frame never depends on the ones before.
 */

import * as THREE from "three";
import { Line2 } from "three/examples/jsm/lines/Line2.js";
import { LineGeometry } from "three/examples/jsm/lines/LineGeometry.js";
import type { LineMaterial } from "three/examples/jsm/lines/LineMaterial.js";
import type { LineSegments2 } from "three/examples/jsm/lines/LineSegments2.js";
import type { ColorMode, SoundfieldOptions } from "../shared/api";
import { lineMaterial, segments } from "./lines";
import { objectMesh, ROOM_HALF, TINT, toScene } from "./scene";
import * as timeline from "./timeline";

// level range mapped onto 0..1 of brightness, glow and size
const FLOOR_DB = -60;
const TOP_DB = -10;
// samples along a trail
const TRAIL_POINTS = 48;
const HALO_SIZE = 0.75;
// scene units per second; anything faster between two trail samples is a jump
const MAX_TRAIL_SPEED = 12;

let haloTexture: THREE.Texture | null = null;

/** Soft round spot, white in the middle, gone at the edge; shared by every halo. */
function halo(): THREE.Texture {
	if (haloTexture) return haloTexture;
	const size = 128;
	const canvas = document.createElement("canvas");
	canvas.width = canvas.height = size;
	const ctx = canvas.getContext("2d")!;
	const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
	g.addColorStop(0, "rgba(255,255,255,1)");
	g.addColorStop(0.25, "rgba(255,255,255,0.45)");
	g.addColorStop(0.6, "rgba(255,255,255,0.1)");
	g.addColorStop(1, "rgba(255,255,255,0)");
	ctx.fillStyle = g;
	ctx.fillRect(0, 0, size, size);
	haloTexture = new THREE.CanvasTexture(canvas);
	haloTexture.colorSpace = THREE.SRGBColorSpace;
	return haloTexture;
}

const tint = new THREE.Color(TINT);
const scratch = new THREE.Color();

/** The object's colour at room height `z` (0 floor .. 1 ceiling). */
function colorFor(mode: ColorMode, index: number, z: number, out: THREE.Color): THREE.Color {
	if (mode === "height") return out.setHSL(0.6 - 0.55 * Math.min(1, Math.max(0, z)), 0.75, 0.6);
	// golden-ratio hues keep neighbouring ids apart
	if (mode === "object") return out.setHSL((0.58 + index * 0.618034) % 1, 0.7, 0.62);
	return out.copy(tint);
}

/** dBFS at `t`, linear between the trace's level steps. */
function levelAt(levels: number[], start: number, step: number, t: number): number {
	const f = (t - start) / step;
	if (f <= 0) return levels[0] ?? FLOOR_DB;
	if (f >= levels.length - 1) return levels[levels.length - 1] ?? FLOOR_DB;
	const i = Math.floor(f);
	return levels[i] + (levels[i + 1] - levels[i]) * (f - i);
}

export interface ObjectData {
	index: number;
	path: timeline.Path;
	vis: timeline.Visibility;
	// in the video's track, 0..1; absent when the video shows the whole mix
	inTrack: timeline.Visibility | null;
	levels: number[];
	levelStart: number;
	levelStep: number;
}

export class ObjectView {
	private readonly sphere = objectMesh();
	private readonly halo: THREE.Sprite;
	private readonly trail: Line2 | null = null;
	private readonly trailPositions: Float32Array | null = null;
	private readonly trailColors: Float32Array | null = null;
	private readonly drop: LineSegments2 | null = null;
	private readonly ring: THREE.Mesh | null = null;

	constructor(
		scene: THREE.Scene,
		private readonly data: ObjectData,
		private readonly options: SoundfieldOptions,
	) {
		scene.add(this.sphere);
		this.halo = new THREE.Sprite(
			new THREE.SpriteMaterial({
				map: halo(),
				blending: THREE.AdditiveBlending,
				transparent: true,
				depthWrite: false,
				opacity: 0,
			}),
		);
		this.halo.renderOrder = 6;
		this.halo.visible = options.glow > 0;
		scene.add(this.halo);

		if (options.trail > 0) {
			const geometry = new LineGeometry();
			this.trailPositions = new Float32Array(TRAIL_POINTS * 3);
			this.trailColors = new Float32Array(TRAIL_POINTS * 3);
			geometry.setPositions(this.trailPositions);
			geometry.setColors(this.trailColors);
			const material = lineMaterial({
				vertexColors: true,
				linewidth: 2.5,
				transparent: true,
				depthWrite: false,
			});
			// The vertex colours carry the fade as brightness.  Move it into alpha, so the
			// tail of a trail turns transparent instead of black in the alpha video.
			material.onBeforeCompile = (shader) => {
				shader.fragmentShader = shader.fragmentShader.replace(
					"gl_FragColor = vec4( diffuseColor.rgb, alpha );",
					`float fade = max( diffuseColor.r, max( diffuseColor.g, diffuseColor.b ) );
					gl_FragColor = vec4( diffuseColor.rgb / max( fade, 1e-4 ), alpha * fade );`,
				);
			};
			material.customProgramCacheKey = () => "trail-fade-alpha";
			this.trail = new Line2(geometry, material);
			this.trail.frustumCulled = false;
			this.trail.renderOrder = 4;
			scene.add(this.trail);
		}

		if (options.drop) {
			this.drop = segments(
				[0, 0, 0, 0, -1, 0],
				lineMaterial({ transparent: true, dashed: true, dashSize: 0.06, gapSize: 0.05, depthWrite: false, linewidth: 1 }),
			);
			this.drop.renderOrder = 2;
			scene.add(this.drop);
			this.ring = new THREE.Mesh(
				new THREE.RingGeometry(0.05, 0.075, 32),
				new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, side: THREE.DoubleSide }),
			);
			this.ring.rotation.x = -Math.PI / 2;
			this.ring.renderOrder = 2;
			scene.add(this.ring);
		}
	}

	/** 0..1 loudness at `t`, shaped so quiet passages still read. */
	private energy(t: number): number {
		const { levels, levelStart, levelStep } = this.data;
		const db = levelAt(levels, levelStart, levelStep, t);
		const e = Math.min(1, Math.max(0, (db - FLOOR_DB) / (TOP_DB - FLOOR_DB)));
		return e ** 1.3;
	}

	/** Opacity the object has at `t`: audible, and in the video's track. */
	private presence(t: number): number {
		const { vis, inTrack } = this.data;
		const v = timeline.visibilityAt(vis, t);
		const focus = inTrack ? this.options.dim + (1 - this.options.dim) * timeline.visibilityAt(inTrack, t) : 1;
		return v * focus;
	}

	update(t: number): void {
		const { options, data } = this;
		const room = timeline.positionAt(data.path, t);
		const [x, y, z] = toScene(room);
		const v = timeline.visibilityAt(data.vis, t);
		const alpha = this.presence(t);
		const energy = this.energy(t) * options.levelResponse;
		const color = colorFor(options.colorMode, data.index, room[2], scratch);
		const shown = v > 0.02;

		const sphere = this.sphere;
		sphere.position.set(x, y, z);
		sphere.scale.setScalar(options.objectScale * (0.4 + 0.6 * v) * (1 + 0.35 * energy));
		sphere.visible = shown;
		const mat = sphere.material as THREE.MeshStandardMaterial;
		mat.color.copy(color);
		mat.emissive.copy(color);
		mat.emissiveIntensity = 0.12 + 1.1 * energy;
		mat.opacity = alpha;
		mat.depthWrite = alpha >= 0.95;

		const halo = this.halo;
		if (options.glow > 0) {
			halo.position.set(x, y, z);
			halo.scale.setScalar(HALO_SIZE * options.objectScale * (0.6 + 0.9 * energy));
			halo.visible = shown;
			const hm = halo.material;
			hm.color.copy(color);
			hm.opacity = alpha * options.glow * (0.15 + 0.85 * energy);
		}

		if (this.trail) this.updateTrail(t);

		if (this.drop && this.ring) {
			const floor = -ROOM_HALF.y + 0.004;
			const ends = this.drop.geometry.getAttribute("instanceStart") as THREE.InterleavedBufferAttribute;
			ends.data.array.set([x, y, z, x, floor, z]);
			ends.data.needsUpdate = true;
			this.drop.geometry.computeBoundingSphere();
			this.drop.computeLineDistances();
			this.drop.visible = this.ring.visible = shown && alpha > 0.05;
			const dm = this.drop.material as LineMaterial;
			dm.color.copy(color);
			dm.opacity = 0.45 * alpha;
			this.ring.position.set(x, floor, z);
			const rm = this.ring.material as THREE.MeshBasicMaterial;
			rm.color.copy(color);
			rm.opacity = 0.6 * alpha;
		}
	}

	/** The last `trail` seconds of the path, brightest at the object, each point as visible as the object was then. */
	private updateTrail(t: number): void {
		const positions = this.trailPositions!;
		const colors = this.trailColors!;
		const { options, data } = this;
		const c = new THREE.Color();
		const step = options.trail / (TRAIL_POINTS - 1);
		let any = false;
		// once the path jumps (a key without a ramp moves the object at once), what lies
		// behind is another place: the trail stops there instead of drawing a line across the room
		let cut = false;
		let px = 0;
		let py = 0;
		let pz = 0;
		for (let i = 0; i < TRAIL_POINTS; i++) {
			const at = Math.max(0, t - step * i);
			const room = timeline.positionAt(data.path, at);
			const [x, y, z] = toScene(room);
			if (i > 0 && !cut && Math.hypot(x - px, y - py, z - pz) > MAX_TRAIL_SPEED * step) cut = true;
			// past the jump every point sits on the last one before it: zero-length, nothing drawn
			if (!cut) [px, py, pz] = [x, y, z];
			positions.set([px, py, pz], i * 3);
			const fade = (1 - i / (TRAIL_POINTS - 1)) ** 1.6;
			const strength = cut ? 0 : fade * this.presence(at) * 0.85;
			if (strength > 0.01) any = true;
			colorFor(options.colorMode, data.index, room[2], c).multiplyScalar(strength);
			colors.set([c.r, c.g, c.b], i * 3);
		}
		const trail = this.trail!;
		// setPositions/setColors would reallocate; write the interleaved buffers in place
		const geometry = trail.geometry as LineGeometry;
		writeSegments(geometry, "instanceStart", positions);
		writeSegments(geometry, "instanceColorStart", colors);
		geometry.computeBoundingSphere();
		trail.visible = any;
	}
}

/** LineGeometry keeps segments as [start xyz, end xyz] pairs in one interleaved buffer. */
function writeSegments(geometry: LineGeometry, attribute: string, points: Float32Array): void {
	const buffer = (geometry.getAttribute(attribute) as THREE.InterleavedBufferAttribute).data;
	const array = buffer.array as Float32Array;
	for (let i = 0; i < TRAIL_POINTS - 1; i++) {
		array.set(points.subarray(i * 3, i * 3 + 3), i * 6);
		array.set(points.subarray(i * 3 + 3, i * 3 + 6), i * 6 + 3);
	}
	buffer.needsUpdate = true;
}

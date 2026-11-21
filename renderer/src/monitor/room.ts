import * as THREE from "three";
import { makeRoomOutline } from "./room_outline";

// Dolby Atmos Renderer room proportions: 3.0 x 2.0 x 4.0 (half: 1.5, 1.0, 2.0)
// Logic Pro room proportions: 4.0 x 2.5 x 4.0 (half: 2.0, 1.25, 2.0)
export const HALF = { x: 1.5, y: 1.0, z: 2.0 };
export const LOGIC_HALF = { x: 2.0, y: 1.25, z: 2.0 };
export const SPHERE_RADIUS = 1.65;

export function buildRendererRoom() {
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
		ctx.fillStyle = "#303136";
		ctx.fillRect(0, 0, 512, 512);
		const glow = ctx.createRadialGradient(210, 70, 12, 225, 145, 390);
		glow.addColorStop(0, "rgba(183, 184, 188, 0.36)");
		glow.addColorStop(0.55, "rgba(154, 158, 164, 0.14)");
		glow.addColorStop(1, "rgba(150, 150, 150, 0)");
		ctx.fillStyle = glow;
		ctx.fillRect(0, 0, 512, 512);
		const t = new THREE.CanvasTexture(c);
		t.colorSpace = THREE.SRGBColorSpace;
		return t;
	}

	const wallMat = new THREE.MeshBasicMaterial({
		map: wallTex("#626368", "#4f5055"),
		transparent: true,
		opacity: 0.85,
		depthWrite: false,
	});
	const sideWallMat = wallMat.clone();
	sideWallMat.map = wallTex("#505157", "#3c3d43");
	const floorMat = new THREE.MeshBasicMaterial({
		map: floorTex(),
		side: THREE.FrontSide,
		polygonOffset: true,
		polygonOffsetFactor: 1,
		polygonOffsetUnits: 1,
	});
	// Move the fills back in depth so their edges stay continuous at grazing angles.
	const logicWallMat = new THREE.MeshBasicMaterial({
		color: 0x27384a, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
	});
	const logicSideWallMat = logicWallMat.clone();
	logicSideWallMat.color.setHex(0x202f40);
	const logicFloorMat = logicWallMat.clone();
	logicFloorMat.color.setHex(0x233242);

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

	// The ceiling faces into the room, just like the floor and walls.
	const ceilIn = new THREE.Mesh(new THREE.PlaneGeometry(w, d), wallMat);
	ceilIn.position.y = HALF.y;
	ceilIn.rotation.x = Math.PI / 2;
	group.add(ceilIn);

	// The studio room view uses the screen as its front reference.
	const screenIn = new THREE.Mesh(
		new THREE.PlaneGeometry(1.96, 1.09),
		new THREE.MeshBasicMaterial({ color: 0xcdd0d2, transparent: true, opacity: 0.77, depthWrite: false }),
	);
	screenIn.position.set(0.016, 0.012, -HALF.z + 0.015);
	screenIn.renderOrder = 1;
	group.add(screenIn);

	const screenOut = new THREE.Mesh(
		new THREE.PlaneGeometry(1.96, 1.09),
		new THREE.MeshBasicMaterial({ color: 0x8d8e91, transparent: true, opacity: 0.10, side: THREE.BackSide, depthWrite: false }),
	);
	screenOut.position.set(0.016, 0.012, -HALF.z + 0.015);
	group.add(screenOut);

	// Room edges
	const boxGeo = new THREE.BoxGeometry(w, h, d);
	const edgeGeometry = new THREE.EdgesGeometry(boxGeo);

	// Floor grid for the Logic Pro style.
	const gridPts: number[] = [];
	const gy = -HALF.y + 0.003;
	for (let i = 1; i < 4; i++) {
		const gx = -HALF.x + (w * i) / 4;
		gridPts.push(gx, gy, -HALF.z, gx, gy, HALF.z);
		const gz = -HALF.z + (d * i) / 4;
		gridPts.push(-HALF.x, gy, gz, HALF.x, gy, gz);
	}
	const outline = makeRoomOutline(edgeGeometry, gridPts, HALF);
	group.add(outline.edges, outline.grid);

	function setStyle(logic: boolean, showScreen: boolean): void {
		group.scale.set(logic ? LOGIC_HALF.x / HALF.x : 1, logic ? LOGIC_HALF.y / HALF.y : 1, 1);
		const walls = logic ? logicWallMat : wallMat;
		rear.material = front.material = ceilIn.material = walls;
		left.material = right.material = logic ? logicSideWallMat : sideWallMat;
		floor.material = logic ? logicFloorMat : floorMat;
		outline.edges.material.color.setHex(logic ? 0x687989 : 0x24252a);
		outline.edges.material.opacity = logic ? 0.75 : 1;
		outline.edges.material.linewidth = logic ? 1.25 : 1.15;
		screenIn.visible = screenOut.visible = showScreen;
	}

	return { group, outline, setStyle };
}

export function buildWalkMixDome(): THREE.Group {
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

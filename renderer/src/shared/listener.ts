import * as THREE from "three";
import { SPATIAL_TINT } from "./spatial";

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
	return pivot;
}

/** Placeholder listener: head, nose towards the front, neck and shoulders. */
export function silhouette(): THREE.Group {
	const group = new THREE.Group();
	const mat = new THREE.MeshStandardMaterial({ color: SPATIAL_TINT, roughness: 0.55, metalness: 0.05 });
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

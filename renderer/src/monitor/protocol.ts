/** State that the Qt monitor sends to the embedded Three.js scene. */

import type { ViewPreset } from "../shared/api";
import type { LayerBand } from "../shared/spatial";

export type MonitorStyle = "person" | "room" | "logic";
export type MonitorMode = "room" | "spherical";

export interface LiveObj {
	id: number;
	x: number;
	y: number;
	z: number;
	db: number;
	active: boolean;
	size?: number;
}

export interface LiveSpeaker {
	name: string;
	channel: number;
	position: [number, number, number];
	layer: LayerBand;
}

export interface MonitorOptions {
	showLabels: boolean;
	showDrop: boolean;
	showSpeakers: boolean;
}

interface LiveMonitor {
	setSpeakers(speakers: LiveSpeaker[]): void;
	updateFrame(objects: LiveObj[], levels: number[]): void;
	setView(preset: ViewPreset): void;
	setOptions(options: Partial<MonitorOptions>): void;
	setStyle(style: MonitorStyle): void;
	setSelectedObject(id: number | null): void;
	setMode(mode: MonitorMode): void;
	setLayers(upper: boolean, middle: boolean, lower: boolean): void;
}

declare global {
	interface Window {
		liveMonitor: LiveMonitor;
	}
}

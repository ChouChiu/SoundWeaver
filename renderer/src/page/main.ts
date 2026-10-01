/** Browser entry: style the stage and expose it to the CLI. */

import "@applemusic-like-lyrics/core/style.css";
import "./page.css";
import { Stage } from "./stage";

const stage = new Stage(document.getElementById("stage")!, document.getElementById("tick")!);

window.amllRender = {
	load: (ttml, options) => stage.load(ttml, options),
	settle: () => stage.settle(),
};
window.renderPage = {
	prerollStart: (ms) => stage.prerollStart(ms),
	seek: (ms) => stage.seek(ms),
	endTime: () => stage.endTime(),
};

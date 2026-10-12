/** Find or download chrome-headless-shell, and the flags that make it deterministic. */

import os from "node:os";
import path from "node:path";
import {
	Browser,
	computeExecutablePath,
	detectBrowserPlatform,
	getInstalledBrowsers,
	install,
	resolveBuildId,
} from "@puppeteer/browsers";
import { status } from "./log";

const cacheDir = path.join(os.homedir(), ".cache", "dolbytool", "chrome");

export const BROWSER_ARGS = [
	"--deterministic-mode",
	"--enable-begin-frame-control",
	"--run-all-compositor-stages-before-draw",
	"--disable-threaded-animation",
	"--disable-threaded-scrolling",
	"--disable-checker-imaging",
	"--disable-new-content-rendering-timeout",
	"--disable-image-animation-resync",
	"--allow-file-access-from-files",
	"--force-device-scale-factor=1",
	"--hide-scrollbars",
	"--font-render-hinting=none",
	// The default tile budget runs out on frames past 8K: the frame is captured
	// with whole areas never rasterised.  Raise it well above what 16K needs.
	"--force-gpu-mem-available-mb=8192",
];

export async function headlessShell(): Promise<string> {
	const platform = detectBrowserPlatform();
	if (!platform) throw new Error("unsupported platform for chrome-headless-shell");
	const browser = Browser.CHROMEHEADLESSSHELL;
	const installed = (await getInstalledBrowsers({ cacheDir })).filter(
		(b) => b.browser === browser && b.platform === platform,
	);
	if (installed.length) {
		// newest build on disk; the shell only gets updated when none is present
		const latest = installed.sort((a, b) => b.buildId.localeCompare(a.buildId, undefined, { numeric: true }))[0];
		return latest.executablePath;
	}
	const buildId = await resolveBuildId(browser, platform, "stable");
	status(`下载 chrome-headless-shell ${buildId}`);
	let last = -1;
	await install({
		browser,
		buildId,
		cacheDir,
		platform,
		downloadProgressCallback: (done, total) => {
			const percent = total ? Math.floor((done / total) * 100) : 0;
			if (percent !== last) {
				last = percent;
				status(`下载 chrome-headless-shell ${percent}%`);
			}
		},
	});
	return computeExecutablePath({ browser, buildId, cacheDir, platform });
}

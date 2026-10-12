/**
 * Lines a lyric file carries that are not lyrics: the "Title - Artist" line the
 * streaming services put first (with a note on who translated it under it),
 * the credits after it, and the copyright notice some end with.
 *
 * Only the run of such lines at the very start and at the very end goes; a
 * sung line with a colon in it further in is left alone.
 */

import type { LyricLine } from "@applemusic-like-lyrics/core";

// "作曲：", "Producer:", "词/曲：" -- a short label, then a colon
const KNOWN_LABEL =
	/^\s*(?:(?:作|填|编|谱|原)?[词曲]|词曲|编曲|制作人?|监制|出品(?:人|方)?|发行(?:方)?|混音|母带|录音|和声|配唱|人声|吉他|贝斯|鼓|键盘|弦乐|合成器|编程|企划|统筹|策划|演唱|原唱|翻唱|OP|SP|ISRC|lyrics?(?: by)?|lyricist|written by|compos(?:ed|er)(?: by)?|arrang(?:ed|er)(?: by)?|produc(?:ed|er)(?: by)?|(?:executive )?producer|mix(?:ed|ing)?(?: by| engineer)?|master(?:ed|ing)?(?: by| engineer)?|record(?:ed|ing)?(?: by| engineer)?|vocals?(?: by)?)(?:\s*[/&、]\s*\S{1,6})*\s*[:：]/i;
// any short CJK label, for credits named differently; a Latin word before a
// colon is as likely sung ("Baby: come on"), so those need a known label
const SHORT_LABEL = /^\s*[\p{Script=Han}\p{Script=Hangul}\p{Script=Hiragana}\p{Script=Katakana}/&、·]{1,8}\s*[:：]\s*\S/u;
const NOTICE = /(?:翻译|歌词|翻譯).{0,8}(?:由|贡献|提供|製作)|未经.{0,12}许可|版权所有|著作权|copyright|℗|©|all rights reserved/i;
const SEPARATOR = /\s+[-–—]\s+/;

function text(line: LyricLine): string {
	return line.words.map((w) => w.word).join("").trim();
}

function squash(value: string): string {
	return value.normalize("NFKC").toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, "");
}

/** `loose` also takes any short label with a colon, which only the opening lines may be. */
function isCredit(line: LyricLine, loose: boolean): boolean {
	const t = text(line);
	return (
		KNOWN_LABEL.test(t) || (loose && SHORT_LABEL.test(t)) || NOTICE.test(t) || NOTICE.test(line.translatedLyric)
	);
}

/** "Title - Artist" (or the other way round) naming this song. */
function isTitle(line: LyricLine, names: string[]): boolean {
	const parts = text(line).split(SEPARATOR);
	if (parts.length < 2) return false;
	const known = names.map(squash).filter(Boolean);
	return parts.some((part) => {
		const key = squash(part);
		return key.length > 0 && known.some((name) => name === key || name.includes(key) || key.includes(name));
	});
}

/**
 * `lines` without the title and credit lines around the lyrics; `names` are
 * the song's title and artists, to tell its title line from a sung one.
 */
export function withoutCredits(lines: LyricLine[], names: string[]): LyricLine[] {
	let start = 0;
	while (start < lines.length && (isCredit(lines[start], true) || (start < 2 && isTitle(lines[start], names)))) {
		start++;
	}
	let end = lines.length;
	while (end > start && isCredit(lines[end - 1], false)) end--;
	// a file that is nothing but credits is left as it is rather than emptied
	return start < end ? lines.slice(start, end) : lines;
}

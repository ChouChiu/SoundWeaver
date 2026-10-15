/**
 * Errors the Python side shows in the user's language.  The message carries
 * the text's key and its values (`#key {json}`), so it survives being thrown
 * inside the page and reported through the browser as plain text.
 */

export type ErrorValues = Record<string, string | number>;

const KEYED = /#([a-z_]+) (\{[^\n]*?\})(?=$|\n)/;

export class KeyedError extends Error {
	constructor(
		readonly key: string,
		readonly values: ErrorValues = {},
	) {
		super(`#${key} ${JSON.stringify(values)}`);
	}
}

/** The keyed error inside `message`, wherever it was wrapped, or null. */
export function findKeyed(message: string): KeyedError | null {
	const match = KEYED.exec(message);
	if (!match) return null;
	try {
		return new KeyedError(match[1], JSON.parse(match[2]) as ErrorValues);
	} catch {
		return null;
	}
}

"""Check that the three locale files agree with each other and with the code.

Every language has the same keys, each key takes the same `str.format`
fields in every language, and every literal `t("...")` key in the code
exists.  Run from the repository root: `uv run python scripts/check_locales.py`.
"""

from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "dolbytool"
LANGUAGES = ("zh_cn", "zh_tw", "en")
# t("key") and t("key", ...) with a literal key; dynamic keys are built from known prefixes
_CALL = re.compile(r"""\bt\(\s*["']([a-z0-9_.]+)["']""")


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def main() -> int:
    bundles = {
        lang: json.loads((ROOT / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        for lang in LANGUAGES
    }
    problems: list[str] = []
    reference = bundles["zh_cn"]
    for lang, bundle in bundles.items():
        for key in reference.keys() - bundle.keys():
            problems.append(f"{lang}: missing {key}")
        for key in bundle.keys() - reference.keys():
            problems.append(f"{lang}: {key} is not in zh_cn")
        for key in reference.keys() & bundle.keys():
            if fields(bundle[key]) != fields(reference[key]):
                problems.append(f"{lang}: {key} takes {fields(bundle[key])}, zh_cn {fields(reference[key])}")
    for path in ROOT.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for key in _CALL.findall(line):
                if key not in reference:
                    problems.append(f"{path.relative_to(ROOT)}:{number}: no string {key}")
    for problem in problems:
        print(problem)
    print(f"{len(reference)} keys, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

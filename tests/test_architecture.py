"""Architecture, dependency boundary, branding, and locale integrity tests."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "soundweaver"


def _imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


class ArchitectureTests(unittest.TestCase):
    def test_no_dolbytool_branding_left(self) -> None:
        targets = [SRC, ROOT / "scripts", ROOT / "renderer" / "src", ROOT / "pyproject.toml", ROOT / "README.md"]
        hits: list[str] = []
        for target in targets:
            files = [target] if target.is_file() else target.rglob("*")
            for f in files:
                if not f.is_file() or f.suffix not in {".py", ".ts", ".tsx", ".json", ".toml", ".md"}:
                    continue
                if "node_modules" in f.parts or "__pycache__" in f.parts:
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
                if "dolbytool" in text.lower():
                    hits.append(str(f.relative_to(ROOT)))
        self.assertEqual(hits, [], f"Legacy dolbytool references found: {hits}")

    def test_core_does_not_depend_on_ui_or_features(self) -> None:
        violations: list[str] = []
        for py in (SRC / "core").rglob("*.py"):
            for mod in _imported_modules(py):
                if mod.startswith(("soundweaver.ui", "soundweaver.features")):
                    violations.append(f"{py.relative_to(ROOT)} -> {mod}")
        self.assertEqual(violations, [])

    def test_no_cross_feature_imports(self) -> None:
        violations: list[str] = []
        features_dir = SRC / "features"
        for py in features_dir.rglob("*.py"):
            rel = py.relative_to(features_dir)
            own_feature = rel.parts[0]
            for mod in _imported_modules(py):
                if mod.startswith("soundweaver.features."):
                    target_feature = mod.split(".")[2]
                    if target_feature != own_feature:
                        violations.append(f"{py.relative_to(ROOT)} -> {mod}")
        self.assertEqual(violations, [])

    def test_feature_services_and_planners_are_qt_free(self) -> None:
        paths = [
            py
            for name in ("service.py", "planner.py")
            for py in (SRC / "features").rglob(name)
        ]
        violations = [
            f"{py.relative_to(ROOT)} -> {module}"
            for py in paths
            for module in _imported_modules(py)
            if module.startswith(("PySide6", "qfluentwidgets", "soundweaver.ui"))
        ]
        self.assertEqual(violations, [])

    def test_native_binaries_staged(self) -> None:
        bin_dir = SRC / "bin"
        engines = (
            "soundweaver_av3a", "soundweaver_mpegh",
            "vivid_encode", "ia_mpeghd_testbench",
        )
        present = all(
            (bin_dir / (name + (".exe" if os.name == "nt" else ""))).is_file()
            for name in engines
        )
        if not present:
            self.skipTest("native binaries not built; run scripts/build_native.py")
        for name in engines:
            p = bin_dir / (name + (".exe" if os.name == "nt" else ""))
            self.assertTrue(p.is_file(), f"Missing native binary: {p}")
            self.assertTrue(os.access(p, os.X_OK), f"Binary not executable: {p}")
        self.assertTrue((bin_dir / "model.bin").is_file())

    def test_locales_script_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_locales.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

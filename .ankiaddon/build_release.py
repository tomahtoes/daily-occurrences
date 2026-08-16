#!/usr/bin/env python3
"""Package the current project as a new .ankiaddon release.

Produces a zip in this (.ankiaddon) folder named with the current date using the
project's non-zero-padded scheme, e.g. ``2026-3-10.ankiaddon``.

The release contains every top-level ``*.py`` source file, ``config.json``,
``config.md``, ``manifest.json``, and the whole ``vendor/`` tree — the vendored
websocket client the add-on cannot import without, including its Apache-2.0
LICENSE, which has to travel with it. Everything else is excluded: tests, the log
and dictionary output under ``user_files/``, Anki's per-install ``meta.json``,
``README.md``, ``__pycache__`` (AnkiWeb rejects archives containing it), and this
script.

``manifest.json`` is bundled because installing a ``.ankiaddon`` by hand reads
the package name from it. AnkiWeb supplies its own manifest and takes the
supported-version range from fields on the upload form, so the copy here matters
only for files handed out directly.

Top-level files sit at the archive root; ``vendor/`` keeps its relative paths, so
``vendor/websocket/_app.py`` lands at exactly that path inside the zip.
"""

from __future__ import annotations

import datetime as _dt
import zipfile
from pathlib import Path

# Extra (non-.py) files to bundle alongside the Python sources.
EXTRA_FILES = ("config.json", "config.md", "manifest.json")

# Directory trees copied wholesale, preserving their relative paths.
TREES = ("vendor",)

# Never packaged, wherever they appear.
EXCLUDED_DIRS = {"__pycache__"}

HERE = Path(__file__).resolve().parent          # the .ankiaddon folder
ROOT = HERE.parent                              # the project root


def _wanted(path: Path) -> bool:
    """Whether a file inside a copied tree belongs in the release."""
    if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
        return False
    return path.suffix not in (".pyc", ".pyo", ".tmp")


def collect_files() -> list[tuple[Path, str]]:
    """Return ``(source path, archive name)`` pairs in a stable sorted order."""
    found: dict[str, Path] = {}

    for path in ROOT.glob("*.py"):
        if path.is_file():
            found[path.name] = path

    for name in EXTRA_FILES:
        path = ROOT / name
        if path.is_file():
            found[name] = path
        else:
            print(f"  warning: expected file not found, skipping: {name}")

    for tree in TREES:
        root = ROOT / tree
        if not root.is_dir():
            # Fatal rather than a warning: without vendor/ the add-on installs
            # cleanly and then dies on import, which is far worse than no build.
            raise SystemExit(f"Required directory is missing: {tree}/")
        for path in sorted(root.rglob("*")):
            if path.is_file() and _wanted(path):
                found[path.relative_to(ROOT).as_posix()] = path

    return sorted(((path, name) for name, path in found.items()), key=lambda p: p[1])


def main() -> None:
    today = _dt.date.today()
    # Non-zero-padded date scheme, e.g. 2026-3-10.ankiaddon
    out_name = f"{today.year}-{today.month}-{today.day}.ankiaddon"
    out_path = HERE / out_name

    files = collect_files()
    if not files:
        raise SystemExit("No files found to package.")

    if out_path.exists():
        print(f"  note: overwriting existing {out_name}")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname=arcname)

    print(f"Created {out_path} with {len(files)} files:")
    for _path, arcname in files:
        print(f"  {arcname}")


if __name__ == "__main__":
    main()

"""Shared by the Hoshi tests: loading the add-on's modules without Anki, and
building a synthetic Hoshi library (EPUB + the JSON files Hoshi keeps beside it).

The add-on modules use package-relative imports, so they are imported under a
stand-in parent package — the same trick ``test_recorder.py`` uses, sharing its
package name so both see one copy of each module.
"""

import importlib
import json
import logging
import os
import sys
import types
import zipfile

logging.getLogger("daily_occurrences").addHandler(logging.NullHandler())
logging.getLogger("daily_occurrences").propagate = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "daily_occurrences_under_test"


def load(name):
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [ROOT]
        sys.modules[_PKG] = package
    return importlib.import_module(f"{_PKG}.{name}")


_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def chapter_html(*paragraphs):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f'<?xml version="1.0"?><html><head><title>t</title><style>p{{x:1}}</style></head><body>{body}</body></html>'


def write_book(folder, chapters, position=0, mark_apple=None, sessions=None,
               imported_ms=0, with_epub=True):
    """Write a Hoshi book folder. ``chapters`` is a list of HTML strings; the
    chapter map is computed with the add-on's own counter, which the text tests
    pin against hand counts. Returns the book's total character count."""
    text = load("hoshi_text")
    os.makedirs(folder, exist_ok=True)
    chapter_info = {}
    total = 0
    names = []
    for index, html in enumerate(chapters):
        href = f"OEBPS/text/ch{index}.xhtml"  # Hoshi keys by full zip path
        names.append((href, html))
        count = text.count_chars(html)
        chapter_info[href] = {"spineIndex": index, "currentTotal": total, "chapterCount": count}
        total += count
    if with_epub:
        with zipfile.ZipFile(os.path.join(folder, "book.epub"), "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", _CONTAINER)
            archive.writestr("OEBPS/content.opf", "<package/>")
            for href, html in names:
                archive.writestr(href, html)
        _dump(os.path.join(folder, "bookinfo.json"),
              {"characterCount": total, "chapterInfo": chapter_info})
    metadata = {"id": "X", "title": os.path.basename(folder), "folder": os.path.basename(folder),
                "modified": imported_ms, "characterCount": total}
    if with_epub:
        metadata["epub"] = "book.epub"
    _dump(os.path.join(folder, "metadata.json"), metadata)
    set_progress(folder, position, mark_apple, sessions)
    return total


def set_progress(folder, position, mark_apple=None, sessions=None):
    bookmark = {"chapterIndex": 0, "progress": 0.0, "characterCount": position}
    if mark_apple is not None:
        bookmark["lastModified"] = mark_apple
    _dump(os.path.join(folder, "bookmark.json"), bookmark)
    if sessions is not None:
        _dump(os.path.join(folder, "statistics.json"), {
            uuid: {"modified": 0, "value": value} for uuid, value in sessions.items()
        })


def session(started_ms, chars, seconds):
    return {"startedAt": started_ms, "endedAt": started_ms + int(seconds * 1000),
            "charactersRead": chars, "readingTime": seconds}


def apple(unix_ms):
    """Unix ms -> Hoshi's bookmark ``lastModified`` (Apple-epoch seconds)."""
    return unix_ms / 1000.0 - 978307200


def _dump(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)

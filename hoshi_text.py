"""Text extraction from the EPUBs Hoshi Reader keeps on disk.

Hoshi measures every reading position as a count of *counted* characters from the
start of the book: chapter HTML is stripped to text, everything outside a fixed
character class is dropped, and what remains is counted. ``bookinfo.json`` stores
each spine item's running total and own count under that rule. To turn a pair of
positions back into the text that was read, this module reproduces the rule
exactly — the strip below mirrors Hoshi's ``library.rs`` step for step, down to
*removing* numeric entities and decoding only four named ones — and walks the raw
text counting as it goes.

The one deliberate difference is that block-closing tags become newlines instead
of vanishing, so paragraphs stay separate lines. A newline is outside the counted
class, so counts are unaffected.

Hoshi counts extended grapheme clusters; this counts code points. Every combining
mark and variation selector is outside the class (it is filtered before counting),
so the two agree on real text — verified exactly against Hoshi's own counts for
whole novels.

No Anki imports, so this is testable on its own.
"""

import json
import os
import posixpath
import re
import zipfile

# Unified_Ideograph and Radical, which Python's re can't express as properties.
_IDEOGRAPHS = (
    "㐀-䶿一-鿿"
    "﨎﨏﨑﨓﨔﨟﨡﨣﨤﨧-﨩"
    "\U00020000-\U0002a6df\U0002a700-\U0002b739\U0002b740-\U0002b81d"
    "\U0002b820-\U0002cea1\U0002ceb0-\U0002ebe0\U0002ebf0-\U0002ee5d"
    "\U00030000-\U0003134a\U00031350-\U000323af"
)
_RADICALS = "⺀-⺙⺛-⻳⼀-⿕"
_CLASS = "0-9A-Za-z○◯々-〇〻ぁ-ゖゝ-ゞァ-ヺー０-９Ａ-Ｚａ-ｚｦ-ﾝ가-힣ㄱ-ㆎ" + _RADICALS + _IDEOGRAPHS

_COUNTED = re.compile(f"[{_CLASS}]")
_UNCOUNTED = re.compile(f"[^{_CLASS}]")

_BODY = re.compile(r"<body.*?</body>", re.S)
_RT = re.compile(r"<rt[^>]*>.*?</rt>", re.S)
_RP = re.compile(r"<rp[^>]*>.*?</rp>", re.S)
_SCRIPT_STYLE = re.compile(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", re.S)
_BLOCK_END = re.compile(r"</(?:p|div|h[1-6]|li|blockquote|section|tr)\s*>|<br\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_NUMERIC_ENTITY = re.compile(r"&#[xX]?[0-9A-Fa-f]+;")

# Sentence enders, and the closers that belong to the sentence they end
# (「……。」 stays one line).
_ENDERS = frozenset("。！？!?")
_CLOSERS = frozenset("」』）)】〉》〕］]\"'”’")
_WRAP_AT = 300  # a line longer than this is wrapped, preferably after 、


def strip_html(html, keep_breaks=False):
    """Hoshi's strip: body only, ruby annotations and script/style removed, tags
    removed, numeric entities removed (not decoded), four named entities decoded.
    ``keep_breaks`` turns block-closing tags into newlines."""
    match = _BODY.search(html)
    text = match.group(0) if match else html
    for pattern in (_RT, _RP, _SCRIPT_STYLE):
        text = pattern.sub("", text)
    if keep_breaks:
        text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = _NUMERIC_ENTITY.sub("", text)
    return (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def count_chars(html):
    """The number of characters Hoshi counts in one chapter's HTML."""
    return len(_UNCOUNTED.sub("", strip_html(html)))


def is_counted(char):
    return _COUNTED.match(char) is not None


def split_lines(text, offset):
    """Split raw ``text`` into reader lines, each tagged with the counted-character
    span it covers: ``[(start, end, line), ...]`` where ``start``/``end`` are book
    positions and ``offset`` is the position of the first character of ``text``.

    Lines break at newlines and after sentence enders (with any closing brackets
    that follow). A line with no counted characters (pure punctuation) is dropped;
    spans of the kept lines still tile the input, because a dropped line covers
    zero counted characters."""
    out = []
    buf = []
    start = pos = offset

    def emit():
        nonlocal start
        line = "".join(buf).strip()
        buf.clear()
        if pos > start and line:
            out.extend(_wrap(line, start))
        start = pos

    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in "\r\n":
            emit()
            i += 1
            continue
        buf.append(char)
        if is_counted(char):
            pos += 1
        i += 1
        if char in _ENDERS:
            while i < n and (text[i] in _ENDERS or text[i] in _CLOSERS):
                buf.append(text[i])
                i += 1
            emit()
    emit()
    return out


def _wrap(line, start):
    """Break an over-long line, after the last 、 before the limit when there is
    one. Spans stay exact: each piece's end is its start plus its counted chars."""
    pieces = []
    while len(line) > _WRAP_AT:
        cut = line.rfind("、", 0, _WRAP_AT)
        cut = cut + 1 if cut > 0 else _WRAP_AT
        head, line = line[:cut], line[cut:]
        counted = sum(1 for c in head if is_counted(c))
        if counted:
            pieces.append((start, start + counted, head))
        start += counted
    counted = sum(1 for c in line if is_counted(c))
    if counted:
        pieces.append((start, start + counted, line))
    return pieces


def counted_offsets(text):
    """The index in ``text`` of each counted character, in order."""
    return [match.start() for match in _COUNTED.finditer(text)]


def _cut(text, offsets, a, b):
    """The raw substring of ``text`` holding counted characters ``[a, b)``, given
    ``offsets`` from ``counted_offsets(text)``. Uncounted characters before the
    first counted one (an opening 「) belong to the start of the text; those
    between counted ones belong to the earlier side, so consecutive cuts tile the
    text with nothing lost or repeated."""
    if a >= len(offsets):
        return ""
    lo = 0 if a == 0 else offsets[a]
    hi = offsets[b] if b < len(offsets) else len(text)
    return text[lo:hi]


class Book:
    """A downloaded Hoshi book: its EPUB plus the chapter map from bookinfo.json.

    Chapter text is stripped once and cached, since the same chapter is sliced
    repeatedly while a backlog drains."""

    def __init__(self, epub_path, bookinfo):
        self.epub_path = epub_path
        self.total = int(bookinfo.get("characterCount") or 0)
        chapters = []
        for href, info in (bookinfo.get("chapterInfo") or {}).items():
            count = int(info.get("chapterCount") or 0)
            if count > 0:
                chapters.append((int(info.get("currentTotal") or 0), count, href))
        self.chapters = sorted(chapters)
        self._texts = {}

    @classmethod
    def open(cls, folder):
        """The book in a Hoshi ``Books/<folder>`` directory, or None while it is
        remote-only (synced from another device and not yet downloaded)."""
        bookinfo_path = os.path.join(folder, "bookinfo.json")
        try:
            with open(os.path.join(folder, "metadata.json"), encoding="utf-8") as handle:
                epub_name = json.load(handle).get("epub") or ""
            with open(bookinfo_path, encoding="utf-8") as handle:
                bookinfo = json.load(handle)
        except (OSError, ValueError):
            return None
        epub_path = os.path.join(folder, epub_name) if epub_name else ""
        if not epub_name or not os.path.isfile(epub_path):
            return None
        return cls(epub_path, bookinfo)

    def _chapter(self, href):
        """A chapter's stripped text and the offsets of its counted characters."""
        cached = self._texts.get(href)
        if cached is None:
            with zipfile.ZipFile(self.epub_path) as archive:
                html = _read_member(archive, href)
            text = strip_html(html, keep_breaks=True)
            cached = self._texts[href] = (text, counted_offsets(text))
        return cached

    def slice(self, start, end):
        """The lines covering book positions ``[start, end)``, as
        ``[(start, end, line), ...]``."""
        out = []
        for chapter_start, count, href in self.chapters:
            lo = max(start, chapter_start)
            hi = min(end, chapter_start + count)
            if lo >= hi:
                continue
            text, offsets = self._chapter(href)
            out.extend(split_lines(_cut(text, offsets, lo - chapter_start, hi - chapter_start), lo))
        return out

    def mismatches(self):
        """Chapters whose count here differs from Hoshi's, as
        ``[(href, hoshi, ours), ...]``. Empty means positions map exactly."""
        bad = []
        with zipfile.ZipFile(self.epub_path) as archive:
            for _start, count, href in self.chapters:
                ours = count_chars(_read_member(archive, href))
                if ours != count:
                    bad.append((href, count, ours))
        return bad


def _read_member(archive, href):
    """A chapter's HTML. Hoshi keys chapters by their full path in the zip; joining
    to the OPF directory is a fallback for any book where that differs."""
    names = archive.namelist()
    if href not in names:
        try:
            container = archive.read("META-INF/container.xml").decode("utf-8", "replace")
            opf = re.search(r'full-path="([^"]+)"', container).group(1)
            href = posixpath.join(posixpath.dirname(opf), href)
        except (KeyError, AttributeError):
            pass
    return archive.read(href).decode("utf-8")

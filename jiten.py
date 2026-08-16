"""Word extraction backed by the Jiten reader API (https://api.jiten.moe).

A batch of text lines is POSTed to ``/api/reader/parse`` (authenticated with an
``X-Api-Key`` header); the server returns segmented tokens plus a vocabulary list
that already carries the resolved spelling and reading for each word. We map that
back to ``(headword, reading)`` pairs per line, dropping function words and
punctuation. Uses only the standard library for HTTP.
"""

import json
import urllib.error
import urllib.request

# --- JMdict part-of-speech filtering ---------------------------------------
#
# Each word the API returns lists short JMdict POS tags (most relevant sense
# first). We map the leading recognized tag to a coarse category and use it to
# decide whether the word is worth recording.

# Categories that are *not* recorded as vocabulary.
_DROPPED = frozenset(
    ("Particle", "Auxiliary", "Conjunction", "Symbol", "BlankSpace", "Numeral")
)

# JMdict name/proper-noun tags that all collapse to a single category.
_NAME_TAGS = frozenset(
    (
        "company", "given", "place", "person", "product", "ship", "surname",
        "unclass", "name-fem", "name-masc", "name-male", "station", "group",
        "char", "creat", "dei", "doc", "ev", "fict", "leg", "myth", "obj",
        "organization", "oth", "relig", "serv", "work", "unc",
    )
)

_TAG_TO_POS = {
    "n": "Noun", "n-adv": "Noun", "n-t": "Noun", "n-pr": "Noun",
    "adj-i": "IAdjective", "adj-ix": "IAdjective",
    "adj-na": "NaAdjective",
    "adj-no": "NominalAdjective", "adj-t": "NominalAdjective", "adj-f": "NominalAdjective",
    "adj-pn": "PrenounAdjectival",
    "adv": "Adverb", "adv-to": "AdverbTo",
    "prt": "Particle", "conj": "Conjunction",
    "aux": "Auxiliary", "aux-v": "Auxiliary", "aux-adj": "Auxiliary", "cop": "Auxiliary",
    "int": "Interjection",
    "pref": "Prefix", "n-pref": "Prefix",
    "suf": "Suffix", "n-suf": "NounSuffix",
    "pn": "Pronoun", "exp": "Expression",
    "num": "Numeral", "ctr": "Counter",
}


def _from_jmdict(tag):
    """Map one JMdict POS tag to a coarse category, or None if unrecognized."""
    # Any verb tag starts with 'v' (apart from a few non-verb exceptions).
    if tag.startswith("v") and tag not in ("vulg", "vet", "vidg"):
        return "Verb"
    if tag.startswith("name-") or tag in _NAME_TAGS:
        return "Name"
    return _TAG_TO_POS.get(tag)


def _primary_pos(tags):
    """The first recognized category among a JMdict POS tag list."""
    for tag in tags:
        pos = _from_jmdict(tag)
        if pos is not None:
            return pos
    return None


def is_recordable(tags):
    """Whether a word with these JMdict POS tags is worth recording. Drops
    particles, auxiliaries/copulas, conjunctions, symbols, and bare numerals;
    unknown/empty tag lists are kept (better to record than silently drop)."""
    return _primary_pos(tags) not in _DROPPED


# --- furigana stripping -----------------------------------------------------


def _is_kanji(char):
    """Kanji plus the iteration/abbreviation marks that can head a reading run."""
    code = ord(char)
    return (
        code in (0x3005, 0x3006, 0x30F6)
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2EBEF
    )


def strip_furigana(text):
    """Turn a furigana-annotated reading (e.g. ``寝[しん]台[だい]車[しゃ]``,
    ``お腹[なか]``) into plain kana (``しんだいしゃ``, ``おなか``). The kanji run
    immediately before each ``[..]`` group is replaced by the bracket contents;
    surrounding kana and bracket-free words (``ベッド``) are kept."""
    out = []
    chars = iter(text)
    for char in chars:
        if char == "[":
            # Drop the kanji run this bracket annotates...
            while out and _is_kanji(out[-1]):
                out.pop()
            # ...and emit the bracketed reading in its place.
            for inner in chars:
                if inner == "]":
                    break
                out.append(inner)
        elif char != "]":
            out.append(char)
    return "".join(out)


# --- API client -------------------------------------------------------------


class JitenError(Exception):
    """Raised when the reader API returns a non-success response."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    def user_message(self):
        """A short, non-technical version for the summary window. The full
        message — including the response body — still goes to the log."""
        if self.status in (401, 403):
            return "Jiten rejected your API key — check jiten_api_key in the config."
        if self.status == 429:
            return "Jiten is rate-limiting this add-on; recording will catch up."
        if self.status is not None:
            return f"The Jiten API returned an error (HTTP {self.status})."
        return "The Jiten API returned an error."


class JitenClient:
    """Calls the reader-parse endpoint and maps responses to per-line words."""

    def __init__(self, base_url, api_key, timeout_ms):
        self.url = base_url.rstrip("/") + "/api/reader/parse"
        self.api_key = api_key
        self.timeout = max(timeout_ms, 1) / 1000.0

    def parse_lines(self, lines):
        """Send a batch of lines and return, per line, a list of
        ``(headword, reading)`` recordable words. Raises on transport or HTTP
        errors so the caller can decide how to handle an outage."""
        if not lines:
            return []

        body = json.dumps({"text": lines}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            snippet = err.read().decode("utf-8", "replace")[:200]
            raise JitenError(
                f"reader/parse returned HTTP {err.code}: {snippet}", status=err.code
            ) from err

        return self._map(payload)

    @staticmethod
    def _map(payload):
        # (wordId, readingIndex) -> vocab entry carrying spelling/reading/POS.
        vocab = {}
        for entry in payload.get("vocabulary", []):
            vocab[(entry.get("wordId"), entry.get("readingIndex"))] = entry

        out = []
        for line_tokens in payload.get("tokens", []):
            words = []
            for token in line_tokens:
                word_id = token.get("wordId")
                if word_id == 0:  # punctuation / gap
                    continue
                entry = vocab.get((word_id, token.get("readingIndex")))
                if entry is None:
                    continue
                spelling = entry.get("spelling") or ""
                if not spelling:
                    continue
                if not is_recordable(entry.get("meaningsPartOfSpeech") or []):
                    continue
                words.append((spelling, strip_furigana(entry.get("reading") or "")))
            out.append(words)
        return out

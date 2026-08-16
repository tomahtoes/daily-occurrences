import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiten import JitenClient, JitenError, is_recordable, strip_furigana


class Recordable(unittest.TestCase):
    def test_drops_function_words_and_numerals(self):
        self.assertFalse(is_recordable(["prt"]))            # は
        self.assertFalse(is_recordable(["prt", "conj"]))    # が
        self.assertFalse(is_recordable(["prt", "prt", "fem"]))  # の
        self.assertFalse(is_recordable(["aux-v", "cop"]))   # だ
        self.assertFalse(is_recordable(["prt", "int"]))     # ね
        self.assertFalse(is_recordable(["num"]))            # 一つ

    def test_keeps_content_words(self):
        self.assertTrue(is_recordable(["n"]))               # 寝台車
        self.assertTrue(is_recordable(["v5u"]))             # 向かい合う
        self.assertTrue(is_recordable(["adj-na", "adj-na", "uk"]))  # 綺麗
        self.assertTrue(is_recordable(["suf", "n"]))        # 屋
        self.assertTrue(is_recordable([]))                  # unknown → keep


class StripFurigana(unittest.TestCase):
    def test_strips_per_kanji_furigana(self):
        self.assertEqual(strip_furigana("寝[しん]台[だい]車[しゃ]"), "しんだいしゃ")

    def test_keeps_leading_and_trailing_okurigana(self):
        self.assertEqual(strip_furigana("お腹[なか]"), "おなか")
        self.assertEqual(strip_furigana("流[なが]れる"), "ながれる")

    def test_passes_through_bracket_free_readings(self):
        self.assertEqual(strip_furigana("ベッド"), "ベッド")
        self.assertEqual(strip_furigana("には"), "には")


def _vocab(word_id, reading_index, spelling, reading, pos):
    return {
        "wordId": word_id,
        "readingIndex": reading_index,
        "spelling": spelling,
        "reading": reading,
        "meaningsPartOfSpeech": pos,
    }


class MapPayload(unittest.TestCase):
    """The response mapping — the only code here coupled to a third-party
    schema, and the one place where a shape change produces zero words per line
    with nothing raised."""

    PAYLOAD = {
        "vocabulary": [
            _vocab(11, 0, "猫", "猫[ねこ]", ["n"]),
            _vocab(12, 0, "は", "は", ["prt"]),
            _vocab(13, 0, "寝る", "寝[ね]る", ["v1", "vi"]),
            _vocab(14, 1, "本", "本[ほん]", ["n"]),
        ],
        "tokens": [
            [
                {"wordId": 11, "readingIndex": 0},
                {"wordId": 12, "readingIndex": 0},   # particle, dropped
                {"wordId": 13, "readingIndex": 0},
                {"wordId": 0, "readingIndex": 0},    # punctuation
            ],
            [
                {"wordId": 14, "readingIndex": 1},
                {"wordId": 99, "readingIndex": 0},   # no vocabulary entry
            ],
        ],
    }

    def test_maps_lines_to_recordable_words(self):
        self.assertEqual(
            JitenClient._map(self.PAYLOAD),
            [[("猫", "ねこ"), ("寝る", "ねる")], [("本", "ほん")]],
        )

    def test_reading_index_is_part_of_the_key(self):
        # Same wordId, different readingIndex: the token must not match.
        payload = dict(self.PAYLOAD, tokens=[[{"wordId": 14, "readingIndex": 0}]])
        self.assertEqual(JitenClient._map(payload), [[]])

    def test_one_entry_per_line_even_when_empty(self):
        # The caller counts lines, so a line that yielded nothing still needs a
        # slot rather than being dropped from the list.
        payload = dict(self.PAYLOAD, tokens=[[{"wordId": 12, "readingIndex": 0}], []])
        self.assertEqual(JitenClient._map(payload), [[], []])

    def test_unknown_shape_yields_nothing_rather_than_raising(self):
        self.assertEqual(JitenClient._map({}), [])
        self.assertEqual(JitenClient._map({"tokens": [], "vocabulary": []}), [])

    def test_skips_entries_with_no_spelling(self):
        payload = {
            "vocabulary": [_vocab(11, 0, "", "ねこ", ["n"])],
            "tokens": [[{"wordId": 11, "readingIndex": 0}]],
        }
        self.assertEqual(JitenClient._map(payload), [[]])


class ErrorMessages(unittest.TestCase):
    """The summary window shows ``user_message``; the full text, including the
    response body, only ever goes to the log."""

    def test_auth_failures_name_the_key(self):
        for status in (401, 403):
            self.assertIn("API key", JitenError("HTTP boom", status=status).user_message())

    def test_other_statuses_are_generic_but_specific(self):
        self.assertIn("500", JitenError("boom", status=500).user_message())
        self.assertIn("rate-limit", JitenError("boom", status=429).user_message())

    def test_status_is_optional(self):
        self.assertEqual(
            JitenError("boom").user_message(), "The Jiten API returned an error."
        )


if __name__ == "__main__":
    unittest.main()

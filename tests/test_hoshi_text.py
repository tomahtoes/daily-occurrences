"""Text extraction: Hoshi's counting rule reproduced exactly, and position spans
that tile a book with nothing lost or repeated."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hoshi_helpers import chapter_html, load, write_book

text = load("hoshi_text")


class Counting(unittest.TestCase):
    """Hand counts of Hoshi's rule. A wrong count shifts every position after it,
    so the slice cut out of a book would be the wrong text."""

    def test_counts_only_the_character_class(self):
        # 吾輩は猫である = 7; the 。, 「」 and spaces don't count.
        self.assertEqual(text.count_chars("<body>「吾輩は 猫である。」</body>"), 7)

    def test_ruby_annotations_are_not_counted(self):
        html = "<body><ruby>漢<rp>(</rp><rt>かん</rt><rp>)</rp>字<rt>じ</rt></ruby></body>"
        self.assertEqual(text.count_chars(html), 2)

    def test_script_style_and_head_are_not_counted(self):
        html = ("<html><head><title>題名</title></head><body><style>本</style>"
                "<script>本</script>本</body></html>")
        self.assertEqual(text.count_chars(html), 1)

    def test_numeric_entities_are_removed_not_decoded(self):
        # &#12354; is あ, but Hoshi deletes numeric entities before counting.
        self.assertEqual(text.count_chars("<body>&#12354;&#x3042;い</body>"), 1)

    def test_unknown_named_entities_leave_their_letters(self):
        # Only &nbsp; &amp; &lt; &gt; are decoded. Anything else keeps its name,
        # whose ASCII letters then count — a Hoshi quirk positions depend on.
        self.assertEqual(text.count_chars("<body>&hellip;</body>"), 6)
        self.assertEqual(text.count_chars("<body>&amp;&nbsp;</body>"), 0)

    def test_counted_scripts(self):
        sample = "ａＡ９ｶﾅカナかな々〆〇ー한ㄱ⺀⼀𠀀"
        self.assertEqual(text.count_chars(f"<body>{sample}</body>"), len(sample))

    def test_block_breaks_do_not_change_counts(self):
        html = "<body><p>一文目。</p><div>二文目</div><br/>三</body>"
        stripped = text.strip_html(html, keep_breaks=True)
        self.assertIn("\n", stripped)
        self.assertEqual(sum(1 for c in stripped if text.is_counted(c)), text.count_chars(html))


class Lines(unittest.TestCase):
    def test_lines_break_after_sentences_keeping_closers(self):
        lines = text.split_lines("「行こう。」と言った。\n次", 100)
        self.assertEqual([l for *_s, l in lines], ["「行こう。」", "と言った。", "次"])
        self.assertEqual([(s, e) for s, e, _l in lines], [(100, 103), (103, 107), (107, 108)])

    def test_punctuation_only_lines_are_dropped_but_spans_still_tile(self):
        lines = text.split_lines("一。\n……\n二。", 0)
        self.assertEqual([l for *_s, l in lines], ["一。", "二。"])
        self.assertEqual(lines[0][1], lines[1][0])

    def test_long_lines_wrap_after_a_comma(self):
        long = "あ" * 250 + "、" + "い" * 100
        lines = text.split_lines(long, 0)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0][2].endswith("、"))
        self.assertEqual(lines[-1][1], 350)
        self.assertEqual(lines[0][1], lines[1][0])


class Slicing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hoshi-text-")
        self.folder = os.path.join(self.tmp, "本")
        self.total = write_book(self.folder, [
            chapter_html("一章の一文目。", "<ruby>二<rt>に</rt></ruby>文目だ。"),
            "<body></body>",  # an empty spine item (cover page) has no count
            chapter_html("二章です。", "最後の文。"),
        ])
        self.book = text.Book.open(self.folder)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_whole_book_tiles_exactly(self):
        lines = self.book.slice(0, self.total)
        self.assertEqual(lines[0][0], 0)
        self.assertEqual(lines[-1][1], self.total)
        for a, b in zip(lines, lines[1:]):
            self.assertEqual(a[1], b[0])
        self.assertEqual([l for *_s, l in lines],
                         ["一章の一文目。", "二文目だ。", "二章です。", "最後の文。"])

    def test_any_split_point_loses_and_repeats_nothing(self):
        whole = "".join(l for *_s, l in self.book.slice(0, self.total))
        for cut in range(1, self.total):
            joined = "".join(l for *_s, l in self.book.slice(0, cut) + self.book.slice(cut, self.total))
            self.assertEqual(joined, whole, cut)

    def test_a_slice_across_chapters(self):
        # 一章の一文目 (6) + 二文目だ (4) = 10 in chapter one; 二章 starts at 10.
        lines = self.book.slice(8, 12)
        self.assertEqual([l for *_s, l in lines], ["目だ。", "二章"])
        self.assertEqual((lines[0][0], lines[-1][1]), (8, 12))

    def test_counts_match_bookinfo(self):
        self.assertEqual(self.book.mismatches(), [])

    def test_remote_only_book_does_not_open(self):
        remote = os.path.join(self.tmp, "remote")
        write_book(remote, [chapter_html("本文。")], with_epub=False)
        self.assertIsNone(text.Book.open(remote))


if __name__ == "__main__":
    unittest.main()

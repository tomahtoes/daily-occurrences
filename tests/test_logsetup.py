"""The log must never block Anki from updating or deleting the add-on.

Anki removes the add-on's folder while it is running, and Windows refuses to
delete a file any process still holds open. A handler that kept the log open for
the session made removal fail outright:

    [WinError 32] The process cannot access the file because it is being used by
    another process: ...\\user_files\\daily-occurrences.log
"""

import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logsetup import ReleasingRotatingFileHandler


class HoldsNoHandle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")
        self.path = os.path.join(self.tmp, "daily-occurrences.log")
        self.log = logging.getLogger(f"test_logsetup_{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.handler = ReleasingRotatingFileHandler(
            self.path, maxBytes=256, backupCount=1, encoding="utf-8", delay=True
        )
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.handler.close()
        self.log.removeHandler(self.handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nothing_is_opened_before_the_first_record(self):
        self.assertFalse(os.path.exists(self.path))

    def test_the_log_can_be_deleted_while_the_handler_is_live(self):
        self.log.info("started; reading ws://localhost:6677")
        self.assertTrue(os.path.isfile(self.path))
        os.remove(self.path)  # this is what raised WinError 32

    def test_the_addon_folder_can_be_removed_while_the_handler_is_live(self):
        # What Anki actually does when updating or deleting an add-on.
        self.log.info("started")
        shutil.rmtree(self.tmp)
        self.assertFalse(os.path.exists(self.tmp))

    def test_logging_still_works_after_a_release(self):
        # Releasing must not retire the handler: close() would have.
        for i in range(3):
            self.log.info("record %d", i)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().strip().splitlines()), 3)

    def test_logging_recovers_if_the_file_is_deleted_underneath_it(self):
        self.log.info("before")
        os.remove(self.path)
        self.log.info("after")
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("after", handle.read())

    def test_rotation_still_happens(self):
        for i in range(40):
            self.log.info("padding record %d with enough text to roll over", i)
        self.assertTrue(os.path.isfile(self.path))
        self.assertTrue(os.path.isfile(self.path + ".1"))
        # Rotation must not leave a handle behind either.
        shutil.rmtree(self.tmp)


if __name__ == "__main__":
    unittest.main()

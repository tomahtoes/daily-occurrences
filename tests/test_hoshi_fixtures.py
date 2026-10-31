"""The shared acceptance cases from the hoshi-daily-occurrences repo, run against
real Hoshi files: the same cases every implementation of the source must pass, so they
can't drift apart.

The fixtures live in that sibling repo (``../hoshi-daily-occurrences/fixtures``),
or wherever ``HOSHI_FIXTURES`` points.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hoshi_helpers import ROOT, load

policy = load("hoshi_policy")
source = load("hoshi_source")
textutil = load("textutil")

FIXTURES = os.environ.get("HOSHI_FIXTURES") or os.path.join(
    os.path.dirname(ROOT), "hoshi-daily-occurrences", "fixtures"
)
CASES = os.path.join(FIXTURES, "cases.json")


@unittest.skipUnless(os.path.isfile(CASES), f"shared fixtures not found at {FIXTURES}")
class SharedCases(unittest.TestCase):
    def test_cases(self):
        with open(CASES, encoding="utf-8") as handle:
            spec = json.load(handle)
        offset = timezone(timedelta(minutes=spec["utc_offset_minutes"]))
        cutoff = spec["cutoff_hour"]

        def day_of(ms):
            local = datetime.fromtimestamp(ms / 1000, offset).replace(tzinfo=None)
            return textutil.day_key(local, cutoff)

        for case in spec["cases"]:
            state = policy.State(installed_at=case["installed_at"])
            for number, step in enumerate(case["steps"], start=1):
                with self.subTest(case=case["name"], step=number):
                    folder = os.path.join(FIXTURES, "snapshots", step["snapshot"], step["book"])
                    observation = source.read_observation(folder, step["book"])
                    self.assertIsNotNone(observation)
                    ranges = policy.plan(state, observation, day_of, spec["max_cpm"],
                                         case["keep_days"], case["today"])
                    got = [{"day": r.day, "start": r.start, "end": r.end} for r in ranges]
                    self.assertEqual(got, step["expect"])


if __name__ == "__main__":
    unittest.main()

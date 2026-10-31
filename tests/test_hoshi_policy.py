"""The crediting policy: what counts as read, on which day, and that nothing is
ever credited twice."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hoshi_helpers import load

policy = load("hoshi_policy")
textutil = load("textutil")

DAY_MS = 86_400_000
T0 = 1_800_000_000_000  # 2027-01-15 08:00 UTC — an arbitrary "now" well after install
INSTALLED = T0 - 30 * DAY_MS
CPM = 1000


def day_of(ms):
    return textutil.day_key(datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None), 4)


def obs(position, mark_ms=T0, total=100_000, sessions=None, imported_ms=0, folder="本"):
    return policy.Observation(folder, total, position, mark_ms, imported_ms, sessions or {})


def s(started_ms, chars, seconds):
    return policy.Session(started_ms, chars, seconds)


def spans(ranges):
    return [(r.day, r.start, r.end) for r in ranges]


class Base(unittest.TestCase):
    def setUp(self):
        self.state = policy.State(installed_at=INSTALLED)

    def plan(self, observation, keep_days=0, today=None):
        return policy.plan(self.state, observation, day_of, CPM, keep_days,
                           today or day_of(T0))

    def baseline(self, position, sessions=None, mark_ms=T0 - DAY_MS):
        """A book first seen at ``position`` after install, and already tracked."""
        self.state.books["本"] = policy.BookState(100_000, position, mark_ms)
        for uuid, session in (sessions or {}).items():
            self.state.books["本"].seen[uuid] = [session.chars, session.seconds]


class FirstSight(Base):
    def test_a_book_read_before_install_is_only_baselined(self):
        self.assertEqual(self.plan(obs(5000, mark_ms=INSTALLED - DAY_MS)), [])
        self.assertEqual(self.state.books["本"].high_water, 5000)

    def test_a_book_imported_after_install_counts_from_zero(self):
        ranges = self.plan(obs(300, imported_ms=INSTALLED + 1))
        self.assertEqual(spans(ranges), [(day_of(T0), 0, 300)])

    def test_a_synced_book_with_sessions_from_before_install_is_baselined(self):
        early = {"a": s(INSTALLED - DAY_MS, 1000, 600)}
        self.assertEqual(self.plan(obs(5000, sessions=early)), [])
        # Its old session is accounted at the baseline, so only growth after
        # this point can back later reading.
        self.assertEqual(self.state.books["本"].seen, {"a": [1000, 600]})

    def test_a_synced_book_read_entirely_after_install_counts_from_zero(self):
        later = {"a": s(INSTALLED + DAY_MS, 400, 120)}
        self.assertEqual(spans(self.plan(obs(400, sessions=later))),
                         [(day_of(INSTALLED + DAY_MS), 0, 400)])


class Crediting(Base):
    def test_going_back_credits_nothing(self):
        self.baseline(5000)
        self.assertEqual(self.plan(obs(4000)), [])
        self.assertEqual(self.state.books["本"].high_water, 5000)
        self.assertEqual(self.state.books["本"].last_mark_ms, T0)

    def test_rereading_up_to_the_high_water_credits_nothing(self):
        self.baseline(5000)
        self.plan(obs(4000))
        self.assertEqual(self.plan(obs(5000)), [])
        self.assertEqual(spans(self.plan(obs(5200))), [(day_of(T0), 5000, 5200)])

    def test_reading_backed_by_session_time_is_credited(self):
        self.baseline(1000)
        ranges = self.plan(obs(1500, sessions={"a": s(T0 - 60_000, 500, 60)}))
        self.assertEqual(spans(ranges), [(day_of(T0 - 60_000), 1000, 1500)])

    def test_a_toc_jump_with_almost_no_time_credits_almost_nothing(self):
        self.baseline(1000)
        ranges = self.plan(obs(6000, sessions={"a": s(T0, 5000, 3)}))
        self.assertEqual(spans(ranges), [(day_of(T0), 5950, 6000)])  # 3 s at 1000 cpm

    def test_a_jump_then_reading_credits_the_tail_sized_by_time(self):
        self.baseline(1000)
        ranges = self.plan(obs(9000, sessions={"a": s(T0, 8000, 120)}))
        self.assertEqual(spans(ranges), [(day_of(T0), 7000, 9000)])

    def test_sessions_on_two_days_split_the_credit_by_day(self):
        self.baseline(1000)
        day1, day2 = T0 - 2 * DAY_MS, T0 - DAY_MS
        sessions = {"a": s(day1, 1000, 1200), "b": s(day2, 500, 600)}
        ranges = self.plan(obs(2500, sessions=sessions))
        self.assertEqual(spans(ranges), [(day_of(day1), 1000, 2000), (day_of(day2), 2000, 2500)])

    def test_stats_off_with_frequent_polls_is_credited_in_full(self):
        self.baseline(0)
        credited = 0
        for step in range(1, 6):
            ranges = self.plan(obs(step * 300, mark_ms=T0 + step * 60_000))
            credited += sum(r.end - r.start for r in ranges)
        self.assertEqual(credited, 1500)

    def test_a_long_stats_off_gap_is_credited_in_full_to_the_last_page_turn(self):
        self.baseline(0, mark_ms=T0 - 5 * DAY_MS)
        ranges = self.plan(obs(30_000, mark_ms=T0))
        self.assertEqual(spans(ranges), [(day_of(T0), 0, 30_000)])

    def test_mixed_tracked_and_untracked_reading_both_count(self):
        self.baseline(0)
        ranges = self.plan(obs(5000, sessions={"a": s(T0 - DAY_MS, 1000, 600)}))
        self.assertEqual(spans(ranges), [(day_of(T0), 0, 4000), (day_of(T0 - DAY_MS), 4000, 5000)])

    def test_a_growing_session_is_budgeted_only_on_its_growth(self):
        self.baseline(0)
        self.assertEqual(sum(r.end - r.start for r in self.plan(
            obs(500, sessions={"a": s(T0, 500, 60)}))), 500)
        # Same session, now a jump: +4900 chars in 2 more seconds.
        ranges = self.plan(obs(5400, sessions={"a": s(T0, 5400, 62)}))
        self.assertEqual(sum(r.end - r.start for r in ranges), 33)

    def test_a_session_whose_count_dips_cannot_recredit(self):
        self.baseline(0)
        self.plan(obs(1000, sessions={"a": s(T0, 1000, 120)}))
        self.plan(obs(1000, sessions={"a": s(T0, 600, 130)}))  # backed up 400
        # Back to 1000 then on to 1100: only the last 100 is new reading.
        ranges = self.plan(obs(1100, sessions={"a": s(T0, 1100, 150)}))
        self.assertEqual(spans(ranges), [(day_of(T0), 1000, 1100)])

    def test_position_is_clamped_to_the_book_length(self):
        self.baseline(99_900)
        ranges = self.plan(obs(100_500))
        self.assertEqual(spans(ranges), [(day_of(T0), 99_900, 100_000)])


class NoDoubleCounting(Base):
    def test_replaying_an_observation_credits_nothing(self):
        self.baseline(0)
        first = obs(2000, sessions={"a": s(T0, 2000, 300)})
        self.assertTrue(self.plan(first))
        self.assertEqual(self.plan(first), [])

    def test_sessions_syncing_after_the_gap_was_credited_add_nothing(self):
        self.baseline(0)
        self.assertEqual(spans(self.plan(obs(1000))), [(day_of(T0), 0, 1000)])
        late = {"phone": s(T0 - 3_600_000, 1000, 600)}
        self.assertEqual(self.plan(obs(1000, sessions=late)), [])
        # ...and they can't fund later movement either.
        ranges = self.plan(obs(1300, sessions=late, mark_ms=T0 + 60_000))
        self.assertEqual(spans(ranges), [(day_of(T0 + 60_000), 1000, 1300)])

    def test_reimporting_the_same_book_keeps_its_high_water(self):
        # Hoshi gives a re-import a new id and resets the bookmark; identity is
        # (folder, length), so reading back up to the old position re-credits
        # nothing.
        self.baseline(8000)
        self.assertEqual(self.plan(obs(0, imported_ms=T0)), [])
        self.assertEqual(self.plan(obs(8000, imported_ms=T0)), [])

    def test_a_different_edition_in_the_same_folder_is_a_new_book(self):
        self.baseline(8000)
        self.assertEqual(self.plan(obs(9000, total=120_000, mark_ms=INSTALLED - 1)), [])
        self.assertEqual(self.state.books["本"].total, 120_000)
        self.assertEqual(self.state.books["本"].high_water, 9000)

    def test_a_lost_state_file_rebaselines_instead_of_recrediting(self):
        self.baseline(0)
        self.plan(obs(5000))
        self.state = policy.State(installed_at=T0 + 1)  # state file deleted, recreated now
        self.assertEqual(self.plan(obs(5000)), [])


class Retention(Base):
    def test_days_past_retention_are_dropped(self):
        self.baseline(0)
        old = T0 - 90 * DAY_MS
        ranges = self.plan(obs(2000, sessions={"a": s(old, 1000, 600)}), keep_days=60)
        self.assertEqual(spans(ranges), [(day_of(T0), 0, 1000)])

    def test_state_round_trips_through_json(self):
        self.baseline(0)
        self.state.pending.extend(self.plan(obs(700, sessions={"a": s(T0, 700, 90)})))
        again = policy.State.from_json(self.state.to_json())
        self.assertEqual(again.to_json(), self.state.to_json())


if __name__ == "__main__":
    unittest.main()

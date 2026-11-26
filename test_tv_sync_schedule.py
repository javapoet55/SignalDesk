"""The automatic TradingView sync runs on a weekday schedule with a daily attempt limit."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import tv_sync as tvs

PT = ZoneInfo("America/Los_Angeles")


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=PT).timestamp()


class Schedule(unittest.TestCase):
    def make(self, **env):
        self.now = [at("2026-11-26T11:00")]  # Thursday
        self.logs = []
        tmp = Path(tempfile.mkdtemp())
        sync = tvs.TVSync(tmp / "tv", tmp, tmp, signal_fn=lambda d: [], read_meta=lambda d: {}, tz=PT,
                          env=env, log=self.logs.append, clock=lambda: self.now[0])
        self.calls = []

        def attempt(force=False):
            self.calls.append(self.now[0])
            sync._note(last_error="quota")
            return False

        sync._sync_quietly = attempt
        return sync

    def run_loop_for(self, sync, start, minutes, step=0.5):
        """Tick like the background loop does (every 30 seconds) with work always pending."""
        self.now[0] = at(start)
        for _ in range(int(minutes / step)):
            if sync.auto_due():
                sync.auto_sync()
            self.now[0] += step * 60

    def test_two_tries_from_2pm_on_weekdays_only(self):
        sync = self.make()
        self.assertEqual((sync.sync_at, sync.sync_tries), ((14, 0), 2))
        self.run_loop_for(sync, "2026-11-26T11:00", 13 * 60)  # Thursday 11:00 to midnight, always failing
        self.assertEqual(self.calls, [at("2026-11-26T14:00"), at("2026-11-26T14:15")])
        self.assertTrue(any("2 automatic attempts used today" in line for line in self.logs))
        self.run_loop_for(sync, "2026-11-27T00:00", 24 * 60)  # Friday
        self.assertEqual(self.calls[2:], [at("2026-11-27T14:00"), at("2026-11-27T14:15")])
        self.run_loop_for(sync, "2026-11-28T00:00", 48 * 60)  # Saturday and Sunday
        self.assertEqual(len(self.calls), 4)
        self.run_loop_for(sync, "2026-11-30T13:00", 90)  # Monday
        self.assertEqual(self.calls[4:], [at("2026-11-30T14:00"), at("2026-11-30T14:15")])

    def test_restart_does_not_grant_more_tries(self):
        sync = self.make()
        self.run_loop_for(sync, "2026-11-26T14:00", 5)
        self.assertEqual(len(self.calls), 1)
        state_dir = sync.dir
        again = tvs.TVSync(state_dir, state_dir, state_dir, signal_fn=lambda d: [], read_meta=lambda d: {}, tz=PT,
                           env={}, log=self.logs.append, clock=lambda: self.now[0])
        self.assertFalse(again.auto_due())  # redeployed five minutes after the first attempt
        self.now[0] = at("2026-11-26T14:15")
        self.assertTrue(again.auto_due())
        again._sync_quietly = lambda force=False: False
        again.auto_sync()
        self.now[0] = at("2026-11-26T18:00")
        self.assertFalse(again.auto_due())

    def test_summer_time_and_settings(self):
        sync = self.make(TV_SYNC_AT="09:30", TV_SYNC_TRIES="1")
        self.run_loop_for(sync, "2026-07-01T00:00", 24 * 60)  # Wednesday, daylight time
        self.assertEqual(self.calls, [at("2026-07-01T09:30")])
        self.assertIn("weekdays at 09:30 PDT, up to 1 time a day", sync.schedule_text())
        self.assertEqual(self.make(TV_SYNC_AT="nonsense", TV_SYNC_TRIES="x").sync_at, (14, 0))

    def test_asap_mode_stops_after_failed_tries_but_not_after_successes(self):
        sync = self.make(TV_SYNC_AT="asap")
        sync._sync_quietly = lambda force=False: self.calls.append(self.now[0]) or True
        self.run_loop_for(sync, "2026-11-28T10:00", 5)  # a Saturday: every tick may sync
        self.assertEqual(len(self.calls), 10)
        sync._sync_quietly = lambda force=False: self.calls.append(self.now[0]) and False
        del self.calls[:]
        self.run_loop_for(sync, "2026-11-28T11:00", 120)
        self.assertEqual(self.calls, [at("2026-11-28T11:00"), at("2026-11-28T11:15")])


class QuotaStopsTheAttempt(unittest.TestCase):
    def test_remaining_lists_are_not_sent_after_a_quota_error(self):
        tmp = Path(tempfile.mkdtemp())
        sync = tvs.TVSync(tmp / "tv", tmp, tmp, signal_fn=lambda d: [], read_meta=lambda d: {}, log=lambda m: None)
        sync.auth.connected = lambda: True
        sync.pending = lambda force=False: [(k, k, [], "", k) for k in ("scan", "buy:a", "buy:b")]
        pushed = []

        def push(key, *rest):
            pushed.append(key)
            raise tvs.TVError("TradingView: TOOL_QUOTA_EXCEEDED - Tool call quota exceeded")

        sync.push = push
        with self.assertRaises(tvs.TVError):
            sync.sync()
        self.assertEqual(pushed, ["scan"])


if __name__ == "__main__":
    unittest.main()

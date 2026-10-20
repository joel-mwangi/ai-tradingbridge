import unittest
from datetime import datetime, timedelta, timezone

from app.strategy.feed_health import (
    FeedState,
    PercentageFeedHealth,
    PercentageSnapshot,
)


class PercentageFeedHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.monitor = PercentageFeedHealth("verified-feed-v1", "R_100", 5.0)
        self.snapshot = PercentageSnapshot(
            source_id="verified-feed-v1",
            market_id="R_100",
            observed_at=self.now - timedelta(seconds=1),
            received_at=self.now - timedelta(milliseconds=500),
            percentages={digit: 10.0 for digit in range(10)},
            event_id="evt-1",
        )

    def test_accepts_fresh_complete_snapshot(self):
        result = self.monitor.accept(self.snapshot, self.now)
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, FeedState.HEALTHY)

    def test_rejects_stale_snapshot_and_marks_unhealthy(self):
        old = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(seconds=8), self.now - timedelta(seconds=7),
            self.snapshot.percentages, "evt-old",
        )
        result = self.monitor.accept(old, self.now)
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, FeedState.UNHEALTHY)

    def test_rejects_wrong_market(self):
        bad = PercentageSnapshot(
            self.snapshot.source_id, "R_50", self.snapshot.observed_at,
            self.snapshot.received_at, self.snapshot.percentages, "evt-2",
        )
        self.assertFalse(self.monitor.accept(bad, self.now).accepted)

    def test_rejects_duplicate_event_id(self):
        self.assertTrue(self.monitor.accept(self.snapshot, self.now).accepted)
        next_snapshot = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(milliseconds=400),
            self.now - timedelta(milliseconds=300),
            self.snapshot.percentages, "evt-1",
        )
        self.assertFalse(self.monitor.accept(next_snapshot, self.now).accepted)

    def test_rejects_out_of_order_source_timestamp(self):
        self.assertTrue(self.monitor.accept(self.snapshot, self.now).accepted)
        older = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(seconds=2), self.now - timedelta(milliseconds=200),
            self.snapshot.percentages, "evt-2",
        )
        self.assertFalse(self.monitor.accept(older, self.now).accepted)

    def test_rejects_incomplete_snapshot(self):
        p = dict(self.snapshot.percentages)
        del p[9]
        bad = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.snapshot.observed_at, self.snapshot.received_at, p, "evt-3",
        )
        self.assertFalse(self.monitor.accept(bad, self.now).accepted)

    def test_rejects_non_finite_percentage(self):
        p = dict(self.snapshot.percentages)
        p[2] = float("nan")
        bad = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.snapshot.observed_at, self.snapshot.received_at, p, "evt-4",
        )
        self.assertFalse(self.monitor.accept(bad, self.now).accepted)

    def test_disconnect_blocks_snapshots_until_reset(self):
        self.monitor.mark_disconnected()
        result = self.monitor.accept(self.snapshot, self.now)
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, FeedState.DISCONNECTED)
        self.monitor.reset_after_reconnect()
        # Reconnect clears continuity; a fresh event is accepted.
        fresh = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(milliseconds=200), self.now - timedelta(milliseconds=100),
            self.snapshot.percentages, "evt-new",
        )
        self.assertTrue(self.monitor.accept(fresh, self.now).accepted)


    def test_unhealthy_feed_requires_explicit_reset(self):
        stale = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(seconds=8), self.now - timedelta(seconds=7),
            self.snapshot.percentages, "evt-stale",
        )
        self.assertFalse(self.monitor.accept(stale, self.now).accepted)
        self.assertFalse(self.monitor.accept(self.snapshot, self.now).accepted)
        self.monitor.reset_after_reconnect()
        fresh = PercentageSnapshot(
            self.snapshot.source_id, self.snapshot.market_id,
            self.now - timedelta(milliseconds=200), self.now - timedelta(milliseconds=100),
            self.snapshot.percentages, "evt-fresh",
        )
        self.assertTrue(self.monitor.accept(fresh, self.now).accepted)

    def test_requires_explicit_positive_freshness_limit(self):
        with self.assertRaises(ValueError):
            PercentageFeedHealth("feed", "R_100", float("nan"))
        with self.assertRaises(ValueError):
            PercentageFeedHealth("feed", "R_100", 0)


if __name__ == "__main__":
    unittest.main()

"""Store behaviour: idempotency, per-metric aggregation, retention, queries."""

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from tests import ROOT  # noqa: F401

from hermes_health import config, queries, store


def ts(day: str, hour: int = 0, minute: int = 0) -> int:
    """Epoch seconds for a local (America/Denver, per the test env) wall clock."""
    naive = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00")
    return int(naive.replace(tzinfo=config.tzinfo()).timestamp())


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "health.db")
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def sample(self, metric, day, hour, value, source="phone", unit=""):
        return store.Sample(metric, ts(day, hour), value, unit=unit, source=source)


class WriteTests(StoreTestCase):
    def test_samples_are_idempotent(self):
        batch = [self.sample("steps", "2026-11-08", 8, 100), self.sample("steps", "2026-11-08", 9, 250)]
        first = store.write_samples(self.conn, batch)
        second = store.write_samples(self.conn, batch)
        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(queries.daily_series(self.conn, "steps", days=1, end_day="2026-11-08")[0]["value"], 350.0)

    def test_sums_counters_within_a_day(self):
        store.write_samples(self.conn, [self.sample("steps", "2026-11-08", h, 100) for h in range(5)])
        day = queries.daily_series(self.conn, "steps", days=1, end_day="2026-11-08")[0]
        self.assertEqual(day["value"], 500.0)
        self.assertEqual(day["samples"], 5)

    def test_averages_rates_within_a_day(self):
        store.write_samples(
            self.conn,
            [self.sample("heart_rate", "2026-11-08", 6, 60), self.sample("heart_rate", "2026-11-08", 7, 80)],
        )
        day = queries.daily_series(self.conn, "heart_rate", days=1, end_day="2026-11-08")[0]
        self.assertEqual(day["value"], 70.0)
        self.assertEqual(day["min"], 60.0)
        self.assertEqual(day["max"], 80.0)

    def test_last_value_metrics(self):
        store.write_samples(
            self.conn,
            [self.sample("body_mass", "2026-11-07", 7, 182.0), self.sample("body_mass", "2026-11-08", 7, 181.0)],
        )
        series = queries.daily_series(self.conn, "body_mass", days=2, end_day="2026-11-08")
        self.assertEqual([entry["value"] for entry in series], [182.0, 181.0])

    def test_two_devices_merge_into_one_day(self):
        store.write_samples(
            self.conn,
            [self.sample("steps", "2026-11-08", 8, 100, source="watch"),
             self.sample("steps", "2026-11-08", 9, 40, source="iphone")],
        )
        series = queries.daily_series(self.conn, "steps", days=1, end_day="2026-11-08")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["value"], 140.0)
        self.assertEqual(sorted(series[0]["sources"]), ["iphone", "watch"])

    def test_source_filter(self):
        store.write_samples(
            self.conn,
            [self.sample("steps", "2026-11-08", 8, 100, source="watch"),
             self.sample("steps", "2026-11-08", 9, 40, source="iphone")],
        )
        series = queries.daily_series(self.conn, "steps", days=1, end_day="2026-11-08", source="watch")
        self.assertEqual(series[0]["value"], 100.0)

    def test_nan_and_garbage_values_are_skipped(self):
        result = store.write_samples(
            self.conn,
            [store.Sample("steps", ts("2026-11-08", 8), float("nan")),
             store.Sample("steps", ts("2026-11-08", 9), "not a number")],
        )
        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.skipped, 2)

    def test_pruned_hour_still_accepts_a_late_sample_additively(self):
        old = ts("2026-01-02", 8)
        store.write_samples(self.conn, [store.Sample("steps", old, 100, source="watch"),
                                        store.Sample("steps", old + 60, 50, source="watch")])
        # A one-day retention window deletes both raw rows (the samples are months old).
        removed = store.prune_raw(self.conn, keep_days=1)
        self.assertEqual(removed, 2)
        before = self.conn.execute("SELECT val_sum, val_n FROM samples_hourly WHERE metric='steps'").fetchone()
        self.assertEqual((before["val_sum"], before["val_n"]), (150.0, 2))
        # A late sample for an already-rolled-up hour must not wipe the bucket.
        store.write_samples(self.conn, [store.Sample("steps", old + 120, 25, source="watch")])
        after = self.conn.execute("SELECT val_sum, val_n FROM samples_hourly WHERE metric='steps'").fetchone()
        self.assertEqual((after["val_sum"], after["val_n"]), (175.0, 3))

    def test_sleep_spans_are_idempotent_and_typed(self):
        spans = [
            store.SleepSpan(ts("2026-11-07", 23), ts("2026-11-08", 2), "core", "watch"),
            store.SleepSpan(ts("2026-11-08", 2), ts("2026-11-08", 3), "deep", "watch"),
        ]
        first = store.write_sleep(self.conn, spans)
        second = store.write_sleep(self.conn, spans)
        self.assertEqual((first.inserted, second.inserted), (2, 0))
        nights = queries.sleep_nights(self.conn, days=2, end_day="2026-11-08")
        self.assertEqual(len(nights), 1)
        self.assertAlmostEqual(nights[0]["asleep_hours"], 4.0, places=2)

    def test_workouts_are_idempotent_with_a_stable_id(self):
        workout = store.Workout("running", ts("2026-11-07", 6), ts("2026-11-07", 7), source="watch",
                                energy_kcal=420, distance_m=4200, avg_hr=152, max_hr=178)
        first = store.write_workouts(self.conn, [workout])
        second = store.write_workouts(self.conn, [workout])
        self.assertEqual((first.inserted, second.inserted), (1, 0))
        self.assertEqual(store.workout_id("running", workout.start_ts, "watch"),
                         store.workout_id("running", workout.start_ts, "watch"))
        report = queries.workout_report(self.conn, days=30, end_day="2026-11-08")
        self.assertEqual(report["n"], 1)
        self.assertEqual(report["total_kcal"], 420.0)
        self.assertAlmostEqual(report["sessions"][0]["distance_mi"], 2.61, places=2)

    def test_cursor_reports_how_far_back_the_store_goes(self):
        store.write_samples(self.conn, [self.sample("steps", "2026-11-08", 8, 100)])
        cursor = store.cursor(self.conn)
        self.assertEqual(cursor["metrics"]["steps"]["last_ts"], ts("2026-11-08", 8))

    def test_ingest_log_and_freshness(self):
        store.write_samples(self.conn, [self.sample("steps", "2026-11-08", 8, 100)])
        store.log_ingest(self.conn, kind="http", source="phone", rows=1, metrics={"steps": 1})
        freshness = queries.freshness(self.conn)
        self.assertIsNotNone(freshness["last_delivery"])
        self.assertEqual(freshness["last_delivery"]["rows"], 1)
        self.assertIsNotNone(freshness["newest_data_ts"])


class RollupTests(StoreTestCase):
    def test_rebuild_rollups_matches_incremental_writes(self):
        samples = [self.sample("steps", "2026-11-08", h, 10 * h) for h in range(6)]
        samples += [self.sample("heart_rate", "2026-11-08", h, 60 + h) for h in range(6)]
        store.write_samples(self.conn, samples)
        incremental = self.conn.execute(
            "SELECT metric, day, value, val_n FROM daily ORDER BY metric, day"
        ).fetchall()
        store.rebuild_rollups(self.conn)
        rebuilt = self.conn.execute(
            "SELECT metric, day, value, val_n FROM daily ORDER BY metric, day"
        ).fetchall()
        self.assertEqual([tuple(r) for r in incremental], [tuple(r) for r in rebuilt])

    def test_rebuild_does_not_touch_history_older_than_the_raw_window(self):
        store.write_samples(self.conn, [self.sample("steps", "2026-01-05", 8, 500)])
        store.prune_raw(self.conn, keep_days=1)
        before = self.conn.execute("SELECT COUNT(*) AS n FROM samples_hourly").fetchone()["n"]
        store.rebuild_rollups(self.conn)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM samples_hourly").fetchone()["n"]
        self.assertEqual((before, after), (1, 1))
        day = queries.daily_series(self.conn, "steps", days=1, end_day="2026-01-05")[0]
        self.assertEqual(day["value"], 500.0)


class QueryTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        samples = []
        for offset in range(10):
            day = (datetime.fromisoformat("2026-11-08") - timedelta(days=offset)).date().isoformat()
            samples.append(self.sample("steps", day, 12, 1000 + offset * 10))
            samples.append(self.sample("resting_heart_rate", day, 6, 50 + offset))
        samples.append(self.sample("active_energy", "2026-11-08", 12, 600))
        samples.append(self.sample("exercise_minutes", "2026-11-08", 12, 40))
        samples.append(self.sample("stand_hours", "2026-11-08", 12, 13))
        store.write_samples(self.conn, samples)

    def test_window_stats(self):
        stats = queries.window_stats(self.conn, "steps", days=7, end_day="2026-11-08")
        self.assertEqual(stats["n"], 7)
        self.assertEqual(stats["total"], 7 * 1000 + 10 * (0 + 1 + 2 + 3 + 4 + 5 + 6))
        self.assertEqual(stats["latest_day"], "2026-11-08")

    def test_compare_windows(self):
        change = queries.compare_windows(self.conn, "resting_heart_rate", days=3, end_day="2026-11-08")
        self.assertEqual(change["direction"], "down")  # later days are the lower values
        self.assertIsNotNone(change["change_pct"])

    def test_rings_use_stored_goals(self):
        store.set_meta(self.conn, "goal_active_energy", "780")
        rings = queries.rings(self.conn, day="2026-11-08")
        self.assertEqual(rings["rings"]["active_energy"]["goal"], 780.0)
        self.assertAlmostEqual(rings["rings"]["active_energy"]["pct"], 77.0, places=1)
        self.assertEqual(rings["steps"], 1000.0)

    def test_summary_shape(self):
        summary = queries.summary(self.conn, days=7, end_day="2026-11-08")
        for key in ("today", "sleep", "heart", "body", "workouts", "changes", "data"):
            self.assertIn(key, summary)
        self.assertIn("resting_heart_rate", summary["heart"])
        self.assertEqual(summary["today"]["day"], "2026-11-08")

    def test_status_and_catalog(self):
        status = queries.status(self.conn)
        self.assertGreaterEqual(status["metric_count"], 3)
        self.assertIn("steps", status["metrics_available"])
        catalog = queries.catalog(self.conn)
        self.assertTrue(any(entry["key"] == "steps" for entry in catalog["metrics"]))
        self.assertEqual(catalog["unregistered_in_store"], [])


class SleepQueryTests(StoreTestCase):
    def test_nights_split_on_gaps_and_are_attributed_to_wake_day(self):
        spans = []
        for night in range(3):
            day = (datetime.fromisoformat("2026-11-08") - timedelta(days=night)).date().isoformat()
            # 23:00 -> 02:00 core + 02:00 -> 06:00 deep, waking on `day`
            previous = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            spans.append(store.SleepSpan(ts(previous, 23), ts(day, 2), "core", "watch"))
            spans.append(store.SleepSpan(ts(day, 2), ts(day, 6), "deep", "watch"))
        store.write_sleep(self.conn, spans)
        nights = queries.sleep_nights(self.conn, days=5, end_day="2026-11-08")
        self.assertEqual(len(nights), 3)
        self.assertEqual(nights[-1]["day"], "2026-11-08")
        self.assertAlmostEqual(nights[-1]["asleep_hours"], 7.0, places=2)
        self.assertAlmostEqual(nights[-1]["efficiency_pct"], 100.0, places=1)

    def test_in_bed_time_is_not_counted_as_asleep(self):
        store.write_sleep(self.conn, [
            store.SleepSpan(ts("2026-11-07", 22), ts("2026-11-08", 6), "in_bed", "watch"),
            store.SleepSpan(ts("2026-11-07", 23), ts("2026-11-08", 2), "core", "watch"),
        ])
        night = queries.sleep_nights(self.conn, days=2, end_day="2026-11-08")[0]
        self.assertAlmostEqual(night["asleep_hours"], 3.0, places=2)
        self.assertAlmostEqual(night["in_bed_hours"], 8.0, places=2)

    def test_short_sessions_are_flagged_as_naps(self):
        store.write_sleep(self.conn, [
            store.SleepSpan(ts("2026-11-07", 23), ts("2026-11-08", 6), "core", "watch"),
            store.SleepSpan(ts("2026-11-07", 14), ts("2026-11-07", 15), "core", "watch"),
        ])
        nights = queries.sleep_nights(self.conn, days=2, end_day="2026-11-08")
        naps = [n for n in nights if n["is_nap"]]
        self.assertEqual(len(naps), 1)
        report = queries.sleep_report(self.conn, days=2, end_day="2026-11-08")
        self.assertEqual(report["last_night"]["day"], "2026-11-08")

    def test_bedtime_average_does_not_wrap_midnight(self):
        # 23:30 and 00:30 are 60 minutes apart, not 23 hours.
        store.write_sleep(self.conn, [
            store.SleepSpan(ts("2026-11-06", 23, 30), ts("2026-11-07", 7), "core", "watch"),
            store.SleepSpan(ts("2026-11-08", 0, 30), ts("2026-11-08", 7), "core", "watch"),
        ])
        report = queries.sleep_report(self.conn, days=3, end_day="2026-11-08")
        self.assertEqual(report["averages"]["bedtime"], "00:00")
        self.assertLess(report["consistency"]["bedtime_sd_minutes"], 90)


if __name__ == "__main__":
    unittest.main()

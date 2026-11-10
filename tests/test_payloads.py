"""Payload parsing: the two delivery shapes, their variants, and junk input."""

import json
import unittest
from datetime import datetime, timezone

from tests import ROOT  # noqa: F401

from hermes_health import metrics, payloads


class ShortcutsShapeTests(unittest.TestCase):
    def test_text_rows_with_offsets(self):
        batch = payloads.parse_json_text(
            json.dumps(
                {
                    "steps": "2026-11-08T08:00:00-07:00 | 8123\n2026-11-08T09:00:00-07:00 | 300",
                    "resting_heart_rate": "2026-11-08T06:00:00-07:00 | 52",
                }
            )
        )
        self.assertEqual(len(batch.samples), 3)
        by_metric = {}
        for sample in batch.samples:
            by_metric.setdefault(sample.metric, []).append(sample)
        self.assertEqual(by_metric["steps"][0].value, 8123.0)
        # The offset travels with the sample so the day it belongs to is the phone's day.
        self.assertEqual(by_metric["steps"][0].offset_s, -7 * 3600)
        self.assertEqual(payloads.parse_ts("2026-11-08T08:00:00-07:00")[0],
                         payloads.parse_ts("2026-11-08T15:00:00+00:00")[0])

    def test_bare_numbers(self):
        batch = payloads.parse_json_text(json.dumps({"steps": 8123, "weight": "181.4"}))
        values = {sample.metric: sample.value for sample in batch.samples}
        self.assertEqual(values["steps"], 8123.0)
        self.assertEqual(values["weight"], 181.4)

    def test_list_of_points(self):
        batch = payloads.parse_json_text(
            json.dumps({"steps": [{"start": "2026-11-08T08:00:00-07:00", "value": 400},
                                  {"date": "2026-11-08 09:00:00 -0700", "qty": 500}]})
        )
        self.assertEqual([s.value for s in batch.samples], [400.0, 500.0])

    def test_value_with_units_and_commas(self):
        batch = payloads.parse_json_text(json.dumps({"steps": "1,234 steps"}))
        self.assertEqual(batch.samples[0].value, 1234.0)

    def test_sleep_rows_in_shortcuts_body(self):
        batch = payloads.parse_json_text(
            json.dumps(
                {
                    "sleep": "2026-11-07T23:10:00-07:00 | 2026-11-08T02:00:00-07:00 | Core\n"
                             "2026-11-08T02:00:00-07:00 | 2026-11-08T03:30:00-07:00 | Deep"
                }
            )
        )
        self.assertEqual(len(batch.sleep), 2)
        self.assertEqual([s.stage for s in batch.sleep], ["core", "deep"])
        self.assertEqual(batch.sleep[0].duration_s, None)  # derived from start/end in the store

    def test_sleep_with_duration_instead_of_end(self):
        batch = payloads.parse_json_text(
            json.dumps({"sleep": "2026-11-08T01:00:00-07:00 | 1.5 | REM"})
        )
        self.assertEqual(len(batch.sleep), 1)
        self.assertAlmostEqual(batch.sleep[0].duration_s, 5400.0)
        self.assertEqual(batch.sleep[0].end_ts - batch.sleep[0].start_ts, 5400)

    def test_workouts_block(self):
        batch = payloads.parse_json_text(
            json.dumps(
                {
                    "workouts": [
                        {
                            "activity": "Running",
                            "start": "2026-11-07T06:30:00-07:00",
                            "end": "2026-11-07T07:10:00-07:00",
                            "activeEnergy": 420,
                            "distance": 4.2,
                            "distanceUnit": "km",
                            "avgHeartRate": 152,
                            "maxHeartRate": 178,
                        }
                    ]
                }
            )
        )
        self.assertEqual(len(batch.workouts), 1)
        workout = batch.workouts[0]
        self.assertEqual(workout.activity, "running")
        self.assertEqual(workout.energy_kcal, 420.0)
        self.assertAlmostEqual(workout.distance_m, 4200.0, places=3)
        self.assertEqual(workout.avg_hr, 152.0)

    def test_metadata_keys_are_not_metrics(self):
        batch = payloads.parse_json_text(
            json.dumps({"source": "iPhone", "tz": "America/Denver", "datetime": "2026-11-08T00:00:00Z",
                        "steps": 10})
        )
        self.assertEqual([s.metric for s in batch.samples], ["steps"])


class HealthAutoExportShapeTests(unittest.TestCase):
    def test_rest_v2_metrics(self):
        payload = {
            "data": {
                "metrics": [
                    {"name": "step_count", "units": "count",
                     "data": [{"date": "2026-11-08 00:00:00 -0700", "qty": 8123}]},
                    {"name": "heart_rate", "units": "count/min",
                     "data": [{"date": "2026-11-08 08:00:00 -0700", "Min": 55, "Avg": 72, "Max": 110}]},
                ]
            }
        }
        batch = payloads.parse_payload(payload)
        self.assertEqual(batch.shape, "hae")
        values = {metrics.resolve(s.metric).key: (s.value, s.unit) for s in batch.samples}
        self.assertEqual(values["steps"][0], 8123.0)
        self.assertEqual(values["steps"][1], "count")
        self.assertEqual(values["heart_rate"][0], 72.0)  # Avg is the value; Min/Max are not samples

    def test_rest_v2_workouts(self):
        payload = {
            "data": {
                "workouts": [
                    {"name": "Cycling", "start": "2026-11-08 07:00:00 -0700",
                     "end": "2026-11-08 07:45:00 -0700",
                     "activeEnergyBurned": {"qty": 500, "units": "kcal"},
                     "distance": {"qty": 12.4, "units": "mi"}}
                ]
            }
        }
        batch = payloads.parse_payload(payload)
        self.assertEqual(len(batch.workouts), 1)
        workout = batch.workouts[0]
        self.assertEqual(workout.activity, "cycling")
        self.assertEqual(workout.energy_kcal, 500.0)
        self.assertAlmostEqual(workout.distance_m / 1609.344, 12.4, places=2)

    def test_flat_metric_mapping_also_works(self):
        batch = payloads.parse_payload({"metrics": {"step_count": [{"date": "2026-11-08T00:00:00Z",
                                                                   "value": 5}]}})
        self.assertEqual(batch.samples[0].value, 5.0)


class RobustnessTests(unittest.TestCase):
    def test_invalid_json_is_reported_not_raised(self):
        batch = payloads.parse_json_text("{not json")
        self.assertTrue(batch.errors)
        self.assertEqual(batch.points, 0)

    def test_empty_body(self):
        self.assertTrue(payloads.parse_json_text("   ").errors)

    def test_bad_row_does_not_lose_the_good_ones(self):
        batch = payloads.parse_json_text(
            json.dumps({"steps": "garbage\n2026-11-08T08:00:00-07:00 | 100"})
        )
        self.assertEqual(len(batch.samples), 1)
        self.assertEqual(len(batch.errors), 1)

    def test_form_encoded_body_is_tolerated(self):
        batch = payloads.parse_json_text("steps=100&weight=181")
        metrics_seen = {s.metric for s in batch.samples}
        self.assertIn("steps", metrics_seen)

    def test_timestamps(self):
        epoch = payloads.parse_ts(1788539567)
        self.assertEqual(epoch[0], 1788539567)
        self.assertIsNone(epoch[1])
        self.assertEqual(payloads.parse_ts(1788539567000)[0], 1788539567)
        self.assertEqual(payloads.parse_ts("2026-11-08")[0],
                         payloads.parse_ts("2026-11-08T00:00:00")[0])
        self.assertIsNone(payloads.parse_ts("not a date"))

    def test_healthkit_names_survive_the_round_trip(self):
        batch = payloads.parse_json_text(
            json.dumps({"HKQuantityTypeIdentifierStepCount": "2026-11-08T08:00:00-07:00 | 12"})
        )
        self.assertEqual(metrics.resolve(batch.samples[0].metric).key, "steps")


if __name__ == "__main__":
    unittest.main()

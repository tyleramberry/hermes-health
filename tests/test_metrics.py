"""Metric resolution: three spellings of the same thing, and one unknown."""

import unittest

from tests import ROOT  # noqa: F401  (sets sys.path and the test timezone)

from hermes_health import metrics


class ResolutionTests(unittest.TestCase):
    def test_healthkit_identifier_resolves(self):
        self.assertEqual(metrics.resolve("HKQuantityTypeIdentifierStepCount").key, "steps")
        self.assertEqual(metrics.resolve("HKQuantityTypeIdentifierRestingHeartRate").key, "resting_heart_rate")
        self.assertEqual(metrics.resolve("HKQuantityTypeIdentifierBodyMass").key, "body_mass")
        self.assertEqual(metrics.resolve("HKDataTypeSleepDurationGoal").key, "sleep_goal")
        self.assertEqual(metrics.resolve("HKCategoryTypeIdentifierSleepAnalysis").key, "sleep")

    def test_shortcuts_label_resolves(self):
        self.assertEqual(metrics.resolve("Steps").key, "steps")
        self.assertEqual(metrics.resolve("Resting Heart Rate").key, "resting_heart_rate")
        self.assertEqual(metrics.resolve("Heart Rate Variability").key, "hrv")
        self.assertEqual(metrics.resolve("Sleep").key, "sleep")
        self.assertEqual(metrics.resolve("Weight").key, "body_mass")

    def test_health_auto_export_name_resolves(self):
        self.assertEqual(metrics.resolve("step_count").key, "steps")
        self.assertEqual(metrics.resolve("heart_rate_variability").key, "hrv")
        self.assertEqual(metrics.resolve("apple_sleeping_wrist_temperature").key, "wrist_temperature")
        self.assertEqual(metrics.resolve("walking_double_support_percentage").key, "walking_double_support")

    def test_all_three_spellings_agree(self):
        groups = [
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "active_energy", "Active Energy"),
            ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "hrv", "HRV"),
            ("HKQuantityTypeIdentifierAppleExerciseTime", "exercise_minutes", "Apple Exercise Time"),
            ("HKQuantityTypeIdentifierDistanceWalkingRunning", "distance_walking_running", "Distance"),
        ]
        for hk, hae, label in groups:
            keys = {metrics.resolve(x).key for x in (hk, hae, label)}
            self.assertEqual(len(keys), 1, f"{hk}/{hae}/{label} disagreed: {keys}")

    def test_unknown_metric_is_kept_not_dropped(self):
        metric = metrics.resolve("HKQuantityTypeIdentifierSomeFutureThing", "kcal/hr\u00b7kg")
        self.assertEqual(metric.key, "some_future_thing")
        self.assertEqual(metric.agg, metrics.AVG)

    def test_unknown_unit_decides_aggregation(self):
        self.assertEqual(metrics.guess_agg("count/min"), metrics.AVG)
        self.assertEqual(metrics.guess_agg("mi/hr"), metrics.AVG)
        self.assertEqual(metrics.guess_agg("%"), metrics.AVG)
        self.assertEqual(metrics.guess_agg("count"), metrics.SUM)
        self.assertEqual(metrics.guess_agg("Cal"), metrics.SUM)

    def test_aggregation_rules_are_sane(self):
        self.assertEqual(metrics.resolve("steps").agg, metrics.SUM)
        self.assertEqual(metrics.resolve("active_energy").agg, metrics.SUM)
        self.assertEqual(metrics.resolve("resting_heart_rate").agg, metrics.AVG)
        self.assertEqual(metrics.resolve("hrv").agg, metrics.AVG)
        self.assertEqual(metrics.resolve("body_mass").agg, metrics.LAST)
        self.assertEqual(metrics.resolve("vo2_max").agg, metrics.LAST)

    def test_empty_name_is_an_error(self):
        with self.assertRaises(ValueError):
            metrics.resolve("  ")

    def test_match_keys_searches_names_and_aliases(self):
        heart = metrics.match_keys("heart")
        self.assertIn("resting_heart_rate", heart)
        self.assertIn("hrv", heart)
        self.assertNotIn("steps", heart)


class SleepStageTests(unittest.TestCase):
    def test_healthkit_values(self):
        self.assertEqual(metrics.sleep_stage("HKCategoryValueSleepAnalysisAsleepDeep"), "deep")
        self.assertEqual(metrics.sleep_stage("HKCategoryValueSleepAnalysisAsleepREM"), "rem")
        self.assertEqual(metrics.sleep_stage("HKCategoryValueSleepAnalysisAsleepCore"), "core")
        self.assertEqual(metrics.sleep_stage("HKCategoryValueSleepAnalysisInBed"), "in_bed")
        self.assertEqual(metrics.sleep_stage("HKCategoryValueSleepAnalysisAwake"), "awake")

    def test_plain_and_localized_labels(self):
        self.assertEqual(metrics.sleep_stage("Deep"), "deep")
        self.assertEqual(metrics.sleep_stage("REM"), "rem")
        self.assertEqual(metrics.sleep_stage("Core"), "core")
        self.assertEqual(metrics.sleep_stage("In Bed"), "in_bed")
        # A German phone emits localized stage names; substring matching handles it.
        self.assertEqual(metrics.sleep_stage("Tiefschlaf"), "deep")

    def test_unrecognized_stage_falls_back_to_asleep(self):
        self.assertEqual(metrics.sleep_stage("SomethingNew"), "asleep")


class ActivityTests(unittest.TestCase):
    def test_healthkit_workout_types(self):
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypeRunning"), "running")
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypeTraditionalStrengthTraining"),
                         "strength_training")
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypeHighIntensityIntervalTraining"),
                         "hiit")
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypePickleball"), "pickleball")
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypeSurfingSports"), "surfing")

    def test_export_snake_case_and_labels(self):
        self.assertEqual(metrics.activity_slug("traditional_strength_training"), "strength_training")
        self.assertEqual(metrics.activity_slug("Running"), "running")
        self.assertEqual(metrics.activity_slug("Other"), "other")

    def test_unknown_activity_degrades_to_a_slug(self):
        self.assertEqual(metrics.activity_slug("HKWorkoutActivityTypeTeleportation"), "teleportation")


if __name__ == "__main__":
    unittest.main()

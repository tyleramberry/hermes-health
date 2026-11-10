"""CLI and importer: a synthetic export in, real answers out."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from tests import ROOT  # noqa: F401

from hermes_health import cli, store

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2026-11-08 12:00:00 -0700"/>
 <Me HKCharacteristicTypeIdentifierDateOfBirth="2005-03-31"
     HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Tyler's Apple Watch" unit="count"
         creationDate="2026-11-01 09:00:00 -0600" startDate="2026-11-01 08:00:00 -0600"
         endDate="2026-11-01 09:00:00 -0600" value="1200"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Tyler's Apple Watch" unit="count"
         creationDate="2026-11-02 09:00:00 -0600" startDate="2026-11-02 08:00:00 -0600"
         endDate="2026-11-02 09:00:00 -0600" value="900"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Tyler's Apple Watch" unit="count/min"
         creationDate="2026-11-01 07:00:00 -0600" startDate="2026-11-01 07:00:00 -0600"
         endDate="2026-11-01 07:00:00 -0600" value="52"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Tyler's iPhone" unit="lb"
         creationDate="2026-11-02 07:00:00 -0600" startDate="2026-11-02 07:00:00 -0600"
         endDate="2026-11-02 07:00:00 -0600" value="181.4"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Tyler's Apple Watch" unit=""
         creationDate="2026-11-02 06:00:00 -0600" startDate="2026-11-02 02:00:00 -0600"
         endDate="2026-11-02 04:00:00 -0600" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Tyler's Apple Watch" unit=""
         creationDate="2026-11-02 06:00:00 -0600" startDate="2026-11-02 04:00:00 -0600"
         endDate="2026-11-02 06:00:00 -0600" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Record type="HKCategoryTypeIdentifierAppleStandHour" sourceName="Tyler's Apple Watch" unit=""
         creationDate="2026-11-02 09:00:00 -0600" startDate="2026-11-02 08:00:00 -0600"
         endDate="2026-11-02 09:00:00 -0600" value="HKCategoryValueAppleStandHourStood"/>
 <Record type="HKCategoryTypeIdentifierAudioExposureEvent" sourceName="Tyler's Apple Watch" unit=""
         creationDate="2026-11-02 09:00:00 -0600" startDate="2026-11-02 09:00:00 -0600"
         endDate="2026-11-02 09:00:00 -0600" value="HKCategoryValueAudioExposureEventLoudEnvironment"/>
 <ActivitySummary dateComponents="2026-11-01" activeEnergyBurned="558.841" activeEnergyBurnedGoal="780"
                  appleMoveTime="0" appleMoveTimeGoal="0" appleExerciseTime="22"
                  appleExerciseTimeGoal="45" appleStandHours="14" appleStandHoursGoal="12"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30.5" durationUnit="min"
          sourceName="Tyler's Apple Watch" creationDate="2026-11-02 07:30:00 -0600"
          startDate="2026-11-02 07:00:00 -0600" endDate="2026-11-02 07:30:30 -0600">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
                     startDate="2026-11-02 07:00:00 -0600" endDate="2026-11-02 07:30:30 -0600"
                     sum="380" unit="Cal"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
                     startDate="2026-11-02 07:00:00 -0600" endDate="2026-11-02 07:30:30 -0600"
                     sum="3.1" unit="mi"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"
                     startDate="2026-11-02 07:00:00 -0600" endDate="2026-11-02 07:30:30 -0600"
                     average="151" minimum="98" maximum="179" unit="count/min"/>
 </Workout>
</HealthData>
"""


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "health.db")
        self.xml = os.path.join(self.dir.name, "export.xml")
        with open(self.xml, "w", encoding="utf-8") as handle:
            handle.write(EXPORT_XML)

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["--db", self.db, *argv])
        return code, buffer.getvalue()


class ImportTests(CliTestCase):
    def test_import_reports_what_landed(self):
        code, output = self.run_cli("import-export", self.xml)
        self.assertEqual(code, 0)
        self.assertIn("records read:", output)
        self.assertIn("workouts:", output)
        conn = store.connect(self.db)
        try:
            counts = store.counts(conn)
            self.assertEqual(counts["workouts"], 1)
            self.assertEqual(counts["sleep"], 2)
            # Category records we do not model are ignored, not stored as samples.
            self.assertEqual(counts["samples_raw"], 5)
            metrics = store.known_metrics(conn)
            self.assertIn("steps", metrics)
            self.assertIn("stand_hours", metrics)
            self.assertNotIn("audio_exposure_event", metrics)
            job = conn.execute("SELECT activity, duration_s, energy_kcal, distance_m, avg_hr, max_hr"
                               " FROM workouts").fetchone()
            self.assertEqual(job["activity"], "running")
            self.assertAlmostEqual(job["energy_kcal"], 380.0)
            self.assertAlmostEqual(job["distance_m"], 3.1 * 1609.344, places=3)
            self.assertEqual(job["avg_hr"], 151.0)
            self.assertEqual(job["max_hr"], 179.0)
            # Goals and profile come from the export rather than being assumed.
            self.assertEqual(store.get_meta(conn, "goal_active_energy"), "780")
            self.assertEqual(store.get_meta(conn, "profile_date_of_birth"), "2005-03-31")
        finally:
            conn.close()

    def test_reimport_is_a_no_op(self):
        self.run_cli("import-export", self.xml)
        _code, output = self.run_cli("import-export", self.xml)
        self.assertIn("(new)", output)
        self.assertIn("already present", output)
        # The second pass inserts nothing new.
        self.assertIn("samples stored:    0 (new)", output)

    def test_since_filter(self):
        code, _output = self.run_cli("import-export", self.xml, "--since", "2026-11-02")
        self.assertEqual(code, 0)
        conn = store.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM samples_raw").fetchone()["n"], 3)
        finally:
            conn.close()


class ReadCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli("import-export", self.xml)

    def test_status(self):
        code, output = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("hermes-health", output)
        self.assertIn("metrics", output)

    def test_status_json(self):
        _code, output = self.run_cli("status", "--json")
        payload = json.loads(output)
        self.assertIn("counts", payload)
        self.assertEqual(payload["counts"]["workouts"], 1)

    def test_brief_is_plain_text_with_the_numbers(self):
        code, output = self.run_cli("brief", "--days", "7", "--end", "2026-11-02")
        self.assertEqual(code, 0)
        self.assertIn("HEALTH BRIEF", output)
        self.assertIn("LAST NIGHT", output)
        self.assertIn("4h00m", output)  # 2h deep + 2h core
        self.assertIn("WORKOUTS", output)
        self.assertIn("running", output)

    def test_query_prints_a_series(self):
        code, output = self.run_cli("query", "steps", "--days", "5", "--end", "2026-11-02")
        self.assertEqual(code, 0)
        self.assertIn("2026-11-02", output)
        self.assertIn("900", output)
        _code, payload_text = self.run_cli("query", "Steps", "--days", "5", "--end", "2026-11-02", "--json")
        payload = json.loads(payload_text)
        self.assertEqual(payload["metric"], "steps")
        self.assertEqual(len(payload["series"]), 2)

    def test_sleep_and_workouts_commands(self):
        _code, sleep_text = self.run_cli("sleep", "--days", "5", "--end", "2026-11-02", "--json")
        sleep = json.loads(sleep_text)
        self.assertEqual(len(sleep["nights"]), 1)
        self.assertAlmostEqual(sleep["nights"][0]["asleep_hours"], 4.0, places=2)
        _code, workout_text = self.run_cli("workouts", "--days", "5", "--end", "2026-11-02", "--json")
        workouts = json.loads(workout_text)
        self.assertEqual(workouts["n"], 1)
        self.assertEqual(workouts["sessions"][0]["activity"], "running")

    def test_widget_writes_a_file(self):
        out = os.path.join(self.dir.name, "widget.html")
        code, output = self.run_cli("widget", "--days", "5", "--end", "2026-11-02", "--out", out)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), out)
        with open(out, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Move", html)
        self.assertIn("Recent workouts", html)

    def test_mcp_config_prints_a_pasteable_block(self):
        code, output = self.run_cli("mcp-config")
        self.assertEqual(code, 0)
        block = json.loads(output)
        self.assertIn("health", block["mcpServers"])
        self.assertTrue(block["mcpServers"]["health"]["args"][0].endswith("health_mcp.py"))

    def test_prune_refuses_without_yes(self):
        code, _output = self.run_cli("prune")
        self.assertEqual(code, 2)

    def test_rebuild(self):
        code, output = self.run_cli("rebuild")
        self.assertEqual(code, 0)
        self.assertIn("hourly", json.loads(output))


if __name__ == "__main__":
    unittest.main()

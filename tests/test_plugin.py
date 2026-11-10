"""The Hermes plugin: load it the way Hermes does, then call every tool."""

import json
import os
import tempfile
import unittest

from tests import ROOT, FakeCtx, load_plugin_package

from hermes_health import store


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "health.db")
        conn = store.connect(self.db)
        store.write_samples(conn, [
            store.Sample("steps", 1783000000, 4321, source="watch"),
            store.Sample("resting_heart_rate", 1783000000, 51, source="watch"),
        ])
        store.write_sleep(conn, [store.SleepSpan(1783000000 - 25200, 1783000000, "core", "watch")])
        store.write_workouts(conn, [store.Workout("running", 1782990000, 1782993600, source="watch",
                                                 energy_kcal=400, distance_m=5000, avg_hr=150)])
        conn.commit()
        conn.close()
        self.package = load_plugin_package()
        self.ctx = FakeCtx({"db_path": self.db})
        self.package.register(self.ctx)

    def tearDown(self):
        # The plugin applies its configured timezone process-wide; restore it so
        # one test's setting cannot leak into the next.
        os.environ["HERMES_HEALTH_TZ"] = "America/Denver"
        self.dir.cleanup()

    def call(self, name, **args):
        result = self.ctx.tools[name]["handler"](args)
        self.assertIsInstance(result, str, "tool handlers must return a JSON string")
        return json.loads(result)

    def test_registers_all_tools_on_one_toolset(self):
        expected = {"health_summary", "health_query", "health_sleep", "health_workouts",
                    "health_metrics", "health_status", "health_dashboard"}
        self.assertEqual(set(self.ctx.tools), expected)
        for entry in self.ctx.tools.values():
            self.assertEqual(entry["toolset"], "health")
            self.assertEqual(entry["schema"]["parameters"]["type"], "object")
            self.assertTrue(callable(entry["handler"]))

    def test_registers_the_command_and_the_skill(self):
        self.assertIn("health", self.ctx.commands)
        self.assertIn("health-brief", self.ctx.skills)
        self.assertTrue(self.ctx.skills["health-brief"].exists())

    def test_summary_tool(self):
        payload = self.call("health_summary", days=7)
        self.assertTrue(payload["ok"])
        self.assertIn("sleep", payload)
        self.assertIn("today", payload)

    def test_query_tool_accepts_any_spelling(self):
        payload = self.call("health_query", metric="Steps", days=400)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["metric"], "steps")
        self.assertEqual(payload["series"][0]["value"], 4321.0)

    def test_query_tool_reports_unknown_window_honestly(self):
        payload = self.call("health_query", metric="vo2_max", days=5)
        self.assertFalse(payload["ok"])
        self.assertIn("no stored data", payload["error"])
        self.assertIn("metrics_with_data", payload)

    def test_query_requires_a_metric(self):
        payload = self.call("health_query")
        self.assertFalse(payload["ok"])
        self.assertIn("metric is required", payload["error"])

    def test_sleep_workouts_metrics_status(self):
        sleep = self.call("health_sleep", days=400)
        self.assertTrue(sleep["ok"])
        self.assertEqual(len(sleep["nights"]), 1)
        workouts = self.call("health_workouts", days=400)
        self.assertEqual(workouts["n"], 1)
        filtered = self.call("health_workouts", days=400, activity="cycling")
        self.assertEqual(filtered["n"], 0)
        metrics = self.call("health_metrics", search="heart")
        self.assertEqual(metrics["search"], "heart")
        self.assertTrue(any(entry["key"] == "resting_heart_rate" for entry in metrics["metrics"]))
        status = self.call("health_status")
        self.assertGreater(status["counts"]["samples_raw"], 0)

    def test_dashboard_writes_html_and_says_how_to_show_it(self):
        payload = self.call("health_dashboard", days=7, metric_days=14)
        self.assertTrue(payload["ok"])
        path = payload["widget_path"]
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("::preview", payload["instructions"])
        self.assertIn(path, payload["instructions"])

    def test_empty_store_gives_an_actionable_message_not_an_exception(self):
        empty_db = os.path.join(self.dir.name, "empty", "health.db")
        ctx = FakeCtx({"db_path": empty_db})
        self.package.register(ctx)
        payload = json.loads(ctx.tools["health_summary"]["handler"]({}))
        self.assertFalse(payload["ok"])
        self.assertIn("import-export", payload["error"])

    def test_slash_command_reports_status_and_brief(self):
        text = self.ctx.commands["health"]("")
        self.assertIn("hermes-health", text)
        brief = self.ctx.commands["health"]("brief 7")
        self.assertIn("HEALTH BRIEF", brief)

    def test_timezone_setting_is_honoured(self):
        ctx = FakeCtx({"db_path": self.db, "timezone": "Asia/Tokyo"})
        self.package.register(ctx)
        payload = json.loads(ctx.tools["health_status"]["handler"]({}))
        self.assertEqual(payload["timezone"], "Asia/Tokyo")


class PluginManifestTests(unittest.TestCase):
    def test_manifest_declares_every_tool_it_registers(self):
        import re

        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        declared = set(re.findall(r"^  - (health_\w+)$", text, flags=re.MULTILINE))
        package = load_plugin_package()
        ctx = FakeCtx({"db_path": os.path.join(tempfile.mkdtemp(), "health.db")})
        package.register(ctx)
        self.assertEqual(declared, set(ctx.tools))

    def test_manifest_requires_and_metadata(self):
        import re

        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: hermes-health", text)
        self.assertIn("license: MIT", text)
        self.assertIn("requires_hermes:", text)
        self.assertIn("config_schema:", text)
        self.assertTrue(re.search(r"manifest_version: 2", text))


if __name__ == "__main__":
    unittest.main()

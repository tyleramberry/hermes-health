"""The ingest endpoint, exercised over real HTTP on a real socket."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests import ROOT  # noqa: F401

from hermes_health import server, store

SHORTCUTS_BODY = {
    "steps": "2026-11-07T08:00:00-07:00 | 800\n2026-11-07T09:00:00-07:00 | 450",
    "resting_heart_rate": "2026-11-07T06:00:00-07:00 | 51",
    "sleep": "2026-11-06T23:00:00-07:00 | 2026-11-07T02:00:00-07:00 | Core",
}

HAE_BODY = {
    "data": {
        "metrics": [
            {"name": "step_count", "units": "count",
             "data": [{"date": "2026-11-07 10:00:00 -0700", "qty": 700}]},
            {"name": "active_energy", "units": "Cal",
             "data": [{"date": "2026-11-07 10:00:00 -0700", "qty": 300}]},
        ],
        "workouts": [
            {"name": "Running", "start": "2026-11-07 06:30:00 -0700", "end": "2026-11-07 07:00:00 -0700",
             "activeEnergyBurned": {"qty": 400, "units": "kcal"}}
        ],
    }
}


class HttpTestCase(unittest.TestCase):
    token = ""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.dir.name, "health.db")
        cls.state = server.Store(cls.db)
        handler = server.make_handler(cls.state, cls.token)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.dir.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, payload, token=None, raw=None):
        body = raw if raw is not None else json.dumps(payload)
        request = urllib.request.Request(self.url(path), data=body.encode("utf-8"),
                                        headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return self._send(request)

    def get(self, path, token=None):
        request = urllib.request.Request(self.url(path))
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return self._send(request)

    def _send(self, request):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


class IngestTests(HttpTestCase):
    def test_shortcuts_body_is_stored_and_receipted(self):
        # A distinct hour so this test does not depend on the order the others ran in.
        body = {
            "steps": "2026-11-07T21:00:00-07:00 | 800\n2026-11-07T22:00:00-07:00 | 450",
            "resting_heart_rate": "2026-11-07T20:00:00-07:00 | 51",
            "sleep": "2026-11-05T23:00:00-07:00 | 2026-11-06T02:00:00-07:00 | Core",
        }
        status, response, _ = self.post("/ingest", body)
        self.assertEqual(status, 200)
        receipt = json.loads(response)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["stored"], 4)  # 2 steps + 1 resting HR + 1 sleep span
        self.assertEqual(receipt["metrics"]["steps"], 2)
        self.assertEqual(receipt["sleep_spans"], 1)
        self.assertEqual(receipt["error_count"], 0)

    def test_reposting_the_same_body_stores_nothing_new(self):
        self.post("/ingest", SHORTCUTS_BODY)
        _status, body, _ = self.post("/ingest", SHORTCUTS_BODY)
        receipt = json.loads(body)
        self.assertEqual(receipt["stored"], 0)
        self.assertEqual(receipt["duplicates"], 4)

    def test_health_auto_export_body_is_stored(self):
        status, body, _ = self.post("/ingest", HAE_BODY)
        self.assertEqual(status, 200)
        receipt = json.loads(body)
        self.assertEqual(receipt["workouts"], 1)
        self.assertEqual(receipt["metrics"]["steps"], 1)

    def test_ingest_alias_paths(self):
        self.assertEqual(self.post("/ingest/shortcuts", {"steps": 10})[0], 200)
        self.assertEqual(self.post("/ingest/hae", HAE_BODY)[0], 200)
        self.assertEqual(self.post("/ingest/", {"steps": 11})[0], 200)

    def test_bad_json_is_a_400_shaped_receipt_not_a_crash(self):
        status, body, _ = self.post("/ingest", None, raw="{oops")
        self.assertEqual(status, 200)  # the delivery was received; the receipt says why it stored nothing
        receipt = json.loads(body)
        self.assertEqual(receipt["stored"], 0)
        self.assertTrue(receipt["error_count"])
        self.assertEqual(self.get("/health")[0], 200)  # server still healthy

    def test_source_override_and_header(self):
        status, body, _ = self.post("/ingest?source=whoop", {"steps": 42})
        self.assertEqual(status, 200)
        conn = store.connect(self.db)
        try:
            row = conn.execute("SELECT source FROM samples_raw WHERE metric='steps' ORDER BY ts DESC LIMIT 1"
                               ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["source"], "whoop")

    def test_unknown_post_path_is_404(self):
        self.assertEqual(self.post("/nope", {"steps": 1})[0], 404)


class ReadEndpointTests(HttpTestCase):
    def test_health_reports_status_and_endpoints(self):
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn("/ingest", payload["endpoints"])
        self.assertIn("counts", payload)

    def test_cursor_supports_metric_filter(self):
        self.post("/ingest", {"steps": "2026-11-07T08:00:00-07:00 | 5"})
        _status, body, _ = self.get("/cursor?metrics=steps")
        payload = json.loads(body)
        self.assertIn("steps", payload["metrics"])
        self.assertIn("server_time", payload)

    def test_query_and_metrics_and_summary(self):
        self.post("/ingest", SHORTCUTS_BODY)
        _status, body, _ = self.get("/query?metric=Steps&days=5&end=2026-11-07")
        payload = json.loads(body)
        self.assertEqual(payload["metric"], "steps")
        self.assertTrue(payload["series"])
        self.assertEqual(self.get("/metrics")[0], 200)
        self.assertEqual(self.get("/sleep?days=5")[0], 200)
        self.assertEqual(self.get("/workouts?days=5")[0], 200)
        self.assertEqual(self.get("/status")[0], 200)
        self.assertEqual(self.get("/query")[0], 400)  # metric is required

    def test_dashboard_returns_html(self):
        status, body, headers = self.get("/dashboard?days=5")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("<html", body)
        self.assertIn("Recent workouts", body)
        self.assertIn("Move", body)

    def test_head_and_unknown_get(self):
        self.assertEqual(self.get("/nope")[0], 404)


class AuthTests(unittest.TestCase):
    token = "s3cret-token"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "health.db")
        state = server.Store(self.db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(state, self.token))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def request(self, path, payload=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_missing_token_is_rejected_everywhere(self):
        self.assertEqual(self.request("/ingest", {"steps": 1})[0], 401)
        self.assertEqual(self.request("/health")[0], 401)
        self.assertEqual(self.request("/cursor")[0], 401)

    def test_bearer_token_and_header_variant_are_accepted(self):
        self.assertEqual(self.request("/ingest", {"steps": 1},
                                      {"Authorization": f"Bearer {self.token}"})[0], 200)
        self.assertEqual(self.request("/ingest", {"steps": 2}, {"X-Health-Token": self.token})[0], 200)
        self.assertEqual(self.request("/health", None, {"Authorization": self.token})[0], 200)

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.request("/ingest", {"steps": 1}, {"Authorization": "Bearer nope"})[0], 401)


if __name__ == "__main__":
    unittest.main()

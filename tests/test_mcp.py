"""The MCP server: protocol handling in-process, and a real stdio handshake."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import ROOT  # noqa: F401

from hermes_health import store
from hermes_health.mcp_server import HealthMCP, TOOLS

sys.path.insert(0, str(ROOT))


def seed(path):
    conn = store.connect(path)
    store.write_samples(conn, [
        store.Sample("steps", 1783000000, 4321, source="watch"),
        store.Sample("resting_heart_rate", 1783000000, 51, source="watch"),
    ])
    store.write_sleep(conn, [store.SleepSpan(1783000000 - 25200, 1783000000, "core", "watch")])
    store.write_workouts(conn, [store.Workout("running", 1782990000, 1782993600, source="watch",
                                             energy_kcal=400, distance_m=5000)])
    conn.commit()
    conn.close()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "health.db")
        seed(self.db)
        self.server = HealthMCP(self.db)

    def tearDown(self):
        self.dir.cleanup()

    def test_initialize_reports_capabilities(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        })
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(response["result"]["serverInfo"]["name"], "hermes-health")

    def test_initialize_echoes_a_supported_protocol_and_offers_its_own_otherwise(self):
        supported = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(supported["result"]["protocolVersion"], "2024-11-05")
        unknown = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                      "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(unknown["result"]["protocolVersion"], "2025-06-18")

    def test_initialized_notification_gets_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_has_valid_schemas(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = response["result"]["tools"]
        self.assertEqual(len(tools), len(TOOLS))
        names = {tool["name"] for tool in tools}
        for expected in ("health_summary", "health_query", "health_sleep", "health_workouts",
                         "health_metrics", "health_status"):
            self.assertIn(expected, names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("description", tool)

    def test_tool_calls_return_json_text(self):
        for name, args in (
            ("health_status", {}),
            ("health_summary", {"days": 7}),
            ("health_query", {"metric": "Steps", "days": 30}),
            ("health_sleep", {"days": 7}),
            ("health_workouts", {"days": 30}),
            ("health_metrics", {"search": "heart"}),
        ):
            response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                           "params": {"name": name, "arguments": args}})
            self.assertFalse(response["result"]["isError"], f"{name} errored")
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(payload, f"{name} returned nothing")

    def test_tool_errors_are_reported_inside_the_result(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                       "params": {"name": "health_query", "arguments": {}}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("metric is required", response["result"]["content"][0]["text"])

    def test_unknown_tool_and_method(self):
        unknown_tool = self.server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                           "params": {"name": "health_nope", "arguments": {}}})
        self.assertTrue(unknown_tool["result"]["isError"])
        unknown_method = self.server.handle({"jsonrpc": "2.0", "id": 6, "method": "does/not/exist"})
        self.assertEqual(unknown_method["error"]["code"], -32601)

    def test_ping_and_empty_capability_lists(self):
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"})["result"], {})
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 8,
                                             "method": "resources/list"})["result"]["resources"], [])

    def test_workout_activity_filter(self):
        response = self.server.call_tool("health_workouts", {"days": 400, "activity": "Running"})
        payload = json.loads(response["content"][0]["text"])
        self.assertEqual(payload["n"], 1)
        filtered = json.loads(self.server.call_tool("health_workouts",
                                                    {"days": 400, "activity": "cycling"})["content"][0]["text"])
        self.assertEqual(filtered["n"], 0)


class StdioTests(unittest.TestCase):
    """A real subprocess handshake: this is how another harness will use it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "health.db")
        seed(self.db)
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "health_mcp.py"), "--db", self.db],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def tearDown(self):
        self.process.kill()
        self.process.wait(timeout=10)
        self.dir.cleanup()

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        self.assertTrue(line, "server closed stdout without answering")
        return json.loads(line)

    def test_full_handshake_and_tool_call(self):
        init = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "hermes-health")
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0",
                                             "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertGreaterEqual(len(listing["result"]["tools"]), 6)
        call = self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "health_status", "arguments": {}}})
        payload = json.loads(call["result"]["content"][0]["text"])
        self.assertGreater(payload["counts"]["samples_raw"], 0)

    def test_garbage_line_gets_a_parse_error_and_the_server_keeps_going(self):
        self.process.stdin.write("this is not json\n")
        self.process.stdin.flush()
        error = json.loads(self.process.stdout.readline())
        self.assertEqual(error["error"]["code"], -32700)
        response = self.send({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_missing_store_does_not_crash_the_server(self):
        self.process.kill()
        self.process.wait(timeout=10)
        missing = os.path.join(self.dir.name, "does-not-exist", "health.db")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "health_mcp.py"), "--db", missing],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        call = self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "health_status", "arguments": {}}})
        payload = json.loads(call["result"]["content"][0]["text"])
        # It creates an empty store rather than failing; the point is that it answers.
        self.assertIn("counts", payload)


if __name__ == "__main__":
    unittest.main()

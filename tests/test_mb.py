import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("mb", Path(__file__).resolve().parents[1] / "mb.py")
mb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mb)


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode("utf-8"))


def http_error(code):
    return urllib.error.HTTPError("https://metabase.example.com/api/database", code,
                                  "private server detail", {}, io.BytesIO(b"private response body"))


class MetabaseTests(unittest.TestCase):
    def client(self, responses, **auth):
        client = mb.Client("https://metabase.example.com/metabase", **(auth or {"api_key": "unit-test-key"}))
        client.opener = FakeOpener(responses)
        return client

    def test_query_cli_resolves_each_database_and_saves_nested_json(self):
        responses = []
        for table_id in (201, 202):
            responses.extend([
                {"tables": [{"id": table_id, "name": "layout_task", "schema": "public"}]},
                {"fields": [{"id": 301, "name": "id"}, {"id": 302, "name": "input_json"}]},
                {"status": "completed", "data": {"cols": [{"name": "id"}, {"name": "input_json"}],
                                                     "rows": [["task-example", json.dumps(json.dumps({"ok": True}))]]}},
            ])
        client = self.client(responses)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "results" / "task.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(mb.Client, "from_env", return_value=client), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = mb.main(["get", "task-example", "--db", "101,102", "--schema", "public",
                                  "--fields", "id,input_json", "--limit", "3", "--out", str(output)])
            self.assertEqual(status, 0)
            rows = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([r["_database_id"] for r in rows], [101, 102])
            self.assertEqual(rows[0]["input_json"], {"ok": True})
            self.assertNotIn('"ok": true', stdout.getvalue())
            self.assertIn("not an exhaustive count", stderr.getvalue())
        requests = client.opener.requests
        self.assertEqual(len(requests), 6)
        for index, table_id, database_id in ((2, 201, 101), (5, 202, 102)):
            request = requests[index]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://metabase.example.com/metabase/api/dataset")
            self.assertEqual(payload["database"], database_id)
            self.assertEqual(payload["query"]["source-table"], table_id)
            self.assertEqual(payload["query"]["limit"], 3)
            self.assertEqual(payload["query"]["filter"], ["=", ["field", 301, None], "task-example"])
            self.assertEqual(request.get_header("X-api-key"), "unit-test-key")

    def test_deps_handles_optional_columns_without_assuming_all_exist(self):
        client = self.client([
            {"tables": [{"id": 201, "name": "jobs"}]},
            {"fields": [{"id": 301, "name": "id"}, {"id": 302, "name": "chain_id"}]},
            {"status": "completed", "data": {"cols": [], "rows": []}},
        ])
        args = argparse.Namespace(cmd="deps", table="jobs", schema=None, arg="parent-example",
                                  dependency_fields="missing,chain_id", fields="id", limit=5,
                                  order_by=None, json_fields="")
        self.assertEqual(mb.query_rows(client, 101, args, None), [])
        query = json.loads(client.opener.requests[-1].data)["query"]
        self.assertEqual(query["filter"], ["=", ["field", 302, None], "parent-example"])

    def test_ambiguous_table_requires_schema(self):
        client = self.client([{"tables": [{"id": 201, "name": "jobs", "schema": "a"},
                                           {"id": 202, "name": "jobs", "schema": "b"}]}])
        with self.assertRaises(mb.QueryError):
            client.table(101, "jobs")
        self.assertEqual(len(client.opener.requests), 1)

    def test_password_session_renews_once_and_stays_in_memory(self):
        client = self.client([{"id": "unit-session-1"}, http_error(401), {"id": "unit-session-2"}, {"data": []}],
                             username="unit-user", password="unit-password")
        self.assertEqual(client.request("/api/database"), {"data": []})
        calls = client.opener.requests
        self.assertEqual([r.get_method() for r in calls], ["POST", "GET", "POST", "GET"])
        self.assertEqual(calls[-1].get_header("X-metabase-session"), "unit-session-2")
        self.assertEqual(json.loads(calls[0].data), {"username": "unit-user", "password": "unit-password"})

    def test_repeated_401_is_bounded_and_does_not_disclose_details(self):
        client = self.client([{"id": "unit-session-1"}, http_error(401), {"id": "unit-session-2"}, http_error(401)],
                             username="unit-user", password="unit-password")
        with self.assertRaises(mb.QueryError) as caught:
            client.request("/api/database")
        self.assertEqual(len(client.opener.requests), 4)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("unit-password", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_api_key_failure_never_falls_back_to_password(self):
        client = self.client([http_error(401)], api_key="unit-test-key", username="unit-user", password="unit-password")
        with self.assertRaises(mb.QueryError):
            client.request("/api/database")
        self.assertEqual(len(client.opener.requests), 1)
        self.assertEqual(client.opener.requests[0].get_method(), "GET")

    def test_mutations_native_sql_and_external_urls_blocked_before_network(self):
        client = self.client([])
        cases = [
            ("/api/card", {"name": "changed"}),
            ("/api/card/1/query", {}),
            ("/api/dataset", {"database": 101, "type": "native", "native": {"query": "SELECT 1"}}),
            ("https://other.example.com/api/database", None),
            ("/api/database/1/metadata?unexpected=1", None),
        ]
        for path, payload in cases:
            with self.subTest(path=path), self.assertRaises(mb.QueryError):
                client.request(path, payload)
        self.assertEqual(client.opener.requests, [])

    def test_remote_http_and_credential_urls_rejected_but_loopback_allowed(self):
        for url in ("http://metabase.example.com", "https://unit-user:unit-password@metabase.example.com",
                    "https://metabase.example.com?key=unit-test-key"):
            with self.subTest(url=url), self.assertRaises(mb.QueryError):
                mb.Client(url, api_key="unit-test-key")
        self.assertEqual(mb.Client("http://127.0.0.1:8765", api_key="unit-test-key").base, "http://127.0.0.1:8765")
        self.assertIsNone(mb.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example.com"))

    def test_api_key_does_not_load_a_stale_credentials_file(self):
        env = {"METABASE_URL": "https://metabase.example.com", "METABASE_API_KEY": "unit-test-key",
               "METABASE_CREDENTIALS_FILE": "missing-private-file.json"}
        with patch.dict(os.environ, env, clear=True), patch.object(Path, "read_text", side_effect=AssertionError("file accessed")):
            self.assertEqual(mb.Client.from_env().api_key, "unit-test-key")

    def test_unknown_field_and_filter_object_fail_before_query(self):
        for value in ({"f": "missing"}, {"f": "id", "extra": True}, {"native": "SELECT 1"}):
            with self.subTest(value=value), self.assertRaises(mb.QueryError):
                mb.resolve_filter(value, {"id": 301})
        self.assertEqual(mb.parse_json(json.dumps(json.dumps({"n": 1}))), {"n": 1})
        self.assertEqual(mb.parse_json("plain text"), "plain text")

    def test_cli_rejects_missing_database_and_unbounded_limit_without_auth(self):
        with patch.object(mb.Client, "from_env", side_effect=AssertionError("auth accessed")), patch.dict(os.environ, {}, clear=True):
            for argv in (["get", "task-example"], ["get", "--db", "101"], ["query", "--db", "101", "--limit", "0"],
                         ["query", "--db", "101", "--limit", "1001"]):
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    mb.main(argv)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

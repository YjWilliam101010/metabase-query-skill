#!/usr/bin/env python3
"""Read-only Metabase discovery and bounded MBQL queries. Python 3.10+."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class QueryError(Exception):
    """An error safe to show without response bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base, api_key=None, username=None, password=None):
        parts = urllib.parse.urlsplit(base)
        if (parts.scheme not in ("https", "http") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise QueryError("METABASE_URL must be an HTTP(S) URL without credentials, query or fragment.")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise QueryError("Use HTTPS for a remote instance; HTTP is supported only on loopback.")
        self.base = base.rstrip("/")
        self.api_key, self.username, self.password = api_key, username, password
        if not api_key and not (username and password):
            raise QueryError("Set METABASE_API_KEY, or a username/password pair via environment or credentials file.")
        self.session = None
        self.opener = urllib.request.build_opener(NoRedirect())
        self.metadata = {}

    @classmethod
    def from_env(cls):
        base = os.environ.get("METABASE_URL", "")
        if not base:
            raise QueryError("Set METABASE_URL to your instance URL, including any /metabase subpath.")
        api_key = os.environ.get("METABASE_API_KEY")
        username = os.environ.get("METABASE_USERNAME")
        password = os.environ.get("METABASE_PASSWORD")
        credentials = os.environ.get("METABASE_CREDENTIALS_FILE")
        if not api_key and not (username and password) and credentials:
            try:
                data = json.loads(Path(credentials).expanduser().read_text(encoding="utf-8-sig"))
                username, password = data["username"], data["password"]
            except (OSError, ValueError, KeyError, TypeError):
                raise QueryError("Cannot read credentials file; expected username and password strings.") from None
        if not api_key and not (isinstance(username, str) and isinstance(password, str)):
            raise QueryError("Username and password must be strings.")
        return cls(base, api_key, username, password)

    def _send(self, path, payload=None, headers=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if payload is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise QueryError("Request failed: check connectivity, TLS and instance API compatibility.") from None

    def _login(self):
        data = self._send("/api/session", {"username": self.username, "password": self.password})
        self.session = data.get("id") if isinstance(data, dict) else None
        if not isinstance(self.session, str) or not self.session:
            raise QueryError("Login did not return a session ID.")

    def request(self, path, payload=None):
        if payload is None:
            if not re.fullmatch(r"/api/(database(?:/\d+/metadata)?|table/\d+/query_metadata|card)", path):
                raise QueryError("Endpoint is outside the read-only allowlist.")
        elif (path != "/api/dataset" or payload.get("type") != "query"
              or type(payload.get("database")) is not int
              or type(payload.get("query", {}).get("source-table")) is not int):
            raise QueryError("Only structured, table-based MBQL dataset queries are supported.")
        for attempt in range(2):
            try:
                if self.api_key:
                    headers = {"X-API-Key": self.api_key}
                else:
                    if self.session is None:
                        self._login()
                    headers = {"X-Metabase-Session": self.session}
                return self._send(path, payload, headers)
            except urllib.error.HTTPError as error:
                if error.code == 401 and not self.api_key and self.session and attempt == 0:
                    self.session = None
                    continue
                raise QueryError(f"Metabase returned HTTP {error.code}; check access, configuration and API version.") from None
        raise QueryError("Authentication retry exhausted.")

    def table(self, database, name, schema=None):
        key = (database, name, schema)
        if key not in self.metadata:
            tables = self.request(f"/api/database/{database}/metadata").get("tables", [])
            matches = [t for t in tables if t.get("name") == name
                       and (schema is None or t.get("schema") == schema)]
            if len(matches) != 1:
                raise QueryError("Table not found or ambiguous; inspect tables and specify --schema.")
            table = matches[0]
            data = self.request(f"/api/table/{table['id']}/query_metadata")
            self.metadata[key] = {
                "table": table["id"],
                "fields": {f["name"]: f["id"] for f in data.get("fields", [])
                           if f.get("parent_id") is None and f.get("active", True)},
            }
        return self.metadata[key]


def resolve_filter(node, fields):
    if isinstance(node, dict):
        if set(node) != {"f"} or not isinstance(node["f"], str):
            raise QueryError('Filter objects must be field references such as {"f": "id"}.')
        if node["f"] not in fields:
            raise QueryError(f"Unknown field: {node['f']}")
        return ["field", fields[node["f"]], None]
    if isinstance(node, list):
        return [resolve_filter(item, fields) for item in node]
    return node


def parse_json(value):
    for _ in range(10):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def query_rows(client, database, args, filter_):
    meta = client.table(database, args.table, args.schema)
    fields = meta["fields"]
    if args.cmd == "get":
        filter_ = ["=", {"f": args.id_field}, args.arg]
    elif args.cmd == "deps":
        present = [f.strip() for f in args.dependency_fields.split(",") if f.strip() in fields]
        if not present:
            raise QueryError("No dependency fields found; specify --dependency-fields for your schema.")
        clauses = [["=", {"f": name}, args.arg] for name in present]
        filter_ = clauses[0] if len(clauses) == 1 else ["or", *clauses]
    names = list(fields) if args.fields == "all" else [f.strip() for f in args.fields.split(",")]
    if any(name not in fields for name in names):
        raise QueryError("A selected field is missing; inspect fields and update --fields.")
    query = {"source-table": meta["table"], "fields": [["field", fields[n], None] for n in names],
             "limit": args.limit}
    if filter_ is not None:
        query["filter"] = resolve_filter(filter_, fields)
    if args.order_by:
        name = args.order_by.lstrip("-")
        if name not in fields:
            raise QueryError("The --order-by field does not exist.")
        query["order-by"] = [["desc" if args.order_by.startswith("-") else "asc", ["field", fields[name], None]]]
    result = client.request("/api/dataset", {"database": database, "type": "query", "query": query})
    if result.get("status") != "completed":
        raise QueryError("Query did not complete; inspect instance query logs for the server error.")
    columns = [c["name"] for c in result["data"]["cols"]]
    rows = []
    for values in result["data"]["rows"]:
        row = dict(zip(columns, values))
        for field in args.json_fields.split(","):
            if field.strip() in row:
                row[field.strip()] = parse_json(row[field.strip()])
        row["_database_id"] = database
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["databases", "tables", "fields", "cards", "get", "deps", "query"])
    parser.add_argument("arg", nargs="?", help="Task ID, card-name substring, or JSON MBQL filter")
    parser.add_argument("--db", default=os.environ.get("METABASE_DATABASES"), help="Comma-separated database IDs; no built-in IDs")
    parser.add_argument("--table", default=os.environ.get("METABASE_TABLE", "layout_task"))
    parser.add_argument("--schema")
    parser.add_argument("--fields", default="all", help="Comma-separated column names, or all")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows per database (1-1000)")
    parser.add_argument("--id-field", default="id")
    parser.add_argument("--dependency-fields", default="depend_task_id,task_chain_id,spawned_by_task_id")
    parser.add_argument("--json-fields", default="input_json,output_json")
    parser.add_argument("--order-by", help="Field name; use --order-by=-created_at for descending")
    parser.add_argument("--filter-file", type=Path, help="UTF-8 JSON filter; avoids shell quoting problems")
    parser.add_argument("--out", type=Path, help="Save full JSON results; terminal shows bounded row summaries")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    if args.cmd in ("get", "deps") and not args.arg:
        parser.error("get/deps requires a task ID")
    databases = []
    if args.cmd not in ("databases", "cards"):
        try:
            databases = [int(v.strip()) for v in (args.db or "").split(",")]
            if not databases or any(v <= 0 for v in databases):
                raise ValueError()
        except ValueError:
            parser.error("provide positive database IDs via --db or METABASE_DATABASES")
    if args.filter_file and (args.cmd != "query" or args.arg):
        parser.error("--filter-file is for query and cannot be combined with an inline filter")
    try:
        filter_ = None
        if args.cmd == "query":
            filter_ = json.loads(args.filter_file.read_text(encoding="utf-8-sig")) if args.filter_file else (json.loads(args.arg) if args.arg else None)
            if filter_ is not None and not isinstance(filter_, list):
                raise QueryError("A filter must be a JSON array.")
        client = Client.from_env()
        if args.cmd in ("databases", "cards"):
            result = client.request("/api/database" if args.cmd == "databases" else "/api/card")
            items = result.get("data", []) if isinstance(result, dict) else result
            keys = ("id", "name", "engine") if args.cmd == "databases" else ("id", "name", "database_id")
            rows = [{k: item.get(k) for k in keys} for item in items
                    if not args.arg or args.arg.casefold() in item.get("name", "").casefold()]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif args.cmd in ("tables", "fields"):
            rows = []
            for database in databases:
                if args.cmd == "tables":
                    items = client.request(f"/api/database/{database}/metadata").get("tables", [])
                    rows.extend({"_database_id": database, **{k: t.get(k) for k in ("id", "name", "schema")}} for t in items)
                else:
                    meta = client.table(database, args.table, args.schema)
                    rows.append({"_database_id": database, **meta})
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            rows = [row for db in databases for row in query_rows(client, db, args, filter_)]
            for row in rows:
                print(" | ".join(f"{k}=<{type(v).__name__}, {len(json.dumps(v, ensure_ascii=False))} chars>"
                                 if isinstance(v, (dict, list)) or k in args.json_fields.split(",")
                                 else f"{k}={str(v)[:80]}" for k, v in row.items()))
            print(f"{len(rows)} row(s); limit {args.limit} per database. This is not an exhaustive count.", file=sys.stderr)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except QueryError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error: cannot read input JSON or write output; check file access and JSON syntax.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

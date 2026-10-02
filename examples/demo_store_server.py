"""A tiny in-memory REST API that matches ``examples/store_catalog.json``.

It exists so Rest Tester can be tried without any backend of your own:

    python examples/demo_store_server.py            # serves http://127.0.0.1:8765

Then load ``examples/store_catalog.json`` from Settings -> API catalog. The three
services (Catalog, Orders, Customers) are mounted under ``/catalog/v1``,
``/orders/v1`` and ``/customers/v1`` on the same port.

The server understands the filter grammar (``Name__op:=value`` joined by ``;``
or ``|``), comma-separated sort with a trailing ``-`` for descending,
``PageSize`` with ``ContinuationToken`` paging, ``Fields`` projection, RFC 6902
JSON Patch, and ``HEAD`` counts. It uses only the standard library. State is held
in memory and reset on restart.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

RESOURCES = {
    "catalog": ("Product", "Category"),
    "orders": ("Order",),
    "customers": ("Customer",),
}
REQUIRED = {
    "Product": ("Name", "Sku", "CategoryId", "Price", "Status"),
    "Category": ("Name",),
    "Order": ("CustomerId", "PaymentMethod", "ShippingAddress", "Lines"),
    "Customer": ("FirstName", "LastName", "Email"),
}
ENUMS = {
    ("Product", "Status"): ("Draft", "Active", "Discontinued"),
    ("Order", "Status"): ("Pending", "Paid", "Shipped", "Delivered", "Cancelled"),
    ("Order", "PaymentMethod"): ("Card", "PayPal", "BankTransfer", "GiftCard"),
    ("Customer", "Tier"): ("Standard", "Silver", "Gold", "Platinum"),
}

LOCK = threading.Lock()
DATA: dict[str, dict[int, dict[str, Any]]] = {}
NEXT_ID: dict[str, int] = {}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed() -> None:
    rng = random.Random(7)
    base = datetime(2025, 1, 6, tzinfo=timezone.utc)
    stamp = lambda days: (base + timedelta(days=days)).isoformat().replace("+00:00", "Z")  # noqa: E731
    categories = ["Audio", "Cameras", "Computers", "Gaming", "Home", "Phones", "Wearables"]
    DATA["Category"] = {
        i: {"Id": i, "Name": name, "ParentId": None, "IsVisible": True, "SortOrder": i * 10,
            "CreatedDate": stamp(i), "ModifiedDate": stamp(i)}
        for i, name in enumerate(categories, start=1)
    }
    adjectives = ["Wireless", "Compact", "Pro", "Smart", "Ultra", "Classic", "Portable", "Studio"]
    nouns = ["Headphones", "Speaker", "Camera", "Laptop", "Controller", "Lamp", "Phone", "Watch", "Monitor"]
    DATA["Product"] = {}
    for i in range(1, 49):
        name = f"{rng.choice(adjectives)} {rng.choice(nouns)} {rng.randint(2, 9)}"
        DATA["Product"][i] = {
            "Id": i, "Name": name, "Sku": f"SKU-{1000 + i}", "Description": f"Demo product number {i}.",
            "CategoryId": rng.randint(1, len(categories)), "Price": round(rng.uniform(9, 1499), 2),
            "Stock": rng.randint(0, 250), "Status": rng.choice(["Active"] * 5 + ["Draft", "Discontinued"]),
            "IsFeatured": rng.random() < 0.25, "ReleaseDate": stamp(-rng.randint(30, 900)),
            "Tags": rng.sample(["new", "sale", "bestseller", "eco", "bundle", "limited"], 2),
            "Dimensions": {"WidthCm": rng.randint(5, 60), "HeightCm": rng.randint(5, 60),
                           "DepthCm": rng.randint(2, 40), "WeightKg": round(rng.uniform(0.1, 9), 2)},
            "CreatedDate": stamp(i), "ModifiedDate": stamp(i + rng.randint(0, 30)),
        }
    first = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Ivy", "Lucas", "Nora", "Omar", "Priya", "Sam"]
    last = ["Smith", "Garcia", "Chen", "Patel", "Müller", "Rossi", "Kim", "Silva", "Novak", "Brown"]
    DATA["Customer"] = {}
    for i in range(1, 31):
        fn, ln = rng.choice(first), rng.choice(last)
        DATA["Customer"][i] = {
            "Id": i, "FirstName": fn, "LastName": ln, "Email": f"{fn}.{ln}{i}@example.com".lower(),
            "Phone": f"+1-555-01{i:02d}", "Tier": rng.choice(["Standard"] * 3 + ["Silver", "Gold", "Platinum"]),
            "IsSubscribed": rng.random() < 0.6, "BirthDate": None,
            "Addresses": [{"Id": 1, "Line1": f"{rng.randint(1, 999)} Market Street", "Line2": None,
                           "City": rng.choice(["Austin", "Toronto", "Berlin", "Lyon", "Pune", "Sydney"]),
                           "PostalCode": f"{rng.randint(10000, 99999)}", "Country": rng.choice(["US", "CA", "DE"])}],
            "CreatedDate": stamp(i), "ModifiedDate": stamp(i),
        }
    DATA["Order"] = {}
    for i in range(1, 61):
        lines = [{"ProductId": rng.randint(1, 48), "Quantity": rng.randint(1, 3)} for _ in range(rng.randint(1, 3))]
        total = sum(DATA["Product"][line["ProductId"]]["Price"] * line["Quantity"] for line in lines)
        customer = DATA["Customer"][rng.randint(1, 30)]
        status = rng.choice(["Pending", "Paid", "Paid", "Shipped", "Delivered", "Delivered", "Cancelled"])
        placed = stamp(rng.randint(0, 120))
        DATA["Order"][i] = {
            "Id": i, "Number": f"SO-{24000 + i}", "CustomerId": customer["Id"], "Status": status,
            "PaymentMethod": rng.choice(["Card", "Card", "PayPal", "BankTransfer", "GiftCard"]),
            "Total": round(total, 2), "CouponCode": None, "Notes": None,
            "ShippingAddress": {k: v for k, v in customer["Addresses"][0].items() if k != "Id"},
            "Lines": lines, "PlacedDate": placed,
            "ShippedDate": placed if status in ("Shipped", "Delivered") else None,
            "CreatedDate": placed, "ModifiedDate": placed,
        }
    for name, rows in DATA.items():
        NEXT_ID[name] = max(rows) + 1


# -- filter / sort ------------------------------------------------------------------------------

CONDITION = re.compile(r"^(?P<field>\w+)__(?P<op>\w+):=(?P<value>.*)$", re.S)


def _coerce(sample: Any, raw: str) -> Any:
    if isinstance(sample, bool):
        return raw.strip().lower() in ("true", "1", "yes")
    if isinstance(sample, int):
        try:
            return int(raw)
        except ValueError:
            return float(raw)
    if isinstance(sample, float):
        return float(raw)
    return raw


def _lookup(row: dict[str, Any], field: str) -> Any:
    for key, value in row.items():
        if key.lower() == field.lower():
            return value
    raise KeyError(field)


def _matches(row: dict[str, Any], condition: str) -> bool:
    match = CONDITION.match(condition.strip())
    if not match:
        raise ValueError(f"Invalid filter condition '{condition}'. Use Name__op:=value.")
    field, op, raw = match["field"], match["op"].lower(), match["value"]
    try:
        actual = _lookup(row, field)
    except KeyError:
        raise ValueError(f"'{field}' is not a filterable field.") from None
    sample = actual if actual is not None else ""
    values = [_coerce(sample, part) for part in raw.split(",")] if op in ("in", "nin", "bt") else [_coerce(sample, raw)]
    value = values[0]
    text, needle = str(actual or "").lower(), str(value).lower()
    if op == "eq":
        return (str(actual).lower() == needle) if isinstance(actual, str) else actual == value
    if op == "neq":
        return not _matches(row, f"{field}__eq:={raw}")
    if op == "ct":
        return needle in text
    if op == "nct":
        return needle not in text
    if op == "sw":
        return text.startswith(needle)
    if op == "ew":
        return text.endswith(needle)
    if op in ("in", "nin"):
        hit = any((str(actual).lower() == str(v).lower()) if isinstance(actual, str) else actual == v for v in values)
        return hit if op == "in" else not hit
    if actual is None:
        return False
    if op == "gt":
        return actual > value
    if op == "lt":
        return actual < value
    if op == "gte":
        return actual >= value
    if op == "lte":
        return actual <= value
    if op == "bt":
        if len(values) != 2:
            raise ValueError("'bt' needs exactly two values: low,high.")
        return values[0] <= actual <= values[1]
    raise ValueError(f"Unsupported operator '{op}'.")


def _filter(rows: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    expression = expression.strip()
    if not expression:
        return rows
    # ';' (AND) binds tighter than '|' (OR).
    groups = [group.split(";") for group in expression.split("|")]
    return [row for row in rows if any(all(_matches(row, c) for c in group if c.strip()) for group in groups)]


def _sort(rows: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    for key in reversed([part.strip() for part in expression.split(",") if part.strip()]):
        descending = key.endswith("-")
        field = key.rstrip("-")
        try:
            rows.sort(key=lambda row: (_lookup(row, field) is None, _lookup(row, field)), reverse=descending)
        except KeyError:
            raise ValueError(f"'{field}' is not a sortable field.") from None
    return rows


def _project(row: dict[str, Any], fields: str) -> dict[str, Any]:
    wanted = {name.strip().lower() for name in fields.split(",") if name.strip()}
    return {k: v for k, v in row.items() if k.lower() in wanted} if wanted else row


def _patch(row: dict[str, Any], operations: Any) -> dict[str, Any]:
    if not isinstance(operations, list):
        raise ValueError("PATCH body must be a JSON Patch operation array.")
    for operation in operations:
        op = str(operation.get("op", "")).lower()
        name = str(operation.get("path", "")).strip("/").split("/")[0]
        key = next((k for k in row if k.lower() == name.lower()), None)
        if not name or key == "Id":
            raise ValueError(f"Cannot patch path '{operation.get('path')}'.")
        if op in ("replace", "add"):
            row[key or name] = operation.get("value")
        elif op == "remove" and key:
            row[key] = None
        else:
            raise ValueError(f"Unsupported patch op '{op}'.")
    return row


def _validate(resource: str, body: Any, partial: bool = False) -> list[str]:
    if not isinstance(body, dict):
        return ["Body must be a JSON object."]
    errors = [] if partial else [f"'{name}' is required." for name in REQUIRED[resource] if body.get(name) in (None, "")]
    for (owner, field), allowed in ENUMS.items():
        if owner == resource and body.get(field) not in (None, *allowed):
            errors.append(f"'{field}' must be one of: {', '.join(allowed)}.")
    if resource == "Product" and isinstance(body.get("Price"), (int, float)) and body["Price"] < 0:
        errors.append("'Price' must be zero or greater.")
    return errors


# -- HTTP -----------------------------------------------------------------------------------------

ROUTE = re.compile(r"^/(?P<service>\w+)/v1/(?P<resource>\w+)(?:/(?P<id>[^/]+))?(?:/(?P<sub>\w+))?/?$", re.I)


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoStore/1.0"
    latency_ms = (5, 40)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        if not self.server.quiet:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # Every verb goes through one dispatcher.
    def do_GET(self) -> None: self._dispatch("GET")  # noqa: E704
    def do_HEAD(self) -> None: self._dispatch("HEAD")  # noqa: E704
    def do_POST(self) -> None: self._dispatch("POST")  # noqa: E704
    def do_PUT(self) -> None: self._dispatch("PUT")  # noqa: E704
    def do_PATCH(self) -> None: self._dispatch("PATCH")  # noqa: E704
    def do_DELETE(self) -> None: self._dispatch("DELETE")  # noqa: E704

    def _send(self, status: int, body: Any = None, headers: dict[str, str] | None = None,
              content_type: str = "application/json") -> None:
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body, indent=2).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", f"{random.getrandbits(48):012x}")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)

    def _problem(self, status: int, title: str, errors: list[str] | None = None) -> None:
        self._send(status, {"type": "about:blank", "title": title, "status": status, "errors": errors or []},
                   content_type="application/problem+json")

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw.strip() else None

    def _dispatch(self, method: str) -> None:
        low, high = self.latency_ms
        time.sleep(random.uniform(low, high) / 1000)
        url = urlsplit(self.path)
        match = ROUTE.match(unquote(url.path))
        if not match or match["service"].lower() not in RESOURCES:
            return self._problem(404, "Not found", [f"No route for {method} {url.path}."])
        resource = next((r for r in RESOURCES[match["service"].lower()] if r.lower() == match["resource"].lower()), None)
        if resource is None:
            return self._problem(404, "Not found", [f"Unknown resource '{match['resource']}'."])
        query = {k.lower(): v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        try:
            body = self._body() if method in ("POST", "PUT", "PATCH") else None
        except json.JSONDecodeError as exc:
            return self._problem(400, "Malformed JSON", [str(exc)])
        try:
            with LOCK:
                self._route(method, resource, match["id"], match["sub"], query, body)
        except ValueError as exc:
            self._problem(400, "Invalid request", [str(exc)])

    def _route(self, method: str, resource: str, raw_id: str | None, sub: str | None,
               query: dict[str, str], body: Any) -> None:
        rows = DATA[resource]
        if raw_id is None:
            if method in ("GET", "HEAD"):
                return self._list(resource, query, head=method == "HEAD")
            if method == "POST":
                errors = _validate(resource, body)
                if errors:
                    return self._problem(400, "Validation failed", errors)
                new_id = NEXT_ID[resource]
                NEXT_ID[resource] += 1
                row = {"Id": new_id, **body, "CreatedDate": _now(), "ModifiedDate": _now()}
                if resource == "Order":
                    row.setdefault("Number", f"SO-{24000 + new_id}")
                    row["Status"] = "Pending"
                    row["Total"] = round(sum(rows_price(line) for line in body.get("Lines", [])), 2)
                    row["PlacedDate"] = _now()
                rows[new_id] = row
                return self._send(201, row, {"Location": f"{self.path.rstrip('/')}/{new_id}"})
            return self._problem(405, "Method not allowed")
        if not raw_id.isdigit():
            return self._problem(400, "Invalid request", [f"'{raw_id}' is not a valid integer id."])
        row = rows.get(int(raw_id))
        if row is None:
            return self._problem(404, "Not found", [f"{resource} {raw_id} does not exist."])
        if sub:
            return self._sub_resource(method, resource, row, sub.lower(), query, body)
        if method == "GET":
            return self._send(200, _project(row, query.get("fields", "")))
        if method == "PUT":
            errors = _validate(resource, body)
            if errors:
                return self._problem(400, "Validation failed", errors)
            row.update({k: v for k, v in body.items() if k != "Id"}, ModifiedDate=_now())
            return self._send(200, row)
        if method == "PATCH":
            patched = _patch(dict(row), body)
            errors = _validate(resource, patched, partial=True)
            if errors:
                return self._problem(400, "Validation failed", errors)
            row.update(patched, ModifiedDate=_now())
            return self._send(200, row)
        if method == "DELETE":
            del rows[int(raw_id)]
            return self._send(204)
        return self._problem(405, "Method not allowed")

    def _sub_resource(self, method: str, resource: str, row: dict[str, Any], sub: str,
                      query: dict[str, str], body: Any) -> None:
        if resource == "Order" and sub == "status" and method == "PUT":
            status = (body or {}).get("Status")
            if status not in ENUMS[("Order", "Status")]:
                return self._problem(400, "Validation failed", ["'Status' is not a valid order status."])
            row.update(Status=status, ModifiedDate=_now())
            if body.get("TrackingNumber"):
                row["TrackingNumber"] = body["TrackingNumber"]
            return self._send(200, row)
        if resource == "Order" and sub == "invoice" and method == "GET":
            text = f"INVOICE {row['Number']}\nTotal: {row['Total']:.2f}\nStatus: {row['Status']}\n"
            return self._send(200, text.encode(), {
                "Content-Disposition": f'attachment; filename="{row["Number"]}.txt"'}, "text/plain; charset=utf-8")
        if resource == "Customer" and sub == "addresses":
            if method == "GET":
                return self._send(200, row.get("Addresses", []))
            if method == "POST":
                missing = [f"'{n}' is required." for n in ("Line1", "City", "PostalCode", "Country")
                           if not (body or {}).get(n)]
                if missing:
                    return self._problem(400, "Validation failed", missing)
                address = {"Id": len(row.setdefault("Addresses", [])) + 1, **body}
                row["Addresses"].append(address)
                return self._send(201, address)
        if resource == "Product" and sub == "image" and method == "GET":
            return self._send(200, _swatch(row["Id"]), content_type="image/svg+xml")
        return self._problem(404, "Not found", [f"No sub-resource '{sub}'."])

    def _list(self, resource: str, query: dict[str, str], head: bool) -> None:
        rows = _sort(_filter(list(DATA[resource].values()), query.get("filter", "")), query.get("sort", ""))
        if head:
            return self._send(200, None, {"X-Total-Count": str(len(rows))})
        try:
            size = max(1, min(int(query.get("pagesize") or 25), 500))
            start = int(query.get("continuationtoken") or 0)
        except ValueError:
            raise ValueError("PageSize and ContinuationToken must be integers.") from None
        page = rows[start:start + size]
        token = str(start + size) if start + size < len(rows) else None
        self._send(200, {"Values": [_project(r, query.get("fields", "")) for r in page],
                         "ContinuationToken": token, "Count": len(rows)})


def rows_price(line: dict[str, Any]) -> float:
    product = DATA["Product"].get(int(line.get("ProductId") or 0))
    return (product or {}).get("Price", 0) * int(line.get("Quantity") or 1)


def _swatch(seed: int) -> bytes:
    hue = (seed * 47) % 360
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'
            f'<rect width="240" height="160" rx="16" fill="hsl({hue},65%,55%)"/>'
            f'<text x="120" y="92" font-family="sans-serif" font-size="28" fill="#fff" '
            f'text-anchor="middle">#{seed}</text></svg>').encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--quiet", action="store_true", help="do not log each request")
    args = parser.parse_args()
    _seed()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.quiet = args.quiet  # type: ignore[attr-defined]
    print(f"Demo Store API on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    for service in RESOURCES:
        print(f"  {service:<10} http://{args.host}:{args.port}/{service}/v1/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

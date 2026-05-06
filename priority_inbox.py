"""
Stage 6 — Priority Inbox: top-N notifications by type weight then recency.

Stdlib only. Fetches from the evaluation API (or --demo), parses payloads,
maintains a fixed-size min-heap + seen_ids, prints a sorted snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import heappush, heappushpop
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_TYPE_WEIGHTS = {"placement": 300, "result": 200, "event": 100}


@dataclass(frozen=True)
class Notification:
    notification_id: str
    notification_type: str
    message: str
    timestamp: datetime
    raw: Dict[str, Any]

    @property
    def timestamp_epoch(self) -> int:
        return int(self.timestamp.timestamp())


class PriorityInbox:
    """Top-N via min-heap; rank key (type_weight, timestamp_epoch)."""

    def __init__(self, top_n: int, type_weights: Dict[str, int]) -> None:
        if top_n <= 0:
            raise ValueError("top_n must be > 0")
        self.top_n = top_n
        self.type_weights = type_weights
        self._heap: List[Tuple[Tuple[int, int], str, Notification]] = []
        self._seen_ids: set[str] = set()

    def _rank(self, n: Notification) -> Tuple[int, int]:
        w = self.type_weights.get(n.notification_type.lower(), 0)
        return (w, n.timestamp_epoch)

    def ingest(self, notification: Notification) -> bool:
        if notification.notification_id in self._seen_ids:
            return False
        self._seen_ids.add(notification.notification_id)
        rank = self._rank(notification)
        entry = (rank, notification.notification_id, notification)
        if len(self._heap) < self.top_n:
            heappush(self._heap, entry)
            return True
        if rank > self._heap[0][0]:
            heappushpop(self._heap, entry)
            return True
        return False

    def snapshot(self) -> List[Notification]:
        return [e[2] for e in sorted(self._heap, key=lambda e: e[0], reverse=True)]


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp format: {value}")


def parse_notification(payload: Dict[str, Any]) -> Notification:
    nid = str(payload.get("ID") or payload.get("id") or "").strip()
    ntype = str(payload.get("Type") or payload.get("type") or "").strip()
    message = str(payload.get("Message") or payload.get("message") or "").strip()
    ts_raw = str(payload.get("Timestamp") or payload.get("timestamp") or "").strip()
    if not nid:
        raise ValueError("missing notification id")
    if not ntype:
        raise ValueError("missing notification type")
    if not ts_raw:
        raise ValueError("missing notification timestamp")
    return Notification(
        notification_id=nid,
        notification_type=ntype,
        message=message,
        timestamp=parse_timestamp(ts_raw),
        raw=payload,
    )


def _http_post_json(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_bearer_token(auth_url: str) -> str:
    if os.getenv("EVAL_ACCESS_TOKEN"):
        return os.environ["EVAL_ACCESS_TOKEN"]
    keys = [
        ("email", ("EVAL_EMAIL",)),
        ("name", ("EVAL_NAME",)),
        ("rollNo", ("EVAL_ROLL_NO", "EVAL_ROLLNo")),
        ("accessCode", ("EVAL_ACCESS_CODE", "EVAL_accessCode")),
        ("clientID", ("EVAL_CLIENT_ID", "EVAL_clientID")),
        ("clientSecret", ("EVAL_CLIENT_SECRET", "EVAL_clientSecret")),
    ]
    body: Dict[str, str] = {}
    missing: List[str] = []
    for k, envs in keys:
        v = next((os.environ[e] for e in envs if os.getenv(e)), "")
        if v:
            body[k] = v
        else:
            missing.append("/".join(envs))
    if len(body) != len(keys):
        raise RuntimeError("Set EVAL_ACCESS_TOKEN or: " + ", ".join(missing))
    resp = _http_post_json(auth_url, body)
    token = str(resp.get("access_token") or resp.get("accessToken") or resp.get("token") or "").strip()
    if not token:
        raise RuntimeError("auth response missing access_token")
    return token


def fetch_notifications(api_url: str, bearer_token: str) -> Tuple[List[Dict[str, Any]], float]:
    req = Request(api_url, headers={"Authorization": f"Bearer {bearer_token}"}, method="GET")
    t0 = time.perf_counter()
    with urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    ms = (time.perf_counter() - t0) * 1000.0
    items = raw.get("notifications", [])
    if not isinstance(items, list):
        raise RuntimeError("invalid response: notifications must be a list")
    return items, ms


def demo_notifications() -> List[Dict[str, Any]]:
    return [
        {"ID": "d146095a-0d86-4a34-9e69-3900a14576bc", "Type": "Result", "Message": "mid-sem", "Timestamp": "2026-04-22 17:51:30"},
        {"ID": "b283218f-ea5a-4b7c-93a9-1f2f240d64b0", "Type": "Placement", "Message": "CSX Corporation hiring", "Timestamp": "2026-04-22 17:51:18"},
        {"ID": "81589ada-0ad3-4f77-9554-f52fb558e09d", "Type": "Event", "Message": "farewell", "Timestamp": "2026-04-22 17:51:06"},
        {"ID": "00c5513a-142b-4bbc-8678-eefec65e1ede", "Type": "Result", "Message": "mid-sem", "Timestamp": "2026-04-22 17:50:54"},
        {"ID": "8a7412bd-6065-4d09-8501-a37f11cc848b", "Type": "Placement", "Message": "Advanced Micro Devices Inc. hiring", "Timestamp": "2026-04-22 17:49:42"},
    ]


def print_top(items: List[Notification], top_n: int) -> None:
    print(f"\nPriority Inbox Top {top_n}")
    print("=" * 90)
    for i, item in enumerate(items, 1):
        print(
            f"{i:>2}. [{item.notification_type:<9}] "
            f"{item.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"{item.notification_id} | {item.message}"
        )
    if not items:
        print("(no notifications yet)")
    print("=" * 90)


def run(args: argparse.Namespace) -> int:
    inbox = PriorityInbox(args.n, DEFAULT_TYPE_WEIGHTS)
    token = None if args.demo else get_bearer_token(args.auth_url)
    while True:
        try:
            if args.demo:
                rows = demo_notifications()
                elapsed_ms = 0.0
            else:
                rows, elapsed_ms = fetch_notifications(args.api_url, token)

            added = 0
            for raw in rows:
                try:
                    if inbox.ingest(parse_notification(raw)):
                        added += 1
                except ValueError as e:
                    print(f"skip invalid notification: {e}", file=sys.stderr)

            print(f"Fetched {len(rows)} notifications in {elapsed_ms:.2f} ms | added to top-{args.n}: {added}")
            print_top(inbox.snapshot(), args.n)
        except HTTPError as e:
            print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
            return 1
        except URLError as e:
            print(f"Network error: {e.reason}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            return 1

        if args.once:
            break
        time.sleep(args.poll_interval)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Stage 6 Priority Inbox (top-N heap).")
    p.add_argument("--n", type=int, default=10, help="Heap size / top-N to keep.")
    p.add_argument("--api-url", default="http://20.207.122.201/evaluation-service/notifications")
    p.add_argument("--auth-url", default="http://20.207.122.201/evaluation-service/auth")
    p.add_argument("--poll-interval", type=int, default=20, help="Seconds between polls if not --once.")
    p.add_argument("--once", action="store_true", help="Single fetch then exit.")
    p.add_argument("--demo", action="store_true", help="Use built-in sample rows (no network).")
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

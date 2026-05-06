"""
Priority Inbox evaluator for placement notifications.

Purpose:
- Fetch protected notifications from the evaluation API.
- Rank notifications using weighted priority (placement > result > event)
  combined with recency.
- Maintain top-N efficiently as new notifications continue to arrive.

Dependencies:
- Python standard library only (no external algorithm libraries).
- Optional `--screenshot` uses Windows PowerShell + System.Drawing (no pip packages).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import heappush, heappushpop
from typing import Any, Dict, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class _TeeTextIO:
    """Writes the same text to multiple text streams (e.g. console + log file)."""

    def __init__(self, *writers: Any) -> None:
        self._writers = writers

    def write(self, data: str) -> int:
        total = 0
        for writer in self._writers:
            total = writer.write(data)
            writer.flush()
        return total

    def flush(self) -> None:
        for writer in self._writers:
            writer.flush()

    @property
    def encoding(self) -> str:
        return getattr(self._writers[0], "encoding", "utf-8")

    def isatty(self) -> bool:
        first = self._writers[0]
        return bool(getattr(first, "isatty", lambda: False)())


DEFAULT_TYPE_WEIGHTS = {
    "placement": 300,
    "result": 200,
    "event": 100,
}


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
    """Maintains top-N using a fixed-size min-heap."""

    def __init__(self, top_n: int, type_weights: Dict[str, int]) -> None:
        if top_n <= 0:
            raise ValueError("top_n must be > 0")
        self.top_n = top_n
        self.type_weights = type_weights
        self._heap: List[Tuple[Tuple[int, int], str, Notification]] = []
        self._seen_ids: set[str] = set()

    def _rank(self, notification: Notification) -> Tuple[int, int]:
        weight = self.type_weights.get(notification.notification_type.lower(), 0)
        return (weight, notification.timestamp_epoch)

    def ingest(self, notification: Notification) -> bool:
        """
        Ingests a new notification.
        Returns True if this notification is currently inside top-N.
        """
        if notification.notification_id in self._seen_ids:
            return False

        self._seen_ids.add(notification.notification_id)
        rank = self._rank(notification)
        heap_entry = (rank, notification.notification_id, notification)

        if len(self._heap) < self.top_n:
            heappush(self._heap, heap_entry)
            return True

        if rank > self._heap[0][0]:
            heappushpop(self._heap, heap_entry)
            return True

        return False

    def snapshot(self) -> List[Notification]:
        sorted_entries = sorted(self._heap, key=lambda entry: entry[0], reverse=True)
        return [entry[2] for entry in sorted_entries]


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp format: {value}")


def parse_notification(payload: Dict[str, Any]) -> Notification:
    notification_id = str(payload.get("ID") or payload.get("id") or "").strip()
    notification_type = str(payload.get("Type") or payload.get("type") or "").strip()
    message = str(payload.get("Message") or payload.get("message") or "").strip()
    timestamp_raw = str(payload.get("Timestamp") or payload.get("timestamp") or "").strip()

    if not notification_id:
        raise ValueError("missing notification id")
    if not notification_type:
        raise ValueError("missing notification type")
    if not timestamp_raw:
        raise ValueError("missing notification timestamp")

    return Notification(
        notification_id=notification_id,
        notification_type=notification_type,
        message=message,
        timestamp=parse_timestamp(timestamp_raw),
        raw=payload,
    )


def _http_post_json(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(body).encode("utf-8")
    req = Request(url, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as resp:
        data = resp.read().decode("utf-8")
        return json.loads(data)


def get_bearer_token(auth_url: str) -> str:
    pre_existing = os.getenv("EVAL_ACCESS_TOKEN")
    if pre_existing:
        return pre_existing

    env_aliases = {
        "email": ("EVAL_EMAIL",),
        "name": ("EVAL_NAME",),
        "rollNo": ("EVAL_ROLL_NO", "EVAL_ROLLNo"),
        "accessCode": ("EVAL_ACCESS_CODE", "EVAL_accessCode"),
        "clientID": ("EVAL_CLIENT_ID", "EVAL_clientID"),
        "clientSecret": ("EVAL_CLIENT_SECRET", "EVAL_clientSecret"),
    }

    resolved_inputs: Dict[str, str] = {}
    missing: List[str] = []
    for payload_key, candidates in env_aliases.items():
        value = ""
        for env_key in candidates:
            env_value = os.getenv(env_key)
            if env_value:
                value = env_value
                break
        if not value:
            missing.append("/".join(candidates))
            continue
        resolved_inputs[payload_key] = value

    if missing:
        raise RuntimeError(
            "missing token inputs. Set EVAL_ACCESS_TOKEN or these env vars: "
            + ", ".join(missing)
        )

    auth_payload = {
        "email": resolved_inputs["email"],
        "name": resolved_inputs["name"],
        "rollNo": resolved_inputs["rollNo"],
        "accessCode": resolved_inputs["accessCode"],
        "clientID": resolved_inputs["clientID"],
        "clientSecret": resolved_inputs["clientSecret"],
    }
    token_response = _http_post_json(auth_url, auth_payload)
    access_token = str(
        token_response.get("access_token")
        or token_response.get("accessToken")
        or token_response.get("token")
        or ""
    ).strip()
    if not access_token:
        raise RuntimeError("auth response missing access_token")
    return access_token


def fetch_notifications(api_url: str, bearer_token: str) -> Tuple[List[Dict[str, Any]], float]:
    headers = {"Authorization": f"Bearer {bearer_token}"}
    req = Request(api_url, headers=headers, method="GET")
    start = time.perf_counter()
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    parsed = json.loads(raw)
    notifications = parsed.get("notifications", [])
    if not isinstance(notifications, list):
        raise RuntimeError("invalid response: notifications must be a list")
    return notifications, elapsed_ms


def demo_notifications() -> List[Dict[str, Any]]:
    return [
        {
            "ID": "d146095a-0d86-4a34-9e69-3900a14576bc",
            "Type": "Result",
            "Message": "mid-sem",
            "Timestamp": "2026-04-22 17:51:30",
        },
        {
            "ID": "b283218f-ea5a-4b7c-93a9-1f2f240d64b0",
            "Type": "Placement",
            "Message": "CSX Corporation hiring",
            "Timestamp": "2026-04-22 17:51:18",
        },
        {
            "ID": "81589ada-0ad3-4f77-9554-f52fb558e09d",
            "Type": "Event",
            "Message": "farewell",
            "Timestamp": "2026-04-22 17:51:06",
        },
        {
            "ID": "00c5513a-142b-4bbc-8678-eefec65e1ede",
            "Type": "Result",
            "Message": "mid-sem",
            "Timestamp": "2026-04-22 17:50:54",
        },
        {
            "ID": "8a7412bd-6065-4d09-8501-a37f11cc848b",
            "Type": "Placement",
            "Message": "Advanced Micro Devices Inc. hiring",
            "Timestamp": "2026-04-22 17:49:42",
        },
    ]


def capture_primary_screen_png(path: str) -> None:
    """
    Saves a PNG of the primary display (full screen) using PowerShell on Windows.
    """
    if sys.platform != "win32":
        raise RuntimeError("screenshot capture is only supported on Windows")

    path_abs = os.path.abspath(path)
    parent = os.path.dirname(path_abs)
    if parent:
        os.makedirs(parent, exist_ok=True)

    script = r"""
$path = $env:PRIORITY_INBOX_SCREENSHOT_PATH
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
"""
    env = os.environ.copy()
    env["PRIORITY_INBOX_SCREENSHOT_PATH"] = path_abs
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip() or "unknown error"
        raise RuntimeError(f"screenshot command failed (exit {completed.returncode}): {detail}")


def print_top_notifications(items: Sequence[Notification], top_n: int) -> None:
    print(f"\nPriority Inbox Top {top_n}")
    print("=" * 90)
    for idx, item in enumerate(items, start=1):
        print(
            f"{idx:>2}. [{item.notification_type:<9}] "
            f"{item.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"{item.notification_id} | {item.message}"
        )
    if not items:
        print("(no notifications yet)")
    print("=" * 90)


def run_loop(args: argparse.Namespace) -> int:
    inbox = PriorityInbox(top_n=args.n, type_weights=DEFAULT_TYPE_WEIGHTS)
    bearer_token = None

    if not args.demo:
        bearer_token = get_bearer_token(args.auth_url)

    while True:
        try:
            if args.demo:
                fetched = demo_notifications()
                elapsed_ms = 0.0
            else:
                fetched, elapsed_ms = fetch_notifications(args.api_url, bearer_token)

            accepted = 0
            for raw in fetched:
                try:
                    parsed = parse_notification(raw)
                    if inbox.ingest(parsed):
                        accepted += 1
                except ValueError as parse_error:
                    print(f"skip invalid notification: {parse_error}", file=sys.stderr)

            print(
                f"Fetched {len(fetched)} notifications "
                f"in {elapsed_ms:.2f} ms | added to top-{args.n}: {accepted}"
            )
            print_top_notifications(inbox.snapshot(), args.n)
        except HTTPError as http_err:
            print(f"HTTP error: {http_err.code} {http_err.reason}", file=sys.stderr)
            return 1
        except URLError as url_err:
            print(f"Network error: {url_err.reason}", file=sys.stderr)
            return 1
        except Exception as err:  # broad on purpose for CLI stability
            print(f"Unexpected error: {err}", file=sys.stderr)
            return 1

        if args.once:
            break
        time.sleep(args.poll_interval)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Priority Inbox top-N notifications in Python.")
    parser.add_argument("--n", type=int, default=10, help="Top-N unread notifications to maintain.")
    parser.add_argument(
        "--api-url",
        default="http://20.207.122.201/evaluation-service/notifications",
        help="Notification API URL.",
    )
    parser.add_argument(
        "--auth-url",
        default="http://20.207.122.201/evaluation-service/auth",
        help="Auth API URL for bearer token.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between polls when running continuously.",
    )
    parser.add_argument("--once", action="store_true", help="Fetch once and exit.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run without network using built-in sample notifications.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Also write stdout and stderr to this file (UTF-8). Example: priority_inbox_output.txt",
    )
    parser.add_argument(
        "--screenshot",
        nargs="?",
        const="priority_inbox_screenshot.png",
        default=None,
        metavar="PATH",
        help="After a successful run, save primary-screen PNG (Windows only). "
        "If the flag is given with no path, uses priority_inbox_screenshot.png.",
    )
    parser.add_argument(
        "--screenshot-delay",
        type=float,
        default=0.75,
        metavar="SECONDS",
        help="Wait this long before capturing the screen (default: 0.75).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    restore_stdout = sys.stdout
    restore_stderr = sys.stderr
    log_file = None
    if args.output:
        log_file = open(args.output, "w", encoding="utf-8", newline="\n")
        sys.stdout = _TeeTextIO(restore_stdout, log_file)
        sys.stderr = _TeeTextIO(restore_stderr, log_file)
    try:
        exit_code = run_loop(args)
        if exit_code == 0 and args.screenshot is not None:
            delay = max(0.0, float(args.screenshot_delay))
            if delay:
                time.sleep(delay)
            try:
                capture_primary_screen_png(args.screenshot)
            except Exception as shot_err:
                print(f"Screenshot error: {shot_err}", file=sys.stderr)
                return 1
            abs_shot = os.path.abspath(args.screenshot)
            print(f"\nSaved screenshot: {abs_shot}", flush=True)
        return exit_code
    finally:
        sys.stdout = restore_stdout
        sys.stderr = restore_stderr
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())

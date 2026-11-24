#!/usr/bin/env python3
"""Serve the last good blog feed and refresh it in the background."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Callable
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from build_feed import FeedError, MAX_BYTES, build_feed


LOG = logging.getLogger("blog-rss")
REFRESH_SECONDS = 30 * 60


@dataclass(frozen=True)
class Snapshot:
    body: bytes
    etag: str
    built_at: float


def validate_feed(body: bytes, now: float) -> Snapshot:
    if not body or len(body) > MAX_BYTES:
        raise FeedError("RSS snapshot is empty or oversized")
    try:
        root = ET.fromstring(body)
        channel = root.find("channel")
        if root.tag != "rss" or root.get("version") != "2.0" or channel is None:
            raise ValueError("not RSS 2.0")
        for name in ("title", "link", "description"):
            if not channel.findtext(name):
                raise ValueError("missing channel metadata")
        built = parsedate_to_datetime(channel.findtext("lastBuildDate", ""))
        if built.tzinfo is None:
            raise ValueError("lastBuildDate lacks timezone")
        timestamp = built.astimezone(timezone.utc).timestamp()
        if timestamp > now + 300:
            raise ValueError("lastBuildDate is in the future")
        items = channel.findall("item")
        guids = [item.findtext("guid") for item in items]
        if not items or not all(guids) or len(set(guids)) != len(guids):
            raise ValueError("missing or duplicated items")
        for item in items:
            if not item.findtext("title") or not item.findtext("link"):
                raise ValueError("missing item metadata")
            if item.find("pubDate") is not None:
                published = parsedate_to_datetime(item.findtext("pubDate", ""))
                if published.tzinfo is None:
                    raise ValueError("pubDate lacks timezone")
    except (ET.ParseError, ValueError, TypeError, OverflowError) as error:
        raise FeedError("RSS snapshot failed validation") from error
    return Snapshot(body, '"' + sha256(body).hexdigest() + '"', timestamp)


def start_background(callback: Callable[[], None]) -> None:
    Thread(target=callback, name="rss-refresh", daemon=True).start()


class FeedCache:
    def __init__(self, seed: bytes, *, refresher: Callable[[], bytes] = build_feed,
                 clock: Callable[[], float] = time.time,
                 schedule: Callable[[Callable[[], None]], None] = start_background) -> None:
        self._clock = clock
        self._snapshot = validate_feed(seed, clock())
        self._refresher = refresher
        self._schedule = schedule
        self._lock = Lock()
        self._last_attempt = self._snapshot.built_at
        self._refreshing = False

    def current(self) -> Snapshot:
        with self._lock:
            snapshot = self._snapshot
            due = not self._refreshing and self._clock() - self._last_attempt >= REFRESH_SECONDS
            if due:
                self._refreshing = True
                self._last_attempt = self._clock()
        if due:
            try:
                self._schedule(self._refresh)
            except Exception:
                with self._lock:
                    self._refreshing = False
                LOG.warning("RSS refresh could not start; retaining last good feed")
        return snapshot

    def _refresh(self) -> None:
        try:
            snapshot = validate_feed(self._refresher(), self._clock())
            with self._lock:
                self._snapshot = snapshot
            LOG.info("RSS refresh completed")
        except Exception:
            LOG.warning("RSS refresh failed; retaining last good feed")
        finally:
            with self._lock:
                self._refreshing = False


def etag_matches(value: str, etag: str) -> bool:
    return any(tag.strip().removeprefix("W/") in (etag, "*") for tag in value.split(","))


class FeedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], cache: FeedCache):
        self.feed_cache = cache
        super().__init__(address, FeedHandler)


class FeedHandler(BaseHTTPRequestHandler):
    server_version = "InfrawatchRSS/1.0"

    def do_GET(self) -> None:
        self._serve(head=False)

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def _serve(self, *, head: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._respond(200, b"ok\n", head=head, content_type="text/plain; charset=utf-8", cache_control="no-store")
            return
        if path not in ("/", "/blog-rss.xml"):
            self._respond(404, b"Not found\n", head=head, content_type="text/plain; charset=utf-8", cache_control="no-store")
            return
        snapshot = self.server.feed_cache.current()
        headers = {"ETag": snapshot.etag}
        if etag_matches(self.headers.get("If-None-Match", ""), snapshot.etag):
            self._respond(304, b"", head=True, headers=headers)
        else:
            self._respond(200, snapshot.body, head=head, headers=headers)

    def _respond(self, status: int, body: bytes, *, head: bool,
                 content_type: str = "application/rss+xml; charset=utf-8",
                 cache_control: str = "public, max-age=300", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head and status != 304:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Avoid logging query strings, headers, or arbitrary request content.
        return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    seed_path = Path(__file__).resolve().parent / "published" / "feed.xml"
    try:
        cache = FeedCache(seed_path.read_bytes())
        port = int(os.environ.get("PORT", "8080"))
        server = FeedServer(("0.0.0.0", port), cache)
    except (OSError, ValueError, FeedError):
        raise SystemExit("RSS server could not start with its validated feed seed")
    LOG.info("RSS server ready on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

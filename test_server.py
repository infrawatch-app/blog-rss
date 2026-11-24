from datetime import datetime, timezone
from http.client import HTTPConnection
from threading import Thread
import unittest

import build_feed
import serve_feed as server


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def snapshot_xml(title="Current public post", built=NOW):
    return build_feed.rss_xml([build_feed.Post(title, "https://infrawatch.com/blog/public-post",
                                              "Public description", None)], built)


class CacheTests(unittest.TestCase):
    def test_stale_seed_refresh_is_single_flight_and_atomically_replaces(self):
        now = [NOW.timestamp() + 1801]
        callbacks = []
        updated = snapshot_xml("Updated post", datetime.fromtimestamp(now[0], timezone.utc))
        cache = server.FeedCache(snapshot_xml(), clock=lambda: now[0], schedule=callbacks.append,
                                 refresher=lambda: updated)
        threads = [Thread(target=cache.current) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(cache.current().body, snapshot_xml())
        callbacks.pop()()
        self.assertEqual(cache.current().body, updated)
        self.assertEqual(callbacks, [])
        now[0] += 1800
        cache.current()
        self.assertEqual(len(callbacks), 1)

    def test_failed_or_invalid_refresh_preserves_good_cache_and_throttles_retries(self):
        for failure in (RuntimeError("private failure details"), b"not RSS"):
            now = [NOW.timestamp() + 1800]
            callbacks = []
            def refresh():
                if isinstance(failure, Exception):
                    raise failure
                return failure
            original = snapshot_xml()
            cache = server.FeedCache(original, clock=lambda: now[0], schedule=callbacks.append, refresher=refresh)
            before = cache.current()
            with self.assertLogs(server.LOG, level="WARNING") as logs:
                callbacks.pop()()
            self.assertNotIn("private failure details", "".join(logs.output))
            self.assertEqual(cache.current(), before)
            self.assertEqual(cache.current().body, original)
            self.assertEqual(callbacks, [])
            now[0] += 1799
            cache.current()
            self.assertEqual(callbacks, [])
            now[0] += 1
            cache.current()
            self.assertEqual(len(callbacks), 1)

    def test_invalid_seed_and_invalid_last_build_date_are_rejected(self):
        for body in (b"", b"not RSS", snapshot_xml().replace(b"Tue, 24 Nov 2026 12:00:00 GMT", b"Invalid Date")):
            with self.assertRaises(build_feed.FeedError):
                server.FeedCache(body, clock=lambda: NOW.timestamp())

    def test_fresh_seed_does_not_refresh(self):
        callbacks = []
        cache = server.FeedCache(snapshot_xml(), clock=lambda: NOW.timestamp() + 1799, schedule=callbacks.append)
        cache.current()
        self.assertEqual(callbacks, [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.body = snapshot_xml()
        cache = server.FeedCache(self.body, clock=lambda: NOW.timestamp())
        self.server = server.FeedServer(("127.0.0.1", 0), cache)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_get_and_proxy_stripped_root_serve_rss_with_stable_etag(self):
        status, headers, body = self.request("GET", "/blog-rss.xml")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.body)
        self.assertEqual(headers["Content-Type"], "application/rss+xml; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "public, max-age=300")
        root = self.request("GET", "/")
        self.assertEqual(root[0], 200)
        self.assertEqual(root[1]["ETag"], headers["ETag"])
        self.assertEqual(root[2], self.body)

    def test_head_has_correct_length_and_no_body(self):
        status, headers, body = self.request("HEAD", "/blog-rss.xml")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(self.body))
        self.assertEqual(body, b"")

    def test_conditional_get_and_head_support_weak_matching_and_lists(self):
        etag = self.request("GET", "/")[1]["ETag"]
        for method in ("GET", "HEAD"):
            for value in (etag, f'"other", W/{etag}', "*"):
                status, headers, body = self.request(method, "/", {"If-None-Match": value})
                self.assertEqual(status, 304)
                self.assertEqual(body, b"")
                self.assertEqual(headers["ETag"], etag)
        self.assertEqual(self.request("GET", "/", {"If-None-Match": '"different"'})[0], 200)

    def test_health_and_unknown_path(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok\n"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/unknown")[0], 404)


if __name__ == "__main__":
    unittest.main()

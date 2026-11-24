from datetime import datetime, timezone
from email.message import Message
import io
import json
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import build_feed as feed


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def article(slug, *, title="Latest AOF research", description="Research & evidence <updated>",
            published="2026-11-23", noindex=False):
    url = f"{feed.ORIGIN}/blog/{slug}"
    node = {"@type": "BlogPosting", "url": url, "headline": title, "description": description,
            "author": {"@type": "Organization", "name": "Infrawatch Research"},
            "image": {"url": "https://framerusercontent.com/image.png"}}
    if published is not None:
        node["datePublished"] = published
    robots = '<meta name="robots" content="noindex, follow">' if noindex else ""
    return feed.Page(f'<html><head>{robots}<link rel="canonical" href="{url}">'
                     '<script type="application/ld+json">' + json.dumps({"@graph": [node]})
                     + '</script></head><body></body></html>', url)


def fixture_fetch(index, pages):
    def fetch(url):
        if url == feed.BLOG_URL:
            return feed.Page(index, url)
        return pages[url]
    return fetch


class DiscoveryTests(unittest.TestCase):
    def test_only_listed_same_origin_one_level_posts_and_deduplicates(self):
        html = '''<a href="./blog/aof?utm_source=x#post">one</a>
        <a href="https://infrawatch.com/blog/aof/">duplicate</a>
        <a href="/blog/second">two</a><a href="/blog">index</a>
        <a href="https://elsewhere.test/blog/hidden">foreign</a>
        <a href="http://infrawatch.com/blog/insecure">insecure</a>
        <a href="/blog/one/two">nested</a><a href="/blog/one%2Ftwo">encoded nested</a>
        <a href="https://infrawatch.com:8443/blog/other">port</a>'''
        self.assertEqual(feed.discover_posts(html), [f"{feed.ORIGIN}/blog/aof", f"{feed.ORIGIN}/blog/second"])

    def test_empty_or_noindex_index_fails(self):
        for html in ("<html></html>", '<meta name="robots" content="noindex"><a href="/blog/a">a</a>'):
            with self.assertRaises(feed.FeedError):
                feed.discover_posts(html)


class FeedTests(unittest.TestCase):
    def test_exact_metadata_escaping_dates_stable_guid_and_self_url(self):
        page = article("aof", title="8,105 hosts & 128 intrusions <inside>")
        xml = feed.build_feed(fixture_fetch('<a href="/blog/aof">aof</a>', {page.url: page}), NOW)
        channel = ET.fromstring(xml).find("channel")
        item = channel.find("item")
        self.assertEqual(item.findtext("title"), "8,105 hosts & 128 intrusions <inside>")
        self.assertEqual(item.findtext("description"), "Research & evidence <updated>")
        self.assertEqual(item.findtext("guid"), page.url)
        self.assertEqual(item.find("guid").get("isPermaLink"), "true")
        self.assertEqual(item.findtext("pubDate"), "Mon, 23 Nov 2026 00:00:00 GMT")
        self.assertEqual(item.findtext(f"{{{feed.DC}}}creator"), "Infrawatch Research")
        self.assertEqual(item.find(f"{{{feed.MEDIA}}}content").get("url"), "https://framerusercontent.com/image.png")
        self.assertEqual(channel.find(f"{{{feed.ATOM}}}link").get("href"), feed.FEED_URL)
        self.assertIn(b"&amp;", xml)
        self.assertNotIn(b"Invalid Date", xml)

    def test_noindex_header_meta_and_future_posts_are_excluded(self):
        pages = [article("listed"), article("noindex", noindex=True), article("future", published="2026-11-25"),
                 article("header")]
        pages[-1] = feed.Page(pages[-1].html, pages[-1].url, "googlebot: noindex")
        index = "".join(f'<a href="{p.url}">{p.url}</a>' for p in pages)
        seen = []
        inner = fixture_fetch(index, {p.url: p for p in pages})
        def fetch(url):
            seen.append(url)
            return inner(url)
        xml = feed.build_feed(fetch, NOW)
        self.assertEqual([i.findtext("link") for i in ET.fromstring(xml).findall("./channel/item")], [pages[0].url])
        self.assertNotIn(f"{feed.ORIGIN}/sitemap.xml", seen)

    def test_missing_dates_omitted_and_sorting_preserves_unknown_index_order(self):
        pages = [article("old", published="2025-01-01"), article("undated-a", published=None),
                 article("new", published="2026-11-24"), article("undated-b", published=None)]
        index = "".join(f'<a href="{p.url}">{p.url}</a>' for p in pages)
        items = ET.fromstring(feed.build_feed(fixture_fetch(index, {p.url: p for p in pages}), NOW)).findall("./channel/item")
        self.assertEqual([i.findtext("link") for i in items], [pages[2].url, pages[0].url, pages[1].url, pages[3].url])
        self.assertIsNone(items[2].find("pubDate"))

    def test_malformed_article_or_upstream_failure_fails_entire_build(self):
        for broken in (article("broken", published="not a date"),
                       feed.Page('<script type="application/ld+json">{broken</script>', f"{feed.ORIGIN}/blog/broken"),
                       feed.Page("<html></html>", f"{feed.ORIGIN}/blog/broken")):
            with self.assertRaises(feed.FeedError):
                feed.build_feed(fixture_fetch('<a href="/blog/broken">broken</a>', {broken.url: broken}), NOW)
        def failing_fetch(url):
            if url == feed.BLOG_URL:
                return feed.Page('<a href="/blog/aof">aof</a>', url)
            raise feed.FeedError("upstream failed")
        with self.assertRaisesRegex(feed.FeedError, "upstream failed"):
            feed.build_feed(failing_fetch, NOW)

    def test_no_posts_after_filters_fails(self):
        page = article("aof", noindex=True)
        with self.assertRaises(feed.FeedError):
            feed.build_feed(fixture_fetch('<a href="/blog/aof">aof</a>', {page.url: page}), NOW)


class FetchTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, html, url=feed.BLOG_URL):
            super().__init__(html.encode())
            self.url = url
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def geturl(self):
            return self.url

    def test_empty_200_shell_retries_before_returning_listed_posts(self):
        responses = [self.Response("<html><title>Blog</title></html>"),
                     self.Response('<html><a href="/blog/aof">AOF</a></html>')]
        class Opener:
            def open(self, request, timeout):
                self_test.assertEqual(timeout, 20)
                return responses.pop(0)
        self_test = self
        with patch.object(feed, "build_opener", return_value=Opener()), patch.object(feed.time, "sleep"):
            page = feed.fetch_page(feed.BLOG_URL)
        self.assertEqual(feed.discover_posts(page.html), [f"{feed.ORIGIN}/blog/aof"])
        self.assertEqual(responses, [])

    def test_three_empty_200_responses_fail_with_clear_error(self):
        class Opener:
            def open(self, request, timeout):
                return FetchTests.Response("<html><title>Blog</title></html>")
        with patch.object(feed, "build_opener", return_value=Opener()), patch.object(feed.time, "sleep"):
            with self.assertRaisesRegex(feed.FeedError, "3 attempts.*empty blog index"):
                feed.fetch_page(feed.BLOG_URL)

    def test_security_article_can_mention_cloudflare_challenge_markers(self):
        page = article("aof")
        html = page.html.replace("</body>", "<p>Research mentions cf-chl- challenge markers.</p></body>")
        class Opener:
            def open(self, request, timeout):
                return FetchTests.Response(html, page.url)
        with patch.object(feed, "build_opener", return_value=Opener()):
            result = feed.fetch_page(page.url)
        self.assertEqual(feed.parse_post(result, page.url, NOW).title, "Latest AOF research")


if __name__ == "__main__":
    unittest.main()

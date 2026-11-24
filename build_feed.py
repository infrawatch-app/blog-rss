#!/usr/bin/env python3
"""Build Infrawatch RSS from the listed, publicly published blog posts."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


ORIGIN = "https://infrawatch.com"
BLOG_URL = f"{ORIGIN}/blog"
FEED_URL = f"{ORIGIN}/rss"
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT_SECONDS = 20
DC = "http://purl.org/dc/elements/1.1/"
MEDIA = "http://search.yahoo.com/mrss/"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("dc", DC)
ET.register_namespace("media", MEDIA)
ET.register_namespace("atom", ATOM)


class FeedError(RuntimeError):
    """The public source cannot produce a trustworthy feed."""


@dataclass(frozen=True)
class Page:
    html: str
    url: str
    robots: str = ""


@dataclass(frozen=True)
class Post:
    title: str
    url: str
    description: str
    published: datetime | None
    author: str | None = None
    image: str | None = None


def same_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "infrawatch.com"
                and parsed.port in (None, 443) and parsed.username is None
                and parsed.password is None)
    except ValueError:
        return False


def canonical_post_url(href: str, base: str = BLOG_URL) -> str | None:
    url = urljoin(base, href)
    if not same_origin(url):
        return None
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    match = re.fullmatch(r"/blog/([^/]+)", unquote(path))
    if not match or match.group(1) in (".", ".."):
        return None
    return urlunsplit(("https", "infrawatch.com", path, "", ""))


class PublicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.jsonld: list[str] = []
        self._script: list[str] | None = None
        self._title = False
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        elif tag == "meta":
            key = values.get("property") or values.get("name") or ""
            key = key.lower()
            content = values.get("content") or ""
            self.meta[key] = f"{self.meta.get(key, '')} {content}".strip() if key in ("robots", "googlebot") else content
        elif tag == "link" and "canonical" in (values.get("rel") or "").lower().split():
            self.canonical = values.get("href")
        elif tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._script = []
        elif tag == "title":
            self._title = True

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        if self._title:
            self.title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.jsonld.append("".join(self._script))
            self._script = None
        elif tag == "title":
            self._title = False


def parse_page(html: str) -> PublicPageParser:
    parser = PublicPageParser()
    parser.feed(html)
    parser.close()
    return parser


def is_noindex(directives: str) -> bool:
    return bool(set(re.split(r"[\s,;:]+", directives.lower())) & {"noindex", "none"})


def discover_posts(html: str) -> list[str]:
    parser = parse_page(html)
    if is_noindex(parser.meta.get("robots", "")):
        raise FeedError("Blog index is marked noindex")
    links = dict.fromkeys(url for href in parser.links if (url := canonical_post_url(href)))
    if not links:
        raise FeedError("Blog index contains no listed post links; refusing to publish an empty feed")
    return list(links)


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_origin(newurl):
            raise FeedError("Public blog fetch redirected outside infrawatch.com")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url: str) -> Page:
    if not same_origin(url):
        raise FeedError("Only public infrawatch.com pages may be fetched")
    opener = build_opener(SameOriginRedirects())
    last_error = "unknown error"
    for attempt in range(3):
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; InfrawatchRSS/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Cache-Control": "no-cache",
        })
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise FeedError(f"Public page exceeds 10 MB: {url}")
                if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                    raise FeedError(f"Public page did not return HTML: {url}")
                html = body.decode(response.headers.get_content_charset() or "utf-8")
                parser = parse_page(html)
                title = "".join(parser.title).strip().lower()
                if (title in ("just a moment...", "attention required! | cloudflare")
                        or response.headers.get("cf-mitigated", "").lower() == "challenge"):
                    raise URLError("upstream returned a challenge page")
                if urlsplit(url).path.rstrip("/") == "/blog":
                    if not any(canonical_post_url(href) for href in parser.links):
                        raise URLError("upstream returned an empty blog index")
                elif not is_noindex(" ".join([response.headers.get("X-Robots-Tag", ""),
                                              parser.meta.get("robots", ""), parser.meta.get("googlebot", "")])):
                    try:
                        has_post = any(any(blog_post_nodes(json.loads(source))) for source in parser.jsonld)
                    except (ValueError, TypeError) as error:
                        raise FeedError(f"Malformed JSON-LD on listed post: {url}") from error
                    if not has_post:
                        raise URLError("upstream returned an incomplete article page")
                final_url = response.geturl()
                if not same_origin(final_url):
                    raise FeedError("Public blog fetch left infrawatch.com")
                return Page(html, final_url, response.headers.get("X-Robots-Tag", ""))
        except HTTPError as error:
            if error.code not in (403, 429, 500, 502, 503, 504):
                raise FeedError(f"Public page returned HTTP {error.code}: {url}") from error
            last_error = f"HTTP {error.code}"
        except (URLError, TimeoutError, OSError, UnicodeError) as error:
            last_error = str(error.reason) if isinstance(error, URLError) else type(error).__name__
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    raise FeedError(f"Public page failed after 3 attempts ({last_error}): {url}")


def blog_post_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from blog_post_nodes(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if "BlogPosting" in ([types] if isinstance(types, str) else types):
            yield value
        if "@graph" in value:
            yield from blog_post_nodes(value["@graph"])


def publication_date(value, url: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise FeedError(f"Invalid datePublished metadata: {url}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise FeedError(f"Invalid datePublished metadata: {url}") from error


def author_name(value) -> str | None:
    if isinstance(value, list):
        names = [name for item in value if (name := author_name(item))]
        return ", ".join(dict.fromkeys(names)) or None
    if isinstance(value, dict):
        value = value.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def image_url(value, base: str) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    if isinstance(value, str):
        url = urljoin(base, value)
        if urlsplit(url).scheme == "https":
            return url
    return None


def parse_post(page: Page, requested_url: str, now: datetime) -> Post | None:
    parser = parse_page(page.html)
    robots = " ".join([page.robots, parser.meta.get("robots", ""), parser.meta.get("googlebot", "")])
    if is_noindex(robots):
        return None
    canonical = canonical_post_url(parser.canonical or page.url, page.url)
    if canonical != requested_url:
        raise FeedError(f"Listed post resolved to a different canonical page: {requested_url}")
    nodes = []
    for source in parser.jsonld:
        try:
            nodes.extend(blog_post_nodes(json.loads(source)))
        except (ValueError, TypeError) as error:
            raise FeedError(f"Malformed JSON-LD on listed post: {requested_url}") from error
    matching = [node for node in nodes if canonical_post_url(str(node.get("url", ""))) == canonical]
    if len(matching) == 1:
        metadata = matching[0]
    elif len(nodes) == 1:
        metadata = nodes[0]
    elif nodes:
        raise FeedError(f"Ambiguous BlogPosting metadata: {requested_url}")
    else:
        raise FeedError(f"Missing BlogPosting metadata: {requested_url}")
    title = metadata.get("headline") or parser.meta.get("og:title")
    description = metadata.get("description") or parser.meta.get("description") or ""
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str):
        raise FeedError(f"Invalid article title or description: {requested_url}")
    published = publication_date(metadata.get("datePublished"), requested_url)
    if published and published > now:
        return None
    return Post(title.strip(), canonical, description.strip(), published,
                author_name(metadata.get("author")), image_url(metadata.get("image"), canonical))


def rss_xml(posts: list[Post], now: datetime) -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for tag, value in (("title", "Infrawatch Blog"), ("link", BLOG_URL),
                       ("description", "Research and updates from Infrawatch."),
                       ("language", "en-gb"), ("lastBuildDate", format_datetime(now, usegmt=True)),
                       ("ttl", "60")):
        ET.SubElement(channel, tag).text = value
    ET.SubElement(channel, f"{{{ATOM}}}link", {"href": FEED_URL, "rel": "self", "type": "application/rss+xml"})
    for post in posts:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = post.title
        ET.SubElement(item, "link").text = post.url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = post.url
        ET.SubElement(item, "description").text = post.description
        if post.published:
            ET.SubElement(item, "pubDate").text = format_datetime(post.published, usegmt=True)
        if post.author:
            ET.SubElement(item, f"{{{DC}}}creator").text = post.author
        if post.image:
            ET.SubElement(item, f"{{{MEDIA}}}content", {"url": post.image, "medium": "image"})
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def build_feed(fetch: Callable[[str], Page] = fetch_page, now: datetime | None = None) -> bytes:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    index = fetch(BLOG_URL)
    if is_noindex(index.robots):
        raise FeedError("Blog index is marked noindex")
    urls = discover_posts(index.html)

    def load(url: str) -> Post | None:
        return parse_post(fetch(url), url, now)

    with ThreadPoolExecutor(max_workers=4) as pool:
        posts = [post for post in pool.map(load, urls) if post is not None]
    if not posts:
        raise FeedError("No listed, indexable published posts remain; refusing to publish an empty feed")
    posts.sort(key=lambda post: (post.published is None, -(post.published.timestamp() if post.published else 0)))
    return rss_xml(posts, now)


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Infrawatch RSS</title>
<link rel="alternate" type="application/rss+xml" title="Infrawatch Blog" href="https://infrawatch.com/rss">
</head><body><h1>Infrawatch RSS</h1><p><a href="./feed.xml">Subscribe to the blog feed</a></p>
<p><a href="https://infrawatch.com/blog">Read the blog</a></p></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "public")
    args = parser.parse_args()
    try:
        xml = build_feed()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = args.output_dir / "feed.xml.tmp"
        temporary.write_bytes(xml)
        temporary.replace(args.output_dir / "feed.xml")
        (args.output_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        print(f"Built {len(ET.fromstring(xml).findall('./channel/item'))} listed posts in {args.output_dir}")
    except FeedError as error:
        parser.exit(1, f"RSS build failed: {error}\n")


if __name__ == "__main__":
    main()

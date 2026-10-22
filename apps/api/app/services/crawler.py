"""A polite same-site website crawler for building a knowledge base.

Follows robots.txt (including Crawl-delay and Sitemap lines), starts from the sitemap
when there is one, stays on the starting site, never exceeds the page limit, and
turns each HTML page into plain text. All network access goes through `fetch`
(safe_get by default), so the SSRF rules apply to every request."""

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from app.services.knowledge_extract import html_to_text
from app.services.safe_fetch import USER_AGENT, FetchError, FetchResult, UnsafeURL, safe_get

logger = logging.getLogger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml")
XML_TYPES = ("application/xml", "text/xml", "application/rss+xml")
MAX_PAGE_LIMIT = 200
MAX_CRAWL_DELAY = 5.0
MAX_SITEMAPS = 10
MIN_TEXT_CHARS = 80
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".gz", ".mp3", ".mp4",
    ".mov", ".avi", ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dmg", ".apk",
)
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$)")
Fetcher = Callable[..., Awaitable[FetchResult]]
Progress = Callable[[int, int], Awaitable[None]]


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    found: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def normalize(url: str) -> str | None:
    """One spelling per page: no fragment, tracking parameters or default port."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.lower()
        port = parts.port
    except ValueError:
        return None
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.lower())))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path.lower().endswith(SKIP_EXTENSIONS):
        return None
    if len(query) > 100:  # calendars and filters that never end
        return None
    return urlunsplit((parts.scheme, host, path, query, ""))


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title = ""
        self.noindex = False
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.links.append(d["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and (d.get("name") or "").lower() == "robots":
            if "noindex" in (d.get("content") or "").lower():
                self.noindex = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_page(html: str, base_url: str) -> tuple[str, list[str], bool]:
    parser = _Links()
    parser.feed(html)
    links = []
    for href in parser.links:
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        links.append(urljoin(base_url, href))
    return re.sub(r"\s+", " ", parser.title).strip(), links, parser.noindex


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """(page URLs, child sitemap URLs). Documents with DTDs or entities are refused."""
    head = xml[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        return [], []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return [], []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    tag = root.tag.removeprefix(ns)
    locs = [
        (el.text or "").strip()
        for el in root.iter(f"{ns}loc")
        if el.text and el.text.strip()
    ] or [(el.text or "").strip() for el in root.iter("loc") if el.text and el.text.strip()]
    return ([], locs) if tag == "sitemapindex" else (locs, [])


async def _robots(origin: str, fetch: Fetcher) -> RobotFileParser | None:
    """Parsed robots.txt, or None meaning "everything is disallowed" (RFC 9309:
    a server error means stay away; a missing file means no rules)."""
    parser = RobotFileParser()
    try:
        result = await fetch(f"{origin}/robots.txt", max_bytes=500_000, allowed_types=("text/plain",))
    except UnsafeURL:
        return None
    except FetchError:
        return None
    if result.status >= 500:
        return None
    if result.status >= 400:
        parser.parse([])
    else:
        parser.parse(result.body.decode("utf-8", "replace").splitlines())
    return parser


async def _sitemap_urls(starts: list[str], site: str, fetch: Fetcher) -> list[str]:
    pages: list[str] = []
    queue, seen = list(starts), set()
    while queue and len(seen) < MAX_SITEMAPS:
        url = queue.pop(0)
        if url in seen or site_of(url) != site:
            continue
        seen.add(url)
        try:
            result = await fetch(url, max_bytes=2_000_000, allowed_types=XML_TYPES + ("text/plain",))
        except (FetchError, UnsafeURL):
            continue
        if result.status != 200:
            continue
        page_urls, child_sitemaps = parse_sitemap(result.body)
        pages.extend(page_urls)
        queue.extend(child_sitemaps)
    return pages


async def crawl(
    start_url: str,
    *,
    page_limit: int = 50,
    fetch: Fetcher = safe_get,
    on_progress: Progress | None = None,
    delay: float = 0.3,
    user_agent: str = USER_AGENT,
) -> CrawlResult:
    page_limit = max(1, min(page_limit, MAX_PAGE_LIMIT))
    start = normalize(start_url)
    if start is None:
        raise UnsafeURL("That is not a web address we can crawl")
    site = site_of(start)
    origin = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"
    result = CrawlResult()

    robots = await _robots(origin, fetch)
    if robots is None:
        raise FetchError("The site's robots.txt asks crawlers to stay away, or could not be read")
    pause = min(MAX_CRAWL_DELAY, max(delay, float(robots.crawl_delay(user_agent) or 0)))

    queue: list[str] = [start]
    queued = {start}

    def enqueue(url: str) -> None:
        n = normalize(url)
        if n is None or n in queued or site_of(n) != site:
            return
        queued.add(n)
        queue.append(n)

    for url in await _sitemap_urls([f"{origin}/sitemap.xml", *(robots.site_maps() or [])], site, fetch):
        enqueue(url)

    seen_text: set[str] = set()
    while queue and len(result.pages) < page_limit:
        url = queue.pop(0)
        if not robots.can_fetch(user_agent, url):
            result.skip("blocked by robots.txt")
            continue
        if result.pages or result.skipped or url != start:
            await asyncio.sleep(pause)
        try:
            fetched = await fetch(url, max_bytes=2_000_000, allowed_types=HTML_TYPES)
        except UnsafeURL:
            result.skip("private address")
            continue
        except FetchError as exc:
            result.skip(str(exc))
            continue
        if fetched.status != 200:
            result.skip(f"HTTP {fetched.status}")
            continue
        final = normalize(fetched.url)
        if final is None or site_of(final) != site:
            result.skip("redirected to another site")
            continue
        html = fetched.body.decode("utf-8", "replace")
        title, links, noindex = parse_page(html, fetched.url)
        for link in links:
            enqueue(link)
        if noindex:
            result.skip("marked noindex")
            continue
        text = html_to_text(html)
        if len(text) < MIN_TEXT_CHARS:
            result.skip("no readable text")
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen_text:
            result.skip("same text as another page")
            continue
        seen_text.add(digest)
        result.pages.append(Page(final, title or final, text))
        result.found = len(queued)
        if on_progress:
            await on_progress(result.found, len(result.pages))
    result.found = max(result.found, len(queued))
    return result

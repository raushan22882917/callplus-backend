"""Safe fetching and the website crawler: what may be fetched, how a site is read,
and how a source keeps its pages in step with the site."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.api.v1 import knowledge as knowledge_api
from app.services import crawler, knowledge_sources, plans
from app.services.safe_fetch import (
    FetchError,
    FetchResult,
    UnsafeURL,
    assert_fetchable,
    check_url,
    is_public_address,
    resolve_public,
    safe_get,
)
from httpx import AsyncClient
from sqlalchemy import func, select

from packages.database.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource, Tenant
from packages.database.session import session_scope

PUBLIC = "93.184.216.34"


def resolver(table: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


# ---- addresses and URLs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
     "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
     "64:ff9b::7f00:1", "2002:7f00:1::1"],
)
def test_private_and_special_addresses_are_refused(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address):
    assert is_public_address(address) is True


@pytest.mark.parametrize(
    "url, message",
    [
        ("ftp://example.com/", "Only http"),
        ("file:///etc/passwd", "Only http"),
        ("gopher://example.com", "Only http"),
        ("javascript:alert(1)", "Only http"),
        ("https://user:pw@example.com/", "username or password"),
        ("https://example.com:8080/", "ports 80 and 443"),
        ("http://example.com:22/", "ports 80 and 443"),
        ("https:///path", "no host"),
        ("https://example.com:notaport/", "invalid port"),
        ("https://example.com/" + "a" * 2100, "too long"),
    ],
)
def test_urls_that_may_not_be_fetched(url, message):
    with pytest.raises(UnsafeURL, match=message):
        check_url(url)


def test_ordinary_urls_pass():
    assert check_url("https://Example.com/a?b=1") == ("https", "example.com", 443)
    assert check_url("http://example.com:80/") == ("http", "example.com", 80)


async def test_a_name_that_resolves_to_a_private_address_is_refused():
    res = resolver({"evil.test": ["10.0.0.5"], "mixed.test": [PUBLIC, "127.0.0.1"], "ok.test": [PUBLIC], "none.test": []})
    with pytest.raises(UnsafeURL, match="private network"):
        await resolve_public("evil.test", 443, res)
    with pytest.raises(UnsafeURL):  # one bad address among good ones is enough
        await resolve_public("mixed.test", 443, res)
    with pytest.raises(FetchError, match="Could not find"):
        await resolve_public("gone.test", 443, res)
    with pytest.raises(FetchError):
        await resolve_public("none.test", 443, res)
    assert await resolve_public("ok.test", 443, res) == [PUBLIC]


async def test_odd_ip_spellings_are_judged_by_what_they_resolve_to():
    # the system resolver turns these into 127.0.0.1; none may get through
    for url in ("http://127.0.0.1/", "http://localhost/", "http://2130706433/", "http://0x7f000001/",
                "http://[::1]/", "http://0/", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises((UnsafeURL, FetchError)):
            await assert_fetchable(url)


# ---- safe_get -------------------------------------------------------------------------------


def site(handler):
    return httpx.MockTransport(handler)


async def test_it_connects_to_the_address_it_checked_and_keeps_the_name():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(host=request.url.host, header=request.headers["host"], sni=request.extensions.get("sni_hostname"),
                    method=request.method, agent=request.headers["user-agent"])
        return httpx.Response(200, text="<p>hi</p>", headers={"content-type": "text/html; charset=utf-8"})

    out = await safe_get("https://shop.test/page", resolver=resolver({"shop.test": [PUBLIC]}), transport=site(handler))
    assert out.status == 200 and out.body == b"<p>hi</p>" and out.content_type == "text/html"
    assert seen["host"] == PUBLIC and seen["header"] == "shop.test" and seen["sni"] == "shop.test"
    assert seen["method"] == "GET" and "CallPulseBot" in seen["agent"]


async def test_a_public_site_cannot_redirect_us_into_the_private_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://internal.test/admin"})

    res = resolver({"shop.test": [PUBLIC], "internal.test": ["10.0.0.9"]})
    with pytest.raises(UnsafeURL):
        await safe_get("https://shop.test/", resolver=res, transport=site(handler))
    assert calls == ["shop.test"]  # the private host was never contacted


async def test_redirects_are_followed_relative_or_absolute_but_not_forever():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"location": "https://other.test/end"})
        return httpx.Response(200, text="done", headers={"content-type": "text/plain"})

    res = resolver({"shop.test": [PUBLIC], "other.test": ["8.8.8.8"]})
    out = await safe_get("https://shop.test/start", resolver=res, transport=site(handler))
    assert out.url == "https://other.test/end" and out.body == b"done"

    loop = site(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(FetchError, match="too many times"):
        await safe_get("https://shop.test/", resolver=res, transport=loop)
    nowhere = site(lambda r: httpx.Response(302))
    with pytest.raises(FetchError, match="without saying where"):
        await safe_get("https://shop.test/", resolver=res, transport=nowhere)


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://example.com/x", "https://u:p@shop.test/", "http://shop.test:8080/"])
async def test_a_redirect_to_a_forbidden_kind_of_address_is_refused(target):
    t = site(lambda r: httpx.Response(302, headers={"location": target}))
    with pytest.raises(UnsafeURL):
        await safe_get("https://shop.test/", resolver=resolver({"shop.test": [PUBLIC]}), transport=t)


async def test_size_and_type_limits():
    res = resolver({"shop.test": [PUBLIC]})
    big = site(lambda r: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"}))
    with pytest.raises(FetchError, match="too large"):
        await safe_get("https://shop.test/", max_bytes=1000, resolver=res, transport=big)
    declared = site(lambda r: httpx.Response(200, content=b"x", headers={"content-type": "text/html", "content-length": "99999999"}))
    with pytest.raises(FetchError, match="too large"):
        await safe_get("https://shop.test/", max_bytes=1000, resolver=res, transport=declared)
    image = site(lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    with pytest.raises(FetchError, match="Unsupported content type: image/png"):
        await safe_get("https://shop.test/", allowed_types=("text/html",), resolver=res, transport=image)
    missing = site(lambda r: httpx.Response(404, text="nope", headers={"content-type": "image/png"}))
    out = await safe_get("https://shop.test/", allowed_types=("text/html",), resolver=res, transport=missing)
    assert out.status == 404  # a status is reported, not raised


async def test_network_failures_become_fetch_errors():
    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(FetchError, match="Could not fetch"):
        await safe_get("https://shop.test/", resolver=resolver({"shop.test": [PUBLIC]}), transport=site(boom))


# ---- the crawler ----------------------------------------------------------------------------


def html(title: str, body: str, links: tuple[str, ...] = (), head: str = "") -> bytes:
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return f"<html><head><title>{title}</title>{head}</head><body><h1>{title}</h1><p>{body}</p>{anchors}</body></html>".encode()


LONG = "This page explains how things work in plenty of detail so that it is long enough to be kept. " * 2


class Site:
    """A fake website: path -> (status, content type, body). Records what was asked for."""

    def __init__(self, pages: dict[str, tuple[int, str, bytes]]):
        self.pages, self.asked = pages, []

    async def __call__(self, url: str, **_kw) -> FetchResult:
        self.asked.append(url)
        status, kind, body = self.pages.get(url, (404, "text/html", b"missing"))
        if status == 200 and _kw.get("allowed_types") and kind not in _kw["allowed_types"]:
            raise FetchError(f"Unsupported content type: {kind}")
        return FetchResult(url, status, kind, body)


def ok(body: bytes, kind: str = "text/html") -> tuple[int, str, bytes]:
    return 200, kind, body


async def run(pages, **kw):
    fake = Site(pages)
    return await crawler.crawl("https://shop.test/", fetch=fake, delay=0, **kw), fake


async def test_it_reads_the_pages_it_can_reach_and_stays_on_the_site():
    result, fake = await run({
        "https://shop.test/robots.txt": (404, "text/plain", b""),
        "https://shop.test/": ok(html("Home", LONG, ("/about", "https://www.shop.test/contact", "https://other.test/x", "mailto:a@b.co", "#top"))),
        "https://shop.test/about": ok(html("About", LONG + " about us", ("/",))),
        "https://www.shop.test/contact": ok(html("Contact", LONG + " contact us")),
        "https://other.test/x": ok(html("Other", LONG)),
    })
    assert [p.title for p in result.pages] == ["Home", "About", "Contact"]
    assert "https://other.test/x" not in fake.asked  # another site is never visited
    assert result.pages[0].text.startswith("Home")


async def test_the_page_limit_is_never_exceeded():
    pages = {"https://shop.test/robots.txt": (404, "text/plain", b""),
             "https://shop.test/": ok(html("Home", LONG, tuple(f"/p{i}" for i in range(20))))}
    pages.update({f"https://shop.test/p{i}": ok(html(f"P{i}", LONG + str(i))) for i in range(20)})
    result, fake = await run(pages, page_limit=5)
    assert len(result.pages) == 5
    assert len([u for u in fake.asked if u.rsplit("/", 1)[-1] not in ("robots.txt", "sitemap.xml")]) == 5


async def test_robots_txt_is_obeyed():
    result, fake = await run({
        "https://shop.test/robots.txt": ok(b"User-agent: *\nDisallow: /private\n", "text/plain"),
        "https://shop.test/": ok(html("Home", LONG, ("/private/accounts", "/public"))),
        "https://shop.test/private/accounts": ok(html("Accounts", LONG + "secret")),
        "https://shop.test/public": ok(html("Public", LONG + "open")),
    })
    assert [p.title for p in result.pages] == ["Home", "Public"]
    assert "https://shop.test/private/accounts" not in fake.asked
    assert result.skipped == {"blocked by robots.txt": 1}


async def test_a_site_that_bans_crawlers_or_errors_on_robots_is_left_alone():
    banned = {"https://shop.test/robots.txt": ok(b"User-agent: *\nDisallow: /\n", "text/plain"),
              "https://shop.test/": ok(html("Home", LONG))}
    result, fake = await run(banned)
    assert result.pages == [] and "https://shop.test/" not in fake.asked
    with pytest.raises(FetchError, match="robots.txt"):
        await crawler.crawl("https://shop.test/", fetch=Site({"https://shop.test/robots.txt": (503, "text/plain", b"")}), delay=0)
    named = {"https://shop.test/robots.txt": ok(b"User-agent: CallPulseBot\nDisallow: /\n", "text/plain"),
             "https://shop.test/": ok(html("Home", LONG))}
    assert (await run(named))[0].pages == []


async def test_the_sitemap_is_used_and_the_indexes_inside_it_followed():
    index = b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://shop.test/s1.xml</loc></sitemap></sitemapindex>'
    child = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://shop.test/deep</loc></url><url><loc>https://elsewhere.test/x</loc></url></urlset>'
    result, fake = await run({
        "https://shop.test/robots.txt": ok(b"Sitemap: https://shop.test/map.xml\n", "text/plain"),
        "https://shop.test/map.xml": ok(index, "application/xml"),
        "https://shop.test/s1.xml": ok(child, "application/xml"),
        "https://shop.test/": ok(html("Home", LONG)),
        "https://shop.test/deep": ok(html("Deep", LONG + " nested page")),
        "https://elsewhere.test/x": ok(html("Elsewhere", LONG)),
    })
    assert [p.title for p in result.pages] == ["Home", "Deep"]
    assert "https://elsewhere.test/x" not in fake.asked


def test_sitemaps_with_entities_or_garbage_are_ignored():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
    assert crawler.parse_sitemap(evil) == ([], [])
    assert crawler.parse_sitemap(b"not xml at all") == ([], [])
    assert crawler.parse_sitemap(b"<urlset><url><loc> https://a.test/x </loc></url></urlset>") == (["https://a.test/x"], [])


async def test_pages_that_should_not_be_kept_are_skipped_with_a_reason():
    result, _ = await run({
        "https://shop.test/robots.txt": (404, "text/plain", b""),
        "https://shop.test/": ok(html("Home", LONG, ("/hidden", "/tiny", "/dup", "/gone", "/file.pdf", "/img", "/a?utm_source=x"))),
        "https://shop.test/hidden": ok(html("Hidden", LONG + "x", head='<meta name="robots" content="noindex,follow">')),
        "https://shop.test/tiny": ok(html("Tiny", "hi")),
        "https://shop.test/dup": ok(html("Home", LONG, ("/hidden", "/tiny", "/dup", "/gone", "/file.pdf", "/img", "/a?utm_source=x"))),  # same text as the home page
        "https://shop.test/img": ok(b"\x89PNG", "image/png"),
        "https://shop.test/a": ok(html("A", LONG + " a page")),
    })
    assert [p.title for p in result.pages] == ["Home", "A"]
    assert result.skipped["marked noindex"] == 1 and result.skipped["no readable text"] == 1
    assert result.skipped["same text as another page"] == 1 and result.skipped["HTTP 404"] == 1
    assert any("Unsupported content type" in k for k in result.skipped)


def test_urls_are_normalised_to_one_spelling():
    n = crawler.normalize
    assert n("HTTPS://Shop.Test:443/a/?utm_source=x&b=2&a=1#frag") == "https://shop.test/a?a=1&b=2"
    assert n("https://shop.test//a///b/") == "https://shop.test/a/b"
    assert n("https://shop.test") == "https://shop.test/"
    assert n("http://shop.test:8080/x") == "http://shop.test:8080/x"
    for bad in ("ftp://a.test/", "javascript:x", "https://a.test/pic.JPG", "https://a.test/?" + "q=1&" * 60, "https://:99999999/"):
        assert n(bad) is None
    assert crawler.site_of("https://www.Shop.test/x") == "shop.test"


async def test_progress_is_reported_and_a_private_target_is_skipped_not_fatal():
    steps = []

    async def progress(found, done):
        steps.append((found, done))

    class Mixed(Site):
        async def __call__(self, url, **kw):
            if url.endswith("/internal"):
                raise UnsafeURL("private")
            return await super().__call__(url, **kw)

    fake = Mixed({"https://shop.test/robots.txt": (404, "text/plain", b""),
                  "https://shop.test/": ok(html("Home", LONG, ("/internal", "/b"))),
                  "https://shop.test/b": ok(html("B", LONG + " b"))})
    result = await crawler.crawl("https://shop.test/", fetch=fake, delay=0, on_progress=progress)
    assert [done for _, done in steps] == [1, 2] and steps[-1][0] >= 3
    assert result.skipped == {"private address": 1}
    with pytest.raises(UnsafeURL):
        await crawler.crawl("ftp://shop.test/", fetch=fake, delay=0)


# ---- sources through the API ----------------------------------------------------------------


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
              "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def queued(monkeypatch):
    """Records crawl requests instead of sending them to a worker, and skips the DNS lookup."""
    ids: list[uuid.UUID] = []
    monkeypatch.setattr(knowledge_sources, "enqueue_crawl", ids.append)

    async def fetchable(url, resolver=None):
        from app.services.safe_fetch import check_url
        check_url(url)

    monkeypatch.setattr(knowledge_api, "assert_fetchable", fetchable)
    return ids


def good_site(extra: dict | None = None) -> Site:
    pages = {
        "https://shop.test/robots.txt": (404, "text/plain", b""),
        "https://shop.test/": ok(html("Home", LONG + " We sell tea.", ("/refunds", "/hours"))),
        "https://shop.test/refunds": ok(html("Refunds", "Refunds are issued within seven days of the returned item arriving. " * 2)),
        "https://shop.test/hours": ok(html("Hours", "We are open from ten to eight, Monday to Saturday, and closed on public holidays. " * 2)),
    }
    pages.update(extra or {})
    return Site(pages)


async def _add_source(client, auth, url="https://shop.test/", **extra) -> dict:
    resp = await client.post("/api/v1/knowledge/sources", headers=auth, json={"url": url, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_a_website_becomes_documents_you_can_search(client: AsyncClient, queued):
    auth = await _signup(client, "src-co")
    source = await _add_source(client, auth, "shop.test")  # https:// is assumed
    assert source["status"] == "queued" and source["url"] == "https://shop.test" and queued == [uuid.UUID(source["id"])]

    await knowledge_sources.run_crawl(uuid.UUID(source["id"]), fetch=good_site(), delay=0)
    done = (await client.get(f"/api/v1/knowledge/sources/{source['id']}", headers=auth)).json()
    assert done["status"] == "ready" and done["pages_done"] == 3 and done["pages_found"] >= 3 and done["last_synced_at"]
    docs = (await client.get("/api/v1/knowledge/documents", headers=auth)).json()
    assert {d["title"] for d in docs} == {"Home", "Refunds", "Hours"}
    assert all(d["source_id"] == source["id"] and d["url"].startswith("https://shop.test") for d in docs)
    hits = (await client.post("/api/v1/knowledge/search", headers=auth, json={"query": "refund for returned item"})).json()
    assert hits[0]["title"] == "Refunds"


async def test_a_second_crawl_adds_changes_and_removes_pages(client: AsyncClient, queued):
    auth = await _signup(client, "resync-co")
    source = await _add_source(client, auth)
    sid = uuid.UUID(source["id"])
    await knowledge_sources.run_crawl(sid, fetch=good_site(), delay=0)

    changed = good_site({
        "https://shop.test/": ok(html("Home", LONG + " We sell tea.", ("/refunds", "/delivery"))),  # /hours no longer linked
        "https://shop.test/refunds": ok(html("Refunds", "Refunds now take ten days after the returned item arrives. " * 2)),
        "https://shop.test/delivery": ok(html("Delivery", "Delivery takes three working days across the country. " * 2)),
    })
    for gone in ("https://shop.test/hours",):
        changed.pages.pop(gone)
    await knowledge_sources.run_crawl(sid, fetch=changed, delay=0)
    docs = {d["title"]: d for d in (await client.get("/api/v1/knowledge/documents", headers=auth)).json()}
    assert set(docs) == {"Home", "Refunds", "Delivery"}  # Hours removed, Delivery added
    detail = (await client.get(f"/api/v1/knowledge/documents/{docs['Refunds']['id']}", headers=auth)).json()
    assert "ten days" in detail["content"]
    hits = (await client.post("/api/v1/knowledge/search", headers=auth, json={"query": "refund days"})).json()
    assert "ten days" in hits[0]["content"]


async def test_a_failed_crawl_says_why_and_keeps_what_was_there(client: AsyncClient, queued):
    auth = await _signup(client, "fail-co")
    source = await _add_source(client, auth)
    sid = uuid.UUID(source["id"])
    await knowledge_sources.run_crawl(sid, fetch=good_site(), delay=0)

    blocked = good_site({"https://shop.test/robots.txt": ok(b"User-agent: *\nDisallow: /\n", "text/plain")})
    await knowledge_sources.run_crawl(sid, fetch=blocked, delay=0)
    failed = (await client.get(f"/api/v1/knowledge/sources/{sid}", headers=auth)).json()
    assert failed["status"] == "failed"
    assert "robots.txt" in failed["error"] or "No readable pages" in failed["error"]
    assert len((await client.get("/api/v1/knowledge/documents", headers=auth)).json()) == 3  # untouched

    class Down(Site):
        async def __call__(self, url, **kw):
            raise RuntimeError("bug")

    await knowledge_sources.run_crawl(sid, fetch=Down({}), delay=0)
    assert (await client.get(f"/api/v1/knowledge/sources/{sid}", headers=auth)).json()["error"].startswith("Unexpected error")

    empty = Site({"https://shop.test/robots.txt": (404, "text/plain", b""), "https://shop.test/": ok(html("Home", "hi"))})
    await knowledge_sources.run_crawl(sid, fetch=empty, delay=0)
    assert "No readable pages" in (await client.get(f"/api/v1/knowledge/sources/{sid}", headers=auth)).json()["error"]


async def test_the_plans_document_limit_stops_a_crawl_adding_more(client: AsyncClient, queued, monkeypatch):
    auth = await _signup(client, "plan-co")
    source = await _add_source(client, auth)

    async def room(db, tenant_id, resource):
        return 2

    monkeypatch.setattr(plans, "remaining", room)
    await knowledge_sources.run_crawl(uuid.UUID(source["id"]), fetch=good_site(), delay=0)
    done = (await client.get(f"/api/v1/knowledge/sources/{source['id']}", headers=auth)).json()
    assert done["status"] == "ready" and "plan's document limit" in done["error"]
    assert len((await client.get("/api/v1/knowledge/documents", headers=auth)).json()) == 2


async def test_creating_a_source_checks_the_address_the_plan_and_the_count(client: AsyncClient, monkeypatch):
    auth = await _signup(client, "val-co")
    monkeypatch.setattr(knowledge_sources, "enqueue_crawl", lambda _id: None)
    for url in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/", "ftp://example.com", "https://example.com:8080/"):
        bad = await client.post("/api/v1/knowledge/sources", headers=auth, json={"url": url})
        assert bad.status_code == 400, url
    assert (await client.post("/api/v1/knowledge/sources", headers=auth, json={"url": "x"})).status_code == 422
    assert (await client.post("/api/v1/knowledge/sources", json={"url": "https://a.test"})).status_code == 401

    async def fetchable(url, resolver=None):
        return None

    monkeypatch.setattr(knowledge_api, "assert_fetchable", fetchable)
    for i in range(knowledge_api.MAX_ACTIVE_SOURCES):
        await _add_source(client, auth, f"https://s{i}.test/")
    over = await client.post("/api/v1/knowledge/sources", headers=auth, json={"url": "https://more.test/"})
    assert over.status_code == 409


async def test_resync_delete_and_tenant_isolation(client: AsyncClient, queued):
    a, b = await _signup(client, "iso-sa"), await _signup(client, "iso-sb")
    source = await _add_source(client, a)
    sid = uuid.UUID(source["id"])
    assert (await client.post(f"/api/v1/knowledge/sources/{sid}/resync", headers=a)).status_code == 409  # still queued
    await knowledge_sources.run_crawl(sid, fetch=good_site(), delay=0)
    resync = await client.post(f"/api/v1/knowledge/sources/{sid}/resync", headers=a)
    assert resync.status_code == 200 and resync.json()["status"] == "queued" and queued[-1] == sid

    for call in (client.get(f"/api/v1/knowledge/sources/{sid}", headers=b), client.delete(f"/api/v1/knowledge/sources/{sid}", headers=b),
                 client.post(f"/api/v1/knowledge/sources/{sid}/resync", headers=b), client.patch(f"/api/v1/knowledge/sources/{sid}", headers=b, json={"resync_weekly": False})):
        assert (await call).status_code == 404
    assert (await client.get("/api/v1/knowledge/sources", headers=b)).json() == []
    off = await client.patch(f"/api/v1/knowledge/sources/{sid}", headers=a, json={"resync_weekly": False})
    assert off.json()["resync_weekly"] is False

    assert (await client.delete(f"/api/v1/knowledge/sources/{sid}", headers=a)).status_code == 204
    async with session_scope() as db:
        for model in (KnowledgeSource, KnowledgeDocument, KnowledgeChunk):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_sources_are_found_again_when_the_queue_missed_them_or_a_week_has_passed(client: AsyncClient, queued):
    auth = await _signup(client, "sweep-co")
    now = datetime.now(UTC)
    ids = {}
    for name in ("fresh", "missed", "stuck", "weekly", "recent", "optout", "never"):
        ids[name] = uuid.UUID((await _add_source(client, auth, f"https://{name}.test/"))["id"])
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        rows = {s.id: s for s in (await db.execute(select(KnowledgeSource))).scalars()}
        rows[ids["missed"]].created_at = now - timedelta(minutes=20)
        for name, status, started, synced, resync in [
            ("stuck", "crawling", now - timedelta(hours=2), None, True),
            ("weekly", "ready", now, now - timedelta(days=8), True),
            ("recent", "ready", now, now - timedelta(days=2), True),
            ("optout", "ready", now, now - timedelta(days=30), False),
            ("never", "failed", now, None, True),
        ]:
            s = rows[ids[name]]
            s.status, s.started_at, s.last_synced_at, s.resync_weekly = status, started, synced, resync
        await db.commit()
        due = set(await knowledge_sources.due_sources(db, now))
        _ = tenant
    assert due == {ids["missed"], ids["stuck"], ids["weekly"]}


async def test_the_sweep_task_enqueues_whatever_is_due(client: AsyncClient, queued):
    from app.worker import knowledge_tasks

    auth = await _signup(client, "task-co")
    source = await _add_source(client, auth)
    async with session_scope() as db:
        s = await db.get(KnowledgeSource, uuid.UUID(source["id"]))
        s.created_at = datetime.now(UTC) - timedelta(minutes=30)
        await db.commit()
    queued.clear()
    assert await knowledge_tasks._sweep() == 1 and queued == [uuid.UUID(source["id"])]

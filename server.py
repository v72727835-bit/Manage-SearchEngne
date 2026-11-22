"""
Freelance Lead Finder - a stateless, cloud-deployable MCP server (Python).

It searches public job boards and hiring threads (official APIs / RSS only)
and returns freelance/contract leads as structured data, so Claude can read
them, rank them and draft outreach messages for you to review.

No database: everything is fetched live, with a small in-memory cache.

Run locally:   python server.py            (serves http://0.0.0.0:8000/mcp)
Env vars:      see .env.example
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config (all optional)
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")  # set a secret path, e.g. /mcp-8f3k2x
MY_PROFILE = os.getenv("MY_PROFILE", "")  # your skills / pitch, used for outreach drafts
USER_AGENT = os.getenv(
    "USER_AGENT", "freelance-lead-finder-mcp/1.0 (personal job search tool)"
)
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "8"))
# Hard limit per source: a slow board is dropped instead of making you wait.
SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT_SECONDS", "6"))
CACHE_MAX = int(os.getenv("CACHE_MAX_ENTRIES", "300"))

mcp = FastMCP(
    "freelance-lead-finder",
    instructions=(
        "Fast freelance lead finder + GitHub/cPanel/Vercel tools. Flow: one "
        "find_freelance_leads call (all boards in parallel, ~1-3 s) -> "
        "get_lead_details only for the 2-3 best -> draft_outreach. Repeat "
        "searches within 10 min are cached (instant). Never send messages "
        "automatically: show drafts to the user."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
FREELANCE_WORDS = ("freelance", "contract", "contractor", "part-time", "part time",
                   "gig", "project", "hourly", "temporary", "consultant")


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=4.0),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20, keepalive_expiry=60),
    )


# One warm client per event loop: keeps TCP/TLS connections open between tool
# calls, so the 2nd search skips DNS + TLS handshakes (~100-300 ms per host).
_clients: dict[int, tuple[Any, httpx.AsyncClient, Any]] = {}


class _Borrowed:
    """`async with _client() as c` that does NOT close the shared client."""

    def __init__(self, c: httpx.AsyncClient):
        self.c = c

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.c

    async def __aexit__(self, *exc) -> None:
        return None


def _client() -> _Borrowed:
    loop = asyncio.get_running_loop()
    hit = _clients.get(id(loop))
    if hit and hit[0] is loop and not hit[1].is_closed and hit[2] is _http:
        return _Borrowed(hit[1])
    c = _http()
    _clients.clear()  # old loops are gone (serverless / tests)
    _clients[id(loop)] = (loop, c, _http)
    return _Borrowed(c)


_inflight: dict[str, asyncio.Future] = {}


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    key = url + repr(sorted(kw.get("params", {}).items())) if kw.get("params") else url
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    fut = _inflight.get(key)  # same URL already being fetched -> share it
    if fut is not None and not fut.done():
        return await asyncio.shield(fut)
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        r = await client.get(url, **kw)
        r.raise_for_status()
        if len(_cache) >= CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX // 4]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), r)
        fut.set_result(r)
        return r
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()  # mark retrieved
        raise
    finally:
        _inflight.pop(key, None)


def _strip_html(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _lead(source: str, title: str, url: str, company: str = "", posted: Any = None,
          description: str = "", location: str = "", job_type: str = "",
          budget: str = "", tags: list[str] | None = None,
          contact_hint: str = "") -> dict[str, Any]:
    d = _parse_date(posted)
    return {
        "source": source,
        "title": _strip_html(title, 200),
        "company": company or "",
        "url": url,
        "posted_at": d.isoformat() if d else None,
        "location": location or "",
        "job_type": job_type or "",
        "budget": budget or "",
        "tags": [str(t) for t in (tags or [])][:10],
        "summary": _strip_html(description),
        "contact_hint": contact_hint,
    }


def _matches(lead: dict, keywords: list[str]) -> int:
    """Score a lead against keywords. 0 means no match."""
    if not keywords:
        return 1
    title = lead["title"].lower()
    body = " ".join([lead["summary"], " ".join(lead["tags"]), lead["company"]]).lower()
    score = 0
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        if k in title:
            score += 3
        elif k in body:
            score += 1
    return score


def _is_freelance(lead: dict) -> bool:
    text = " ".join([lead.get("job_type", ""), lead.get("title", ""), lead.get("summary", "")]).lower()
    return any(w in text for w in FREELANCE_WORDS)


# ---------------------------------------------------------------------------
# Sources (each returns a list of leads; keyword filtering happens later)
# ---------------------------------------------------------------------------
async def src_remotive(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    params = {"limit": 100}
    if keywords:
        params["search"] = keywords[0]
    r = await _get(c, "https://remotive.com/api/remote-jobs", params=params)
    out = []
    for j in r.json().get("jobs", []):
        out.append(_lead(
            "remotive", j.get("title", ""), j.get("url", ""), j.get("company_name", ""),
            j.get("publication_date"), j.get("description", ""),
            j.get("candidate_required_location", ""), j.get("job_type", ""),
            j.get("salary", ""), j.get("tags"),
        ))
    return out


async def src_remoteok(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    r = await _get(c, "https://remoteok.com/api")
    out = []
    for j in r.json():
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is a legal notice
        budget = ""
        if j.get("salary_min") or j.get("salary_max"):
            budget = f"${j.get('salary_min') or '?'}-{j.get('salary_max') or '?'} /yr"
        out.append(_lead(
            "remoteok", j.get("position", ""), j.get("url") or j.get("apply_url", ""),
            j.get("company", ""), j.get("epoch") or j.get("date"), j.get("description", ""),
            j.get("location", ""), "", budget, j.get("tags"),
        ))
    return out


async def src_weworkremotely(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-design-jobs.rss",
        "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        "https://weworkremotely.com/remote-jobs.rss",
    ]
    out, seen = [], set()
    results = await asyncio.gather(*[_get(c, f) for f in feeds], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            continue
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            if link in seen:
                continue
            seen.add(link)
            raw_title = it.findtext("title") or ""
            company, _, title = raw_title.partition(":")
            if not title:
                company, title = "", raw_title
            out.append(_lead(
                "weworkremotely", title.strip(), link, company.strip(),
                it.findtext("pubDate"), it.findtext("description") or "",
                it.findtext("region") or "", it.findtext("type") or "",
            ))
    if not out and any(isinstance(r, Exception) for r in results):
        raise next(r for r in results if isinstance(r, Exception))
    return out


async def src_jobicy(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    params: dict[str, Any] = {"count": 50}
    if keywords:
        params["tag"] = keywords[0]
    r = await _get(c, "https://jobicy.com/api/v2/remote-jobs", params=params)
    out = []
    for j in r.json().get("jobs", []):
        jt = j.get("jobType")
        jt = ", ".join(jt) if isinstance(jt, list) else (jt or "")
        budget = ""
        if j.get("annualSalaryMin"):
            budget = f"{j.get('salaryCurrency', '')} {j['annualSalaryMin']}-{j.get('annualSalaryMax', '?')} /yr"
        out.append(_lead(
            "jobicy", j.get("jobTitle", ""), j.get("url", ""), j.get("companyName", ""),
            j.get("pubDate"), j.get("jobExcerpt") or j.get("jobDescription", ""),
            j.get("jobGeo", ""), jt, budget, j.get("jobIndustry"),
        ))
    return out


async def src_himalayas(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    r = await _get(c, "https://himalayas.app/jobs/api", params={"limit": 100})
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("locationRestrictions") or []
        out.append(_lead(
            "himalayas", j.get("title", ""), j.get("applicationLink") or j.get("guid", ""),
            j.get("companyName", ""), j.get("pubDate"), j.get("excerpt") or j.get("description", ""),
            ", ".join(loc) if isinstance(loc, list) else str(loc),
            j.get("employmentType", ""), "", j.get("categories"),
        ))
    return out


async def src_hackernews(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    """Latest 'Freelancer? Seeking freelancer?' and 'Who is hiring?' threads."""
    r = await _get(c, "https://hn.algolia.com/api/v1/search_by_date",
                   params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
    threads: dict[str, str] = {}
    for h in r.json().get("hits", []):
        t = (h.get("title") or "").lower()
        if "freelancer" in t and "freelancer" not in threads:
            threads["freelancer"] = h["objectID"]
        elif "who is hiring" in t and "hiring" not in threads:
            threads["hiring"] = h["objectID"]
    out = []

    async def comments(story_id: str):
        params = {"tags": f"comment,story_{story_id}", "hitsPerPage": 100}
        if keywords:
            params["query"] = " ".join(keywords[:1])
        return await _get(c, "https://hn.algolia.com/api/v1/search", params=params)

    kinds = list(threads.items())
    responses = await asyncio.gather(*[comments(sid) for _, sid in kinds])  # both threads at once
    for (kind, story_id), cr in zip(kinds, responses):
        for h in cr.json().get("hits", []):
            text = h.get("comment_text") or ""
            if h.get("parent_id") and str(h.get("parent_id")) != str(story_id):
                continue  # only top-level posts
            low = text.lower()
            if kind == "freelancer" and "seeking freelancer" not in low:
                continue  # skip 'SEEKING WORK' posts (those are other freelancers)
            if _JOB_SEEKER.search(low[:400]):
                continue  # people looking for work, not clients
            first_line = _strip_html(text.split("<p>")[0], 160)
            out.append(_lead(
                f"hackernews-{kind}", first_line or "HN hiring post",
                f"https://news.ycombinator.com/item?id={h['objectID']}",
                first_line.split("|")[0].strip(), h.get("created_at"), text,
                job_type="freelance" if kind == "freelancer" else "",
                contact_hint=f"HN user: {h.get('author', '')}",
            ))
    return out


_JOB_SEEKER = re.compile(r"seeking work|willing to relocate|r[ée]sum[ée]/cv|looking for (full-time|a job|work)|"
                         r"i'?m (currently )?(open to|looking for|seeking)")

_reddit_token: dict[str, Any] = {}


async def _reddit_auth(c: httpx.AsyncClient) -> str | None:
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    if _reddit_token.get("exp", 0) > time.time() + 60:
        return _reddit_token["tok"]
    r = await c.post("https://www.reddit.com/api/v1/access_token",
                     data={"grant_type": "client_credentials"},
                     auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET))
    r.raise_for_status()
    d = r.json()
    _reddit_token.update(tok=d["access_token"], exp=time.time() + d.get("expires_in", 3600))
    return d["access_token"]


async def src_reddit(c: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    subs = "forhire+hiring+freelance_forhire+jobbit+HireAWriter+DesignJobs"
    token = await _reddit_auth(c)
    base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
    headers = {"Authorization": f"bearer {token}"} if token else {}
    q = " ".join(keywords[:3]) if keywords else "hiring"
    r = await _get(c, f"{base}/r/{subs}/search.json", headers=headers,
                   params={"q": q, "restrict_sr": 1, "sort": "new", "t": "month", "limit": 100})
    out = []
    for ch in r.json().get("data", {}).get("children", []):
        p = ch.get("data", {})
        title = p.get("title", "")
        flair = (p.get("link_flair_text") or "").lower()
        tl = title.lower()
        if "[for hire]" in tl or "for hire" in flair:
            continue  # other freelancers advertising themselves
        if not ("hiring" in tl or "hiring" in flair or "task" in tl):
            continue
        out.append(_lead(
            f"reddit-r/{p.get('subreddit', '')}", title,
            "https://www.reddit.com" + p.get("permalink", ""), "",
            p.get("created_utc"), p.get("selftext", ""), job_type="freelance",
            contact_hint=f"Reddit user: u/{p.get('author', '')} (reply on the post or DM if the post allows)",
        ))
    return out


SOURCES: dict[str, Any] = {
    "remotive": src_remotive,
    "remoteok": src_remoteok,
    "weworkremotely": src_weworkremotely,
    "jobicy": src_jobicy,
    "himalayas": src_himalayas,
    "hackernews": src_hackernews,
    "reddit": src_reddit,
}
_BUILTIN_SOURCES = set(SOURCES)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _compact(ld: dict, summary_chars: int) -> dict:
    """Small lead: only fields with a value, short summary -> model reads it faster."""
    out = {k: v for k, v in ld.items() if v not in ("", None, [], {})}
    if out.get("company") and out["company"] == out.get("title"):
        out.pop("company")
    if "summary" in out and len(out["summary"]) > summary_chars:
        out["summary"] = out["summary"][:summary_chars].rstrip() + "..."
    if "tags" in out:
        out["tags"] = out["tags"][:5]
    if out.get("posted_at"):
        out["posted_at"] = out["posted_at"][:10]
    return out


def _default_sources() -> list[str]:
    # Reddit blocks cloud IPs without OAuth (403), so don't waste time on it by default.
    return [s for s in SOURCES if s != "reddit" or (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)]


@mcp.tool()
async def find_freelance_leads(
    keywords: list[str],
    sources: list[str] | None = None,
    freelance_only: bool = False,
    posted_within_days: int = 30,
    max_results: int = 20,
    detail: str = "compact",
) -> dict:
    """Search all job boards + hiring threads in parallel (~1-3 s, cached 10 min).

    Args:
        keywords: Skills, e.g. ["python", "django"], ["shopify"]. First one is
            also sent to boards with server-side search.
        sources: Optional subset (see list_sources). Default: all working ones.
        freelance_only: Only freelance / contract / part-time / project work.
        posted_within_days: Drop older leads.
        max_results: Max leads returned (best first).
        detail: "compact" (short summaries, fast to read) or "full".
    """
    t0 = time.perf_counter()
    chosen = [s for s in (sources or _default_sources()) if s in SOURCES]
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, posted_within_days))
    status: dict[str, str] = {}
    leads: list[dict] = []

    async with _client() as c:
        async def run(name: str):
            ts = time.perf_counter()
            try:
                res = await asyncio.wait_for(SOURCES[name](c, keywords), SOURCE_TIMEOUT)
                status[name] = f"ok {len(res)} in {int((time.perf_counter() - ts) * 1000)}ms"
                return res
            except asyncio.TimeoutError:
                status[name] = f"skipped: slower than {SOURCE_TIMEOUT:g}s"
                return []
            except Exception as e:  # one failing source must not break the rest
                status[name] = f"error: {type(e).__name__}: {str(e)[:100]}"
                return []

        for res in await asyncio.gather(*[run(s) for s in chosen]):
            leads.extend(res)

    seen, ranked = set(), []
    for ld in leads:
        key = re.sub(r"[?&]utm_[^&]*", "", ld["url"] or ld["title"]).lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        score = _matches(ld, keywords)
        if score == 0:
            continue
        free = _is_freelance(ld)
        if freelance_only and not free:
            continue
        d = _parse_date(ld["posted_at"])
        if d and d < cutoff:
            continue
        if free:
            score += 2
        ld["match_score"] = score
        ranked.append(ld)

    ranked.sort(key=lambda x: (x["match_score"], x["posted_at"] or ""), reverse=True)
    top = ranked[: max(1, min(max_results, 100))]
    if detail != "full":
        top = [_compact(ld, 220) for ld in top]
    return {
        "total_matches": len(ranked),
        "took_ms": int((time.perf_counter() - t0) * 1000),
        "leads": top,
        "source_status": status,
    }


@mcp.tool()
def list_sources() -> dict:
    """List the lead sources this server can search, and which optional keys are set."""
    return {
        "sources": {
            "remotive": "Remote jobs API (remotive.com)",
            "remoteok": "Remote OK public API",
            "weworkremotely": "We Work Remotely RSS feeds",
            "jobicy": "Jobicy remote jobs API",
            "himalayas": "Himalayas remote jobs API",
            "hackernews": "HN monthly 'Freelancer? Seeking freelancer?' + 'Who is hiring?' threads",
            "reddit": "r/forhire, r/hiring, ... ([Hiring] posts only). Needs REDDIT_CLIENT_ID/SECRET in the cloud (else 403, skipped by default)",
            **{k: "plugin source" for k in SOURCES if k not in _BUILTIN_SOURCES},
        },
        "reddit_oauth_configured": bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET),
        "web_search_configured": bool(BRAVE_API_KEY),
        "profile_configured": bool(MY_PROFILE),
    }


@mcp.tool()
async def web_search_leads(query: str, count: int = 15) -> dict:
    """Search the open web for freelance leads (needs BRAVE_API_KEY on the server).

    Good queries: '"looking for a freelance" shopify developer',
    '"hiring" "react developer" contract site:linkedin.com/posts',
    '"need a" logo designer budget'.
    """
    if not BRAVE_API_KEY:
        return {"error": "BRAVE_API_KEY is not set on the server. Get a free key at "
                         "https://brave.com/search/api/ and add it as an env var."}
    async with _client() as c:
        r = await _get(c, "https://api.search.brave.com/res/v1/web/search",
                       params={"q": query, "count": max(1, min(count, 20)), "freshness": "pm"},
                       headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"})
    results = [
        {"title": w.get("title"), "url": w.get("url"), "snippet": _strip_html(w.get("description"), 300),
         "age": w.get("age")}
        for w in r.json().get("web", {}).get("results", [])
    ]
    return {"query": query, "results": results}


async def _robots_allows(c: httpx.AsyncClient, url: str) -> bool:
    p = urlparse(url)
    robots = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = await _get(c, robots)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True  # no robots.txt -> allowed


@mcp.tool()
async def get_lead_details(url: str, max_chars: int = 6000) -> dict:
    """Fetch the full text of one lead's page so you can understand the client's needs.

    Respects robots.txt. For Hacker News and Reddit links it uses their public JSON.
    """
    if urlparse(url).scheme not in ("http", "https"):
        return {"error": "Only http(s) URLs are supported."}
    async with _client() as c:
        try:
            m = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", url)
            if m:
                r = await _get(c, f"https://hn.algolia.com/api/v1/items/{m.group(1)}")
                d = r.json()
                return {"url": url, "author": d.get("author"),
                        "text": _strip_html(d.get("text"), max_chars)}
            if "reddit.com" in url and "/comments/" in url:
                token = await _reddit_auth(c)
                base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
                path = urlparse(url).path.rstrip("/")
                headers = {"Authorization": f"bearer {token}"} if token else {}
                r = await _get(c, f"{base}{path}.json", headers=headers)
                post = r.json()[0]["data"]["children"][0]["data"]
                return {"url": url, "author": post.get("author"), "title": post.get("title"),
                        "text": _strip_html(post.get("selftext"), max_chars)}
            if not await _robots_allows(c, url):
                return {"url": url, "error": "robots.txt disallows fetching this page. Open it in your browser."}
            r = await _get(c, url)
            title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            return {"url": str(r.url), "title": _strip_html(title.group(1), 200) if title else "",
                    "text": _strip_html(r.text, max_chars)}
        except Exception as e:
            return {"url": url, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@mcp.tool()
def get_my_profile() -> dict:
    """Return the freelancer's profile (skills, rates, portfolio) configured on the server.

    Use it to judge which leads fit and to personalise outreach drafts.
    """
    if not MY_PROFILE:
        return {"profile": "", "note": "MY_PROFILE env var is empty. Ask the user for their skills, "
                                       "experience, portfolio links and rates."}
    return {"profile": MY_PROFILE}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
@mcp.prompt()
def draft_outreach(lead_url: str, extra_notes: str = "") -> str:
    """Draft a short, personal pitch for one lead (for the user to review and send)."""
    return (
        f"Open this lead with get_lead_details: {lead_url}\n"
        "Then call get_my_profile. Write a short, honest pitch (max 120 words) that:\n"
        "1. Shows I read their post (mention one specific detail).\n"
        "2. Says how I would solve it, with one relevant example or portfolio link.\n"
        "3. Ends with a simple question or call to action.\n"
        "Match the platform's rules (e.g. Reddit: reply on the post or DM only if allowed). "
        "Do not invent experience I don't have. Do not send anything: show me the draft.\n"
        f"Extra notes: {extra_notes}"
    )


@mcp.prompt()
def daily_lead_hunt(skills: str) -> str:
    """Run a full daily lead hunt for the given skills."""
    return (
        f"My skills: {skills}.\n"
        "1. Call find_freelance_leads with 2-4 focused keywords from my skills, freelance_only=false, "
        "posted_within_days=7.\n"
        "2. If web search is configured, also run web_search_leads with 2 queries.\n"
        "3. If the Upwork connector is available, search Upwork jobs too.\n"
        "4. Pick the 10 best leads for me. For each: title, client, link, why it fits, budget if known.\n"
        "5. For the top 3, draft a pitch (draft_outreach style). Do not send anything."
    )


# ---------------------------------------------------------------------------
# Plugins: every plugins/*.py is loaded automatically (GitHub, cPanel, Vercel,
# and anything you add later). See plugins/_template.py.
# ---------------------------------------------------------------------------
from plugin_loader import load_plugins  # noqa: E402

PLUGIN_STATUS = load_plugins(mcp, "plugins", lead_sources=SOURCES)


@mcp.tool()
async def server_status() -> dict:
    """Health + speed check: loaded plugins, tool count, cache size, timeouts."""
    tools = await mcp.list_tools()
    return {"plugins": PLUGIN_STATUS, "tools": len(tools), "lead_sources": list(SOURCES),
            "cache_entries": len(_cache), "cache_ttl_s": CACHE_TTL,
            "source_timeout_s": SOURCE_TIMEOUT}


# Health check for cloud hosts (Render/Railway ping this).
@mcp.custom_route("/", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "mcp_endpoint": MCP_PATH})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

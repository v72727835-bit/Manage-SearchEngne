"""Offline tests: every source is mocked, so no network is needed.
Run: pip install pytest && python -m pytest -q
"""
import asyncio, json, sys, time, pathlib
import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server  # noqa: E402

NOW = time.time()
ISO = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - 3600))
RSS_DATE = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(NOW - 7200))

def handler(req: httpx.Request) -> httpx.Response:
    u = str(req.url)
    if "remotive.com" in u:
        return httpx.Response(200, json={"jobs": [{"title": "Python Django Developer", "url": "https://remotive.com/j/1",
            "company_name": "Acme", "publication_date": ISO, "description": "<p>Build APIs</p>", "job_type": "contract",
            "candidate_required_location": "Worldwide", "salary": "$40/hr", "tags": ["python"]}]})
    if "remoteok.com" in u:
        return httpx.Response(200, json=[{"legal": "x"}, {"position": "React Developer", "url": "https://remoteok.com/j/2",
            "company": "Beta", "epoch": NOW - 100, "description": "frontend", "tags": ["react"]},
            {"position": "Old Python job", "url": "https://remoteok.com/j/old", "company": "Z", "epoch": NOW - 90*86400,
             "description": "python", "tags": []}])
    if "weworkremotely.com" in u:
        return httpx.Response(200, content=f"""<rss><channel><item><title>Gamma: Senior Python Engineer</title>
            <link>https://weworkremotely.com/j/3</link><pubDate>{RSS_DATE}</pubDate><description>Python backend</description>
            <region>Anywhere</region></item></channel></rss>""".encode())
    if "jobicy.com" in u:
        return httpx.Response(200, json={"jobs": [{"jobTitle": "Freelance Python Scraper", "url": "https://jobicy.com/j/4",
            "companyName": "Delta", "pubDate": ISO, "jobExcerpt": "scraping", "jobType": ["freelance"], "jobGeo": "Anywhere"}]})
    if "himalayas.app" in u:
        return httpx.Response(200, json={"jobs": [{"title": "Designer", "applicationLink": "https://himalayas.app/j/5",
            "companyName": "Eps", "pubDate": int(NOW - 500), "excerpt": "figma", "employmentType": "Full Time"}]})
    if "search_by_date" in u:
        return httpx.Response(200, json={"hits": [{"title": "Ask HN: Freelancer? Seeking freelancer? (November 2026)", "objectID": "100"},
                                                  {"title": "Ask HN: Who is hiring? (November 2026)", "objectID": "200"}]})
    if "hn.algolia.com/api/v1/search" in u:
        if "story_100" in u:
            return httpx.Response(200, json={"hits": [
                {"objectID": "101", "parent_id": 100, "author": "bob", "created_at": ISO,
                 "comment_text": "SEEKING FREELANCER | Python | Remote<p>Need a Django dev"},
                {"objectID": "102", "parent_id": 100, "author": "me", "created_at": ISO,
                 "comment_text": "SEEKING WORK | Python dev"}]})
        return httpx.Response(200, json={"hits": [{"objectID": "201", "parent_id": 200, "author": "co", "created_at": ISO,
                 "comment_text": "Foo Inc | Python Engineer | Contract | Remote<p>details"}]})
    if "reddit.com" in u:
        return httpx.Response(200, json={"data": {"children": [
            {"data": {"title": "[Hiring] Python script for data cleanup", "subreddit": "forhire", "permalink": "/r/forhire/comments/a/x/",
                      "created_utc": NOW - 300, "selftext": "budget $200", "author": "client1", "link_flair_text": "Hiring"}},
            {"data": {"title": "[For Hire] Python dev available", "subreddit": "forhire", "permalink": "/r/forhire/comments/b/y/",
                      "created_utc": NOW - 300, "selftext": "", "author": "dev", "link_flair_text": "For Hire"}}]}})
    return httpx.Response(404)

def mock_http():
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

def run(coro):
    return asyncio.run(coro)

def setup_function(_):
    server._cache.clear()
    server._clients.clear()
    server._http = mock_http

def test_all_sources_ok_and_filtering():
    res = run(server.find_freelance_leads(["python"], sources=list(server.SOURCES), detail="full"))
    assert all(v.startswith("ok") for v in res["source_status"].values()), res["source_status"]
    urls = [l["url"] for l in res["leads"]]
    assert "https://remoteok.com/j/old" not in urls          # too old
    assert "https://remoteok.com/j/2" not in urls            # no keyword match
    assert not any("For Hire" in l["title"] for l in res["leads"])   # skip freelancers advertising
    assert not any("SEEKING WORK" in l["summary"] for l in res["leads"])
    srcs = {l["source"] for l in res["leads"]}
    assert {"remotive", "weworkremotely", "jobicy", "hackernews-freelancer", "hackernews-hiring", "reddit-r/forhire"} <= srcs
    assert res["leads"][0]["match_score"] >= res["leads"][-1]["match_score"]

def test_freelance_only():
    res = run(server.find_freelance_leads(["python"], freelance_only=True))
    assert res["leads"] and all(server._is_freelance(l) for l in res["leads"])

def test_source_failure_isolated():
    def bad():
        def h(req):
            if "remotive" in str(req.url):
                return httpx.Response(500)
            return handler(req)
        return httpx.AsyncClient(transport=httpx.MockTransport(h))
    server._http = bad
    res = run(server.find_freelance_leads(["python"], sources=["remotive", "jobicy"]))
    assert res["source_status"]["remotive"].startswith("error")
    assert res["source_status"]["jobicy"].startswith("ok")
    assert res["leads"]

def test_web_search_without_key():
    assert "error" in run(server.web_search_leads("x"))

def test_github_confirm_guards():
    from plugins import github_tools
    github_tools.TOKEN = "x"
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert {"github_create_repo", "github_delete_repo", "github_api"} <= set(tools)
    res = asyncio.run(server.mcp.call_tool("github_delete_repo", {"repo": "a/b"}))
    assert "needs_confirmation" in str(res)
    res = asyncio.run(server.mcp.call_tool("github_update_repo", {"repo": "a/b", "private": False}))
    assert "needs_confirmation" in str(res)
    res = asyncio.run(server.mcp.call_tool("github_api", {"method": "POST", "path": "/user/repos"}))
    assert "needs_confirmation" in str(res)

def test_hosting_guards():
    from plugins import hosting_tools as h
    h.CPANEL_HOST, h.CPANEL_USER, h.CPANEL_API_TOKEN = "x", "u", "t"
    h.VERCEL_TOKEN = "t"
    for name, args in [("cpanel_uapi", {"module": "Email", "function": "add_pop"}),
                       ("cpanel_files", {"action": "delete", "path": "public_html/a"}),
                       ("cpanel_database", {"action": "delete_db", "name": "a"}),
                       ("cpanel_domains", {"action": "delete_subdomain", "domain": "a.b.c"}),
                       ("vercel_manage", {"action": "delete_project", "project": "p"})]:
        assert "needs_confirmation" in str(asyncio.run(server.mcp.call_tool(name, args))), name


def test_reddit_skipped_by_default_without_oauth():
    res = run(server.find_freelance_leads(["python"]))
    assert "reddit" not in res["source_status"]
    assert res["took_ms"] < 2000

def test_compact_output_is_small():
    full = run(server.find_freelance_leads(["python"], detail="full"))
    small = run(server.find_freelance_leads(["python"]))
    assert len(json.dumps(small)) < len(json.dumps(full))
    assert all("" not in l.values() for l in small["leads"])

def test_slow_source_is_dropped_fast():
    async def slow(c, kw):
        await asyncio.sleep(5)
        return []
    server.SOURCES["slowpoke"] = slow
    old = server.SOURCE_TIMEOUT
    server.SOURCE_TIMEOUT = 0.3
    try:
        t = time.perf_counter()
        res = run(server.find_freelance_leads(["python"], sources=["slowpoke", "jobicy"]))
        assert time.perf_counter() - t < 1.5
        assert res["source_status"]["slowpoke"].startswith("skipped")
        assert res["leads"]
    finally:
        server.SOURCES.pop("slowpoke")
        server.SOURCE_TIMEOUT = old

def test_same_url_fetched_once_in_parallel():
    calls = []
    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"ok": 1})
    async def go():
        c = httpx.AsyncClient(transport=httpx.MockTransport(h))
        await asyncio.gather(*[server._get(c, "https://x.test/a") for _ in range(5)])
        await server._get(c, "https://x.test/a")  # cached
    run(go())
    assert len(calls) == 1

def test_plugins_auto_loaded():
    assert server.PLUGIN_STATUS["github_tools"]["ok"]
    assert server.PLUGIN_STATUS["hosting_tools"]["ok"]
    assert "_template" not in server.PLUGIN_STATUS
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"github_whoami", "cpanel_overview", "vercel_manage", "server_status"} <= names

def test_new_plugin_file_is_picked_up(tmp_path, monkeypatch):
    import plugin_loader
    from mcp.server.fastmcp import FastMCP
    pkg = tmp_path / "myplugs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "extra.py").write_text(
        "async def src(c, kw):\n    return []\n"
        "LEAD_SOURCES = {'extra_board': src}\n"
        "def register(mcp):\n    @mcp.tool()\n    def ping() -> str:\n        return 'pong'\n")
    (pkg / "broken.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setattr(plugin_loader, "ROOT", tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    m, srcs = FastMCP("t"), {}
    st = plugin_loader.load_plugins(m, "myplugs", lead_sources=srcs)
    assert st["extra"]["ok"] and "extra_board" in srcs
    assert not st["broken"]["ok"]
    assert "ping" in {t.name for t in asyncio.run(m.list_tools())}

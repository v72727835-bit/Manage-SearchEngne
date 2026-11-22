"""
Browser Agent MCP - runs on YOUR computer and lets Claude see and control a
real browser: open any site, read it, click, type, upload photos, take
screenshots. Use it to fix your profiles, research, and find clients on any
platform (Upwork, Fiverr, LinkedIn, Freelancer, Reddit, ...).

Why local? A cloud server cannot see your screen or use your logins. This one
runs next to your browser.

Two modes:
  1. Own Chrome (recommended, uses your logins):
       Start Chrome with a debug port, then set CHROME_CDP_URL=http://127.0.0.1:9222
  2. Built-in browser: just run it. A Chromium window opens with its own saved
     profile (BROWSER_PROFILE_DIR). Log in once; logins are remembered.

Safety: clicking buttons like Send / Submit / Post / Publish / Save / Delete /
Pay needs confirm=true. Claude must ask you first.

Run:  python browser_mcp.py   (stdio transport, add it to Claude Desktop config)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

CDP_URL = os.getenv("CHROME_CDP_URL", "")
PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", str(Path.home() / ".lead-finder-browser"))
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
CHROME_PATH = os.getenv("CHROME_PATH") or None
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", str(Path.home()))  # files must be inside this folder
TRANSPORT = os.getenv("TRANSPORT", "stdio")  # "http" for cloud hosting
PORT = int(os.getenv("PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
SENSITIVE = re.compile(
    r"\b(send|submit|post|publish|apply|save|update|confirm|delete|remove|pay|buy|"
    r"purchase|checkout|order|withdraw|transfer|accept|hire|bid|propose|connect)\b",
    re.I,
)

mcp = FastMCP(
    "browser-agent",
    instructions=(
        "Controls a real browser on the user's computer. Workflow: browser_open -> "
        "browser_snapshot (gives numbered refs) -> browser_click / browser_type / "
        "browser_upload with those refs -> browser_snapshot again to check. Use "
        "browser_screenshot when layout or images matter. Before any action that "
        "sends, posts, saves, pays or deletes, show the user exactly what will happen "
        "and only call with confirm=true after they agree. Never enter passwords; ask "
        "the user to log in themselves (or import their cookies with browser_import_cookies)."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
)


class _State:
    pw: Playwright | None = None
    browser: Browser | None = None
    ctx: BrowserContext | None = None
    page: Page | None = None


S = _State()

SNAPSHOT_JS = r"""
(maxItems) => {
  const sel = 'a[href],button,input,textarea,select,[role=button],[role=link],[role=tab],' +
              '[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=combobox],' +
              '[contenteditable=""],[contenteditable=true],summary,label[for]';
  document.querySelectorAll('[data-mcp-ref]').forEach(e => e.removeAttribute('data-mcp-ref'));
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.type === 'hidden') continue;
    n += 1; el.setAttribute('data-mcp-ref', String(n));
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                  el.title || el.alt || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const inView = r.bottom > 0 && r.top < innerHeight;
    out.push({ref: n, tag: el.tagName.toLowerCase(), type: el.type || el.getAttribute('role') || '',
              name, href: el.href ? String(el.href).slice(0, 120) : undefined,
              value: (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.type !== 'password'
                     ? String(el.value || '').slice(0, 60) : undefined,
              in_view: inView});
    if (out.length >= maxItems) break;
  }
  return out;
}
"""


async def _page() -> Page:
    if S.page and not S.page.is_closed():
        return S.page
    if not S.pw:
        S.pw = await async_playwright().start()
    if CDP_URL:
        S.browser = await S.pw.chromium.connect_over_cdp(CDP_URL)
        S.ctx = S.browser.contexts[0] if S.browser.contexts else await S.browser.new_context()
    elif not S.ctx:
        S.ctx = await S.pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, executable_path=CHROME_PATH,
            viewport={"width": 1366, "height": 860},
        )
    pages = [p for p in S.ctx.pages if not p.is_closed()]
    S.page = pages[-1] if pages else await S.ctx.new_page()
    return S.page


def _loc(page: Page, ref: int):
    return page.locator(f'[data-mcp-ref="{int(ref)}"]').first


async def _describe(page: Page, ref: int) -> str:
    loc = _loc(page, ref)
    if await loc.count() == 0:
        raise ValueError(f"ref {ref} not found. Call browser_snapshot again (page may have changed).")
    return (await loc.evaluate(
        "e => (e.getAttribute('aria-label')||e.innerText||e.value||e.title||'').trim().slice(0,80)"
    )) or ""


async def _summary(page: Page) -> dict:
    return {"url": page.url, "title": await page.title()}


@mcp.tool()
async def browser_open(url: str) -> dict:
    """Open a URL in the current tab (adds https:// if missing)."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    return await _summary(page)


@mcp.tool()
async def browser_snapshot(max_elements: int = 150, text_chars: int = 4000) -> dict:
    """See the page: visible text plus numbered interactive elements (refs).

    Use the 'ref' numbers with browser_click, browser_type, browser_select, browser_upload.
    Refs change when the page changes, so snapshot again after actions.
    """
    page = await _page()
    elements = await page.evaluate(SNAPSHOT_JS, max(10, min(max_elements, 400)))
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    text = re.sub(r"\n\s*\n+", "\n", text or "").strip()
    return {**await _summary(page), "text": text[:text_chars],
            "text_truncated": len(text) > text_chars, "elements": elements}


@mcp.tool()
async def browser_screenshot(full_page: bool = False) -> Image:
    """Take a screenshot of the current page (to check layout, photos, banners)."""
    page = await _page()
    return Image(data=await page.screenshot(full_page=full_page, type="jpeg", quality=70), format="jpeg")


@mcp.tool()
async def browser_click(ref: int, confirm: bool = False) -> dict:
    """Click an element by ref. Buttons like Send/Submit/Post/Save/Delete/Pay need confirm=true
    (ask the user first)."""
    page = await _page()
    name = await _describe(page, ref)
    if SENSITIVE.search(name) and not confirm:
        return {"needs_confirmation": True, "element": name,
                "message": f"'{name}' looks like a send/save/post/pay action. Ask the user, "
                           "then call again with confirm=true."}
    await _loc(page, ref).click(timeout=10000)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    return {"clicked": name, **await _summary(page)}


@mcp.tool()
async def browser_type(ref: int, text: str, clear_first: bool = True, press_enter: bool = False,
                       confirm: bool = False) -> dict:
    """Type text into an input / textarea / editor by ref. press_enter=true submits, so it
    needs confirm=true."""
    page = await _page()
    loc = _loc(page, ref)
    await _describe(page, ref)
    if await loc.evaluate("e => e.type === 'password'"):
        return {"error": "Will not type passwords. Ask the user to log in themselves."}
    if press_enter and not confirm:
        return {"needs_confirmation": True, "message": "Pressing Enter may submit the form. "
                "Ask the user, then call again with confirm=true."}
    editable = await loc.evaluate("e => e.isContentEditable")
    if editable:
        await loc.click()
        if clear_first:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        await page.keyboard.insert_text(text)
    elif clear_first:
        await loc.fill(text)
    else:
        await loc.press_sequentially(text)
    if press_enter:
        await loc.press("Enter")
    return {"typed_chars": len(text), **await _summary(page)}


@mcp.tool()
async def browser_select(ref: int, option: str) -> dict:
    """Choose an option in a <select> dropdown by its visible label or value."""
    page = await _page()
    loc = _loc(page, ref)
    try:
        chosen = await loc.select_option(label=option)
    except Exception:
        chosen = await loc.select_option(value=option)
    return {"selected": chosen}


@mcp.tool()
async def browser_upload(ref: int, file_path: str) -> dict:
    """Upload a file (e.g. a new profile photo) to a file input, or to the button that opens
    the file picker. file_path must be inside UPLOAD_ROOT (default: your home folder)."""
    p = Path(file_path).expanduser().resolve()
    root = Path(UPLOAD_ROOT).expanduser().resolve()
    if root not in p.parents and p != root:
        return {"error": f"File must be inside {root}"}
    if not p.is_file():
        return {"error": f"File not found: {p}"}
    page = await _page()
    loc = _loc(page, ref)
    is_input = await loc.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'")
    if is_input:
        await loc.set_input_files(str(p))
    else:
        async with page.expect_file_chooser(timeout=10000) as fc:
            await loc.click()
        await (await fc.value).set_files(str(p))
    return {"uploaded": p.name, "note": "Now snapshot/screenshot to check, then click Save (with the user's OK)."}


@mcp.tool()
async def browser_press_key(key: str) -> dict:
    """Press a key, e.g. 'Escape', 'Tab', 'PageDown', 'Control+A'. (Enter: use browser_type.)"""
    if key.lower() == "enter":
        return {"error": "Use browser_type(..., press_enter=true, confirm=true) for Enter."}
    page = await _page()
    await page.keyboard.press(key)
    return await _summary(page)


@mcp.tool()
async def browser_scroll(direction: str = "down", amount: int = 800) -> dict:
    """Scroll the page up or down by pixels."""
    page = await _page()
    await page.mouse.wheel(0, amount if direction == "down" else -amount)
    await page.wait_for_timeout(400)
    return await _summary(page)


@mcp.tool()
async def browser_back() -> dict:
    """Go back one page."""
    page = await _page()
    await page.go_back(wait_until="domcontentloaded")
    return await _summary(page)


@mcp.tool()
async def browser_tabs(switch_to: int | None = None, open_new_url: str | None = None) -> dict:
    """List open tabs. Optionally switch to tab index, or open a URL in a new tab."""
    await _page()
    if open_new_url:
        S.page = await S.ctx.new_page()
        await browser_open(open_new_url)
    pages = [p for p in S.ctx.pages if not p.is_closed()]
    if switch_to is not None and 0 <= switch_to < len(pages):
        S.page = pages[switch_to]
        await S.page.bring_to_front()
    return {"tabs": [{"index": i, "url": p.url, "active": p is S.page} for i, p in enumerate(pages)]}


@mcp.tool()
async def browser_find_text(text: str) -> dict:
    """Find where some text appears on the page and scroll to it."""
    page = await _page()
    loc = page.get_by_text(text, exact=False).first
    if await loc.count() == 0:
        return {"found": False}
    await loc.scroll_into_view_if_needed()
    return {"found": True, "context": (await loc.inner_text())[:300]}


@mcp.tool()
async def browser_wait(seconds: float = 2.0) -> dict:
    """Wait for the page to finish loading or animating (max 20s)."""
    page = await _page()
    await page.wait_for_timeout(int(max(0, min(seconds, 20)) * 1000))
    return await _summary(page)


@mcp.tool()
async def browser_upload_from_url(ref: int, file_url: str) -> dict:
    """Cloud mode: download an image/file from a public URL (e.g. a photo link the user shared)
    and upload it into a file input or the button that opens the file picker."""
    if not re.match(r"^https?://", file_url):
        return {"error": "file_url must be http(s)"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(file_url)
        r.raise_for_status()
    if len(r.content) > 15 * 1024 * 1024:
        return {"error": "File larger than 15 MB"}
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "application/pdf": ".pdf"}.get(
        r.headers.get("content-type", "").split(";")[0], ".jpg")
    tmp = Path(tempfile.mkdtemp()) / f"upload{ext}"
    tmp.write_bytes(r.content)
    page = await _page()
    loc = _loc(page, ref)
    await _describe(page, ref)
    if await loc.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'"):
        await loc.set_input_files(str(tmp))
    else:
        async with page.expect_file_chooser(timeout=10000) as fc:
            await loc.click()
        await (await fc.value).set_files(str(tmp))
    return {"uploaded": tmp.name, "bytes": len(r.content),
            "note": "Check with a screenshot, then click Save only with the user's OK."}


@mcp.tool()
async def browser_import_cookies(cookies_json: str) -> dict:
    """Log in without a password: import cookies the user exported from their own browser
    (e.g. with the 'Cookie-Editor' extension -> Export -> JSON). Cookies are kept only in this
    browser's profile, never written to logs."""
    try:
        raw = json.loads(cookies_json)
    except json.JSONDecodeError as e:
        return {"error": f"Not valid JSON: {e}"}
    if isinstance(raw, dict):
        raw = raw.get("cookies", [raw])
    same_site = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax", "strict": "Strict", "none": "None"}
    cookies = []
    for ck in raw:
        if not ck.get("name") or not ck.get("domain"):
            continue
        c = {"name": ck["name"], "value": str(ck.get("value", "")), "domain": ck["domain"],
             "path": ck.get("path", "/"), "secure": bool(ck.get("secure", True)),
             "httpOnly": bool(ck.get("httpOnly", False))}
        ss = same_site.get(str(ck.get("sameSite", "")).lower())
        if ss:
            c["sameSite"] = ss
        exp = ck.get("expirationDate") or ck.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            c["expires"] = float(exp)
        cookies.append(c)
    await _page()
    await S.ctx.add_cookies(cookies)
    domains = sorted({c["domain"].lstrip(".") for c in cookies})
    return {"imported": len(cookies), "domains": domains,
            "note": "Open the site again with browser_open to use the login."}


@mcp.tool()
async def browser_clear_cookies(domain_contains: str = "") -> dict:
    """Log out: delete saved cookies (all, or only those whose domain contains the text)."""
    await _page()
    if not domain_contains:
        await S.ctx.clear_cookies()
        return {"cleared": "all"}
    await S.ctx.clear_cookies(domain=re.compile(re.escape(domain_contains)))
    return {"cleared": domain_contains}


@mcp.custom_route("/", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "service": "browser-agent"})


@mcp.prompt()
def improve_profile(platform_url: str, my_skills: str) -> str:
    """Review and improve my freelance profile on any platform."""
    return (
        f"Open my profile: {platform_url}. My skills: {my_skills}.\n"
        "1. browser_snapshot + browser_screenshot and read the whole profile (scroll).\n"
        "2. Score it: headline, photo, overview, skills, portfolio, rates, reviews.\n"
        "3. Write improved versions (headline, overview, skills list) that fit clients searching on that site.\n"
        "4. Show me the changes. Only after I say yes, edit the fields and ask again before clicking Save."
    )


@mcp.prompt()
def find_clients_on_site(platform_url: str, skills: str) -> str:
    """Search for clients/jobs on whatever site is open."""
    return (
        f"Go to {platform_url} and use its search for jobs/requests matching: {skills}.\n"
        "Read the results (snapshot, scroll, open the best ones). Give me the 10 best leads with link, "
        "budget, why it fits, and a short draft pitch for the top 3. Don't send anything."
    )


if __name__ == "__main__":
    if TRANSPORT == "http":
        if MCP_PATH in ("/mcp", "/", ""):
            sys.exit("Refusing to start: set MCP_PATH to a secret path (e.g. /mcp-x7k2p9q4) in cloud mode.")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()

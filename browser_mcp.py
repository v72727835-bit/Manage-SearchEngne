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
from typing import Any
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
# Speed: skip downloading fonts/video (and images if "images") -> pages load 2-5x faster.
BLOCK = {x.strip() for x in os.getenv("BROWSER_BLOCK", "font,media").lower().split(",") if x.strip()}
SETTLE_MS = int(os.getenv("BROWSER_SETTLE_MS", "1200"))  # max wait for network to go quiet
SENSITIVE = re.compile(
    r"\b(send|submit|post|publish|apply|save|update|confirm|delete|remove|pay|buy|"
    r"purchase|checkout|order|withdraw|transfer|accept|hire|bid|propose|connect)\b",
    re.I,
)

mcp = FastMCP(
    "browser-agent",
    instructions=(
        "Controls a real browser. FAST flow: browser_open already returns a compact "
        "snapshot (lines like `[12] button \"Save\"`). Then do several steps in ONE "
        "call with browser_act (click/type/select/key/scroll/wait/open) - it returns a "
        "fresh snapshot at the end, so no separate snapshot call is needed. Use "
        "browser_screenshot only when layout or images matter. Before any action that "
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
    const field = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
    const lbl = field && el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const name = (el.getAttribute('aria-label') ||
                  (field ? (lbl || el.placeholder || el.name || el.title) : (el.innerText || el.title || el.alt)) ||
                  el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const inView = r.bottom > 0 && r.top < innerHeight;
    out.push({ref: n, tag: el.tagName.toLowerCase(), type: el.type || el.getAttribute('role') || '',
              name, href: el.href ? String(el.href).slice(0, 120) : undefined,
              value: el.tagName === 'SELECT' ? (el.selectedOptions[0] || {}).text :
                     (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.type !== 'password'
                     ? String(el.value || '').slice(0, 60) : undefined,
              in_view: inView});
    if (out.length >= maxItems) break;
  }
  return out;
}
"""


def _fmt_elements(elements: list[dict], only_in_view: bool) -> str:
    lines = []
    for e in elements:
        if only_in_view and not e.get("in_view"):
            continue
        tag, typ = e["tag"], e.get("type") or ""
        if tag == "a":
            kind = "link"
        elif tag == "button" or typ in ("submit", "button"):
            kind = "button"
        elif tag in ("input", "select", "textarea"):
            kind = typ or tag
        else:
            kind = typ or tag
        line = f'[{e["ref"]}] {kind} "{e["name"]}"'
        if e.get("value"):
            line += f' ={e["value"]!r}'
        if e["tag"] == "a" and e.get("href") and not e["name"]:
            line += f' -> {e["href"]}'
        lines.append(line)
    return "\n".join(lines)


async def _snap(page: Page, max_elements: int = 120, text_chars: int = 2500,
                only_in_view: bool = False) -> dict:
    """One JS round trip for elements + text (was two)."""
    data = await page.evaluate(
        "([n]) => ({els: (" + SNAPSHOT_JS + ")(n), text: document.body ? document.body.innerText : ''})",
        [max(10, min(max_elements, 400))],
    )
    text = re.sub(r"\n\s*\n+", "\n", data["text"] or "").strip()
    out = {"url": page.url, "title": await page.title(),
           "elements": _fmt_elements(data["els"], only_in_view)}
    if text_chars:
        out["text"] = text[:text_chars]
        if len(text) > text_chars:
            out["text_more"] = len(text) - text_chars
    return out


async def _settle(page: Page, ms: int | None = None) -> None:
    """Wait only as long as needed: DOM ready, then network quiet up to SETTLE_MS."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=ms if ms is not None else SETTLE_MS)
    except Exception:
        pass


async def _route(route):
    if route.request.resource_type in BLOCK:
        await route.abort()
    else:
        await route.continue_()


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
    if BLOCK and not getattr(S, "routed", False):
        await S.ctx.route("**/*", _route)
        S.routed = True
    pages = [p for p in S.ctx.pages if not p.is_closed()]
    S.page = pages[-1] if pages else await S.ctx.new_page()
    S.page.set_default_timeout(8000)
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
async def browser_open(url: str, snapshot: bool = True) -> dict:
    """Open a URL (adds https:// if missing). Returns a compact snapshot right away."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _settle(page)
    return await _snap(page) if snapshot else await _summary(page)


@mcp.tool()
async def browser_snapshot(max_elements: int = 120, text_chars: int = 2500,
                           only_in_view: bool = False) -> dict:
    """See the page: visible text + numbered interactive elements, one per line:
    `[12] button "Save"`. Use the numbers as refs. Refs change after the page changes.
    only_in_view=true lists just what is on screen (smaller, faster)."""
    return await _snap(await _page(), max_elements, text_chars, only_in_view)


@mcp.tool()
async def browser_screenshot(full_page: bool = False) -> Image:
    """Take a screenshot of the current page (to check layout, photos, banners)."""
    page = await _page()
    return Image(data=await page.screenshot(full_page=full_page, type="jpeg", quality=70), format="jpeg")


async def _do_click(page: Page, ref: int, confirm: bool) -> dict:
    name = await _describe(page, ref)
    if SENSITIVE.search(name) and not confirm:
        return {"needs_confirmation": True, "element": name,
                "message": f"'{name}' looks like a send/save/post/pay action. Ask the user, "
                           "then call again with confirm=true."}
    await _loc(page, ref).click(timeout=8000)
    await _settle(page, 800)
    return {"clicked": name}


async def _do_type(page: Page, ref: int, text: str, clear_first: bool, press_enter: bool,
                   confirm: bool) -> dict:
    loc = _loc(page, ref)
    await _describe(page, ref)
    info = await loc.evaluate("e => ({pw: e.type === 'password', ce: e.isContentEditable})")
    if info["pw"]:
        return {"error": "Will not type passwords. Ask the user to log in themselves."}
    if press_enter and not confirm:
        return {"needs_confirmation": True, "message": "Pressing Enter may submit the form. "
                "Ask the user, then call again with confirm=true."}
    if info["ce"]:
        await loc.click()
        if clear_first:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        await page.keyboard.insert_text(text)
    elif clear_first:
        await loc.fill(text)  # instant, no per-key delay
    else:
        await loc.focus()
        await page.keyboard.insert_text(text)
    if press_enter:
        await loc.press("Enter")
        await _settle(page, 800)
    return {"typed_chars": len(text)}


async def _do_select(page: Page, ref: int, option: str) -> dict:
    loc = _loc(page, ref)
    try:
        chosen = await loc.select_option(label=option, timeout=5000)
    except Exception:
        chosen = await loc.select_option(value=option, timeout=5000)
    return {"selected": chosen}


@mcp.tool()
async def browser_click(ref: int, confirm: bool = False, snapshot: bool = False) -> dict:
    """Click an element by ref. Buttons like Send/Submit/Post/Save/Delete/Pay need confirm=true
    (ask the user first). snapshot=true returns the new page view in the same call."""
    page = await _page()
    res = await _do_click(page, ref, confirm)
    if res.get("needs_confirmation"):
        return res
    return {**res, **(await _snap(page) if snapshot else await _summary(page))}


@mcp.tool()
async def browser_type(ref: int, text: str, clear_first: bool = True, press_enter: bool = False,
                       confirm: bool = False) -> dict:
    """Type text into an input / textarea / editor by ref (instant paste, not key by key).
    press_enter=true submits, so it needs confirm=true."""
    page = await _page()
    res = await _do_type(page, ref, text, clear_first, press_enter, confirm)
    return res if "typed_chars" not in res else {**res, **await _summary(page)}


@mcp.tool()
async def browser_act(steps: list[dict], confirm: bool = False, snapshot: bool = True) -> dict:
    """Do many browser steps in ONE call (much faster than one tool call per step).

    steps: list of objects, run in order, e.g.
      [{"type": 5, "text": "Full-stack developer"},
       {"type": 7, "text": "I build web apps...", "clear": true},
       {"select": 9, "option": "Expert"},
       {"click": 12},
       {"key": "Tab"}, {"scroll": 800}, {"wait": 500}, {"open": "https://contra.com"}]
    A step that sends/saves/posts/pays (or {"type":..,"enter":true}) stops the batch with
    needs_confirmation unless confirm=true. Show the user the whole plan before confirm=true.
    Returns what each step did, plus a fresh compact snapshot.
    """
    page = await _page()
    done: list[Any] = []
    for i, st in enumerate(steps[:40]):
        try:
            if "click" in st:
                r = await _do_click(page, int(st["click"]), confirm)
            elif "type" in st:
                r = await _do_type(page, int(st["type"]), str(st.get("text", "")),
                                   bool(st.get("clear", True)), bool(st.get("enter", False)), confirm)
            elif "select" in st:
                r = await _do_select(page, int(st["select"]), str(st.get("option", "")))
            elif "key" in st:
                if str(st["key"]).lower() == "enter" and not confirm:
                    r = {"needs_confirmation": True, "message": "Enter may submit. Needs confirm=true."}
                else:
                    await page.keyboard.press(str(st["key"]))
                    r = {"key": st["key"]}
            elif "scroll" in st:
                await page.mouse.wheel(0, int(st["scroll"]))
                await page.wait_for_timeout(120)
                r = {"scrolled": int(st["scroll"])}
            elif "wait" in st:
                await page.wait_for_timeout(int(max(0, min(float(st["wait"]), 10000))))
                r = {"waited_ms": st["wait"]}
            elif "open" in st:
                u = str(st["open"])
                await page.goto(u if re.match(r"^https?://", u) else "https://" + u,
                                wait_until="domcontentloaded", timeout=30000)
                await _settle(page)
                r = {"opened": page.url}
            else:
                r = {"error": f"unknown step {st}"}
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        if r.get("needs_confirmation") or r.get("error"):
            out = {"done": done, "stopped_at": i, "step": st, **r}
            if snapshot:
                out.update(await _snap(page))
            return out
        done.append(r)
    out: dict[str, Any] = {"done": done}
    out.update(await _snap(page) if snapshot else await _summary(page))
    return out


@mcp.tool()
async def browser_select(ref: int, option: str) -> dict:
    """Choose an option in a <select> dropdown by its visible label or value."""
    return await _do_select(await _page(), ref, option)


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
    await page.wait_for_timeout(120)
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
        await browser_open(open_new_url, snapshot=False)
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


# New browser tools: drop a file into browser_plugins/ and it is loaded automatically.
from plugin_loader import load_plugins  # noqa: E402

PLUGIN_STATUS = load_plugins(mcp, "browser_plugins")


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

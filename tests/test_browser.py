"""Browser agent tests (need Playwright + Chromium; skipped otherwise).
Run: CHROME_PATH=/path/to/chrome python -m pytest -q tests/test_browser.py
"""
import asyncio, os, pathlib, sys, tempfile
import pytest

pytest.importorskip("playwright")
if not os.getenv("CHROME_PATH") and not os.getenv("RUN_BROWSER_TESTS"):
    pytest.skip("set CHROME_PATH (or RUN_BROWSER_TESTS=1) to run browser tests", allow_module_level=True)
os.environ["BROWSER_HEADLESS"] = "true"
os.environ["BROWSER_PROFILE_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import browser_mcp as B  # noqa: E402

PAGE = """<html><head><title>Profile</title></head><body>
<label for=h>Headline</label><input id=h><textarea placeholder="Bio"></textarea>
<select><option>Beginner</option><option>Expert</option></select>
<input type=password placeholder=Password>
<button onclick="document.title='Saved'">Save</button></body></html>"""


def _ref(snap, word):
    for line in snap["elements"].splitlines():
        if word in line:
            return int(line[1:line.index("]")])
    raise AssertionError(word)


def test_open_and_batch_with_confirmation_gate(tmp_path):
    f = tmp_path / "p.html"
    f.write_text(PAGE)

    async def go():
        page = await B._page()
        await page.goto(f.as_uri())
        s = await B._snap(page)
        assert '"Headline"' in s["elements"]
        steps = [{"type": _ref(s, "Headline"), "text": "Full-stack dev"},
                 {"select": _ref(s, "Beginner"), "option": "Expert"},
                 {"click": _ref(s, "Save")}]
        r = await B.browser_act(steps)                  # no confirm -> stops at Save
        assert r["needs_confirmation"] and r["stopped_at"] == 2 and r["title"] == "Profile"
        r = await B.browser_act(steps, confirm=True)
        assert r["title"] == "Saved" and "='Full-stack dev'" in r["elements"]
        r = await B.browser_act([{"type": _ref(s, "Password"), "text": "x"}], confirm=True)
        assert "Will not type passwords" in r["error"]
        await B.S.ctx.close()
    asyncio.run(go())

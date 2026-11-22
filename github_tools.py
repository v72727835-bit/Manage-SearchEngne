"""GitHub management tools for the lead-finder MCP server.

Needs GITHUB_TOKEN (a fine-grained personal access token you create once at
https://github.com/settings/personal-access-tokens). GitHub does not let any
tool create tokens through the API, so this step is manual.

Risky actions (delete repo, make a repo public, any non-GET raw API call)
need confirm=true, so Claude must ask you first.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "")


def _need_token() -> dict | None:
    if not TOKEN:
        return {"error": "GITHUB_TOKEN is not set on the server. Create a fine-grained token at "
                         "https://github.com/settings/personal-access-tokens and add it as an env var."}
    return None


async def _gh(method: str, path: str, json: Any = None, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, f"{API}{path}", json=json, params=params, headers={
            "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lead-finder-mcp"})
    if r.status_code == 204:
        return {"ok": True}
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:500]}
    if r.status_code >= 400:
        return {"error": f"GitHub {r.status_code}", "details": data}
    return data


def _slim_repo(r: dict) -> dict:
    return {k: r.get(k) for k in ("full_name", "private", "html_url", "description",
                                  "default_branch", "updated_at")}


def register(mcp) -> None:
    @mcp.tool()
    async def github_whoami() -> dict:
        """Show which GitHub account the token belongs to."""
        if e := _need_token():
            return e
        u = await _gh("GET", "/user")
        return {k: u.get(k) for k in ("login", "name", "html_url", "public_repos")} if "login" in u else u

    @mcp.tool()
    async def github_list_repos(owner: str = "", limit: int = 50) -> dict:
        """List your repos (or an org's repos if owner is an org), newest first."""
        if e := _need_token():
            return e
        path = f"/orgs/{owner}/repos" if owner else "/user/repos"
        data = await _gh("GET", path, params={"per_page": max(1, min(limit, 100)), "sort": "updated"})
        return {"repos": [_slim_repo(r) for r in data]} if isinstance(data, list) else data

    @mcp.tool()
    async def github_create_repo(name: str, private: bool = True, description: str = "",
                                 org: str = "", auto_init: bool = True) -> dict:
        """Create a new repository (private by default). Set org to create it in an organisation."""
        if e := _need_token():
            return e
        body = {"name": name, "private": private, "description": description, "auto_init": auto_init}
        r = await _gh("POST", f"/orgs/{org}/repos" if org else "/user/repos", json=body)
        return _slim_repo(r) if "full_name" in r else r

    @mcp.tool()
    async def github_update_repo(repo: str, private: bool | None = None, description: str | None = None,
                                 new_name: str | None = None, confirm: bool = False) -> dict:
        """Change a repo's visibility, description or name. repo = 'owner/name'.
        Making a repo public needs confirm=true (ask the user first)."""
        if e := _need_token():
            return e
        if private is False and not confirm:
            return {"needs_confirmation": True,
                    "message": f"Making {repo} PUBLIC shows all its code to everyone. Ask the user, then retry with confirm=true."}
        body = {k: v for k, v in {"private": private, "description": description, "name": new_name}.items()
                if v is not None}
        r = await _gh("PATCH", f"/repos/{repo}", json=body)
        return _slim_repo(r) if "full_name" in r else r

    @mcp.tool()
    async def github_delete_repo(repo: str, confirm: bool = False) -> dict:
        """Permanently delete a repo ('owner/name'). Cannot be undone: needs confirm=true after the
        user agrees. The token needs the 'Administration: write' permission."""
        if e := _need_token():
            return e
        if not confirm:
            return {"needs_confirmation": True,
                    "message": f"Deleting {repo} is permanent. Ask the user, then retry with confirm=true."}
        return await _gh("DELETE", f"/repos/{repo}")

    @mcp.tool()
    async def github_get_file(repo: str, path: str, ref: str = "") -> dict:
        """Read a file (or list a folder) from a repo."""
        if e := _need_token():
            return e
        d = await _gh("GET", f"/repos/{repo}/contents/{path.lstrip('/')}", params={"ref": ref} if ref else None)
        if isinstance(d, list):
            return {"folder": [{"name": x["name"], "type": x["type"], "path": x["path"]} for x in d]}
        if d.get("encoding") == "base64":
            text = base64.b64decode(d["content"]).decode("utf-8", "replace")
            return {"path": d["path"], "sha": d["sha"], "content": text[:50000]}
        return d

    @mcp.tool()
    async def github_put_file(repo: str, path: str, content: str, message: str, branch: str = "") -> dict:
        """Create or update a file in a repo with a commit."""
        if e := _need_token():
            return e
        p = f"/repos/{repo}/contents/{path.lstrip('/')}"
        existing = await _gh("GET", p, params={"ref": branch} if branch else None)
        body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if branch:
            body["branch"] = branch
        if isinstance(existing, dict) and existing.get("sha"):
            body["sha"] = existing["sha"]
        r = await _gh("PUT", p, json=body)
        return {"commit": r.get("commit", {}).get("html_url"), "path": path} if "commit" in r else r

    @mcp.tool()
    async def github_create_branch(repo: str, new_branch: str, from_branch: str = "") -> dict:
        """Create a branch from another branch (default: the repo's default branch)."""
        if e := _need_token():
            return e
        if not from_branch:
            from_branch = (await _gh("GET", f"/repos/{repo}")).get("default_branch", "main")
        ref = await _gh("GET", f"/repos/{repo}/git/ref/heads/{from_branch}")
        if "object" not in ref:
            return ref
        return await _gh("POST", f"/repos/{repo}/git/refs",
                         json={"ref": f"refs/heads/{new_branch}", "sha": ref["object"]["sha"]})

    @mcp.tool()
    async def github_create_pull_request(repo: str, title: str, head: str, base: str = "",
                                         body: str = "", draft: bool = True) -> dict:
        """Open a pull request (draft by default)."""
        if e := _need_token():
            return e
        if not base:
            base = (await _gh("GET", f"/repos/{repo}")).get("default_branch", "main")
        r = await _gh("POST", f"/repos/{repo}/pulls",
                      json={"title": title, "head": head, "base": base, "body": body, "draft": draft})
        return {"url": r.get("html_url"), "number": r.get("number")} if "html_url" in r else r

    @mcp.tool()
    async def github_issues(repo: str, action: str = "list", title: str = "", body: str = "",
                            number: int = 0, state: str = "open") -> dict:
        """Work with issues. action: list | create | comment | close."""
        if e := _need_token():
            return e
        if action == "list":
            d = await _gh("GET", f"/repos/{repo}/issues", params={"state": state, "per_page": 50})
            return {"issues": [{"number": i["number"], "title": i["title"], "state": i["state"],
                                "url": i["html_url"]} for i in d]} if isinstance(d, list) else d
        if action == "create":
            r = await _gh("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})
            return {"url": r.get("html_url"), "number": r.get("number")} if "html_url" in r else r
        if action == "comment":
            return await _gh("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        if action == "close":
            return await _gh("PATCH", f"/repos/{repo}/issues/{number}", json={"state": "closed"})
        return {"error": "action must be list, create, comment or close"}

    @mcp.tool()
    async def github_api(method: str, path: str, body: dict | None = None, confirm: bool = False) -> dict:
        """Call any GitHub REST endpoint (e.g. GET /repos/o/r/actions/runs, PUT /repos/o/r/collaborators/u).
        Anything other than GET needs confirm=true after the user agrees."""
        if e := _need_token():
            return e
        m = method.upper()
        if m != "GET" and not confirm:
            return {"needs_confirmation": True, "message": f"{m} {path} changes GitHub. Ask the user, then retry with confirm=true."}
        if not path.startswith("/"):
            path = "/" + path
        r = await _gh(m, path, json=body)
        return {"result": r} if isinstance(r, list) else r

"""cPanel and Vercel management tools.

cPanel (UAPI): set CPANEL_HOST (e.g. server.example.com), CPANEL_USER and,
preferably, CPANEL_API_TOKEN (cPanel -> Security -> Manage API Tokens).
CPANEL_PASSWORD works too but a token is safer and can be revoked.

Vercel: set VERCEL_TOKEN (https://vercel.com/account/tokens), optional VERCEL_TEAM_ID.

Anything that deletes or removes needs confirm=true, and so does any raw
call that is not a read.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

CPANEL_HOST = os.getenv("CPANEL_HOST", "")
CPANEL_PORT = os.getenv("CPANEL_PORT", "2083")
CPANEL_USER = os.getenv("CPANEL_USER", "")
CPANEL_API_TOKEN = os.getenv("CPANEL_API_TOKEN", "")
CPANEL_PASSWORD = os.getenv("CPANEL_PASSWORD", "")
CPANEL_VERIFY_SSL = os.getenv("CPANEL_VERIFY_SSL", "true").lower() != "false"
VERCEL_TOKEN = os.getenv("VERCEL_TOKEN", "")
VERCEL_TEAM_ID = os.getenv("VERCEL_TEAM_ID", "")

READ_FN = re.compile(r"^(list|get|fetch|search|count|check|is_|has_|show|stat|locale|retrieve)", re.I)
DANGER_FN = re.compile(r"(delete|remove|trash|terminate|destroy|kill|uninstall|revoke|unlink|empty)", re.I)


# ----------------------------------------------------------------- cPanel
def _cp_ready() -> dict | None:
    if not (CPANEL_HOST and CPANEL_USER and (CPANEL_API_TOKEN or CPANEL_PASSWORD)):
        return {"error": "Set CPANEL_HOST, CPANEL_USER and CPANEL_API_TOKEN (or CPANEL_PASSWORD) on the server."}
    return None


async def _uapi(module: str, function: str, params: dict | None = None,
                files: dict | None = None) -> dict:
    url = f"https://{CPANEL_HOST}:{CPANEL_PORT}/execute/{module}/{function}"
    headers, auth = {}, None
    if CPANEL_API_TOKEN:
        headers["Authorization"] = f"cpanel {CPANEL_USER}:{CPANEL_API_TOKEN}"
    else:
        auth = (CPANEL_USER, CPANEL_PASSWORD)
    async with httpx.AsyncClient(timeout=60, verify=CPANEL_VERIFY_SSL) as c:
        if files:
            r = await c.post(url, data=params or {}, files=files, headers=headers, auth=auth)
        else:
            r = await c.post(url, data=params or {}, headers=headers, auth=auth)
    try:
        d = r.json()
    except ValueError:
        return {"error": f"cPanel HTTP {r.status_code}", "text": r.text[:300]}
    return {"ok": bool(d.get("status")), "data": d.get("data"), "errors": d.get("errors"),
            "messages": d.get("messages")}


def _guard(function: str, confirm: bool) -> dict | None:
    if DANGER_FN.search(function) and not confirm:
        return {"needs_confirmation": True,
                "message": f"'{function}' deletes or removes something. Ask the user, then retry with confirm=true."}
    return None


# ----------------------------------------------------------------- Vercel
async def _vc(method: str, path: str, json: Any = None, params: dict | None = None) -> dict:
    p = dict(params or {})
    if VERCEL_TEAM_ID:
        p["teamId"] = VERCEL_TEAM_ID
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(method, f"https://api.vercel.com{path}", json=json, params=p,
                            headers={"Authorization": f"Bearer {VERCEL_TOKEN}"})
    try:
        d = r.json()
    except ValueError:
        d = {"text": r.text[:300]}
    return {"error": f"Vercel {r.status_code}", "details": d} if r.status_code >= 400 else d


def register(mcp) -> None:
    # ---------------- cPanel helpers
    @mcp.tool()
    async def cpanel_overview() -> dict:
        """cPanel account summary: domains, disk usage and SSL certificates."""
        if e := _cp_ready():
            return e
        return {"domains": await _uapi("DomainInfo", "list_domains"),
                "disk": await _uapi("Quota", "get_quota_info"),
                "ssl": await _uapi("SSL", "list_certs")}

    @mcp.tool()
    async def cpanel_files(action: str, path: str = "public_html", content: str = "",
                           confirm: bool = False) -> dict:
        """Manage files. action: list (folder) | read (file) | write (create/overwrite file with content)
        | delete (file, needs confirm). Paths are relative to the home folder, e.g. public_html/index.html."""
        if e := _cp_ready():
            return e
        folder, _, name = path.rstrip("/").rpartition("/")
        if action == "list":
            return await _uapi("Fileman", "list_files", {"dir": path, "include_mime": 0})
        if action == "read":
            return await _uapi("Fileman", "get_file_content", {"dir": folder or ".", "file": name})
        if action == "write":
            return await _uapi("Fileman", "save_file_content",
                               {"dir": folder or ".", "file": name, "content": content})
        if action == "delete":
            if not confirm:
                return {"needs_confirmation": True, "message": f"Delete {path}? Ask the user, then confirm=true."}
            return await _uapi("Fileman", "trash", {"path": path})
        return {"error": "action must be list, read, write or delete"}

    @mcp.tool()
    async def cpanel_upload_from_url(file_url: str, dest_dir: str = "public_html",
                                     filename: str = "") -> dict:
        """Download a file (zip, image, html...) from a URL and upload it to a cPanel folder."""
        if e := _cp_ready():
            return e
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(file_url)
            r.raise_for_status()
        name = filename or file_url.rstrip("/").split("/")[-1].split("?")[0] or "upload.bin"
        return await _uapi("Fileman", "upload_files", {"dir": dest_dir, "overwrite": 1},
                           files={"file-1": (name, r.content)})

    @mcp.tool()
    async def cpanel_git_deploy(repo_url: str, repo_dir: str, branch: str = "main",
                                deploy_now: bool = True) -> dict:
        """Deploy from Git: clone a repo into cPanel 'Git Version Control' (or pull if it exists) and
        run its deployment (.cpanel.yml in the repo decides where files go)."""
        if e := _cp_ready():
            return e
        existing = await _uapi("VersionControl", "retrieve")
        repos = existing.get("data") or []
        match = next((x for x in repos if x.get("repository_root", "").rstrip("/").endswith(repo_dir.strip("/"))), None)
        if match:
            res = await _uapi("VersionControl", "update", {"repository_root": match["repository_root"],
                                                           "branch": branch})
            root = match["repository_root"]
        else:
            root = repo_dir if repo_dir.startswith("/") else f"/home/{CPANEL_USER}/{repo_dir.strip('/')}"
            res = await _uapi("VersionControl", "create", {"type": "git", "name": repo_dir.strip("/").split("/")[-1],
                                                           "repository_root": root, "source_repository":
                                                           f'{{"remote_name":"origin","url":"{repo_url}"}}'})
        out = {"repo": res}
        if deploy_now:
            out["deploy"] = await _uapi("VersionControlDeployment", "create", {"repository_root": root})
        return out

    @mcp.tool()
    async def cpanel_database(action: str, name: str = "", user: str = "", password: str = "",
                              confirm: bool = False) -> dict:
        """MySQL: list | create_db | create_user | grant (all privileges of user on db) | delete_db (confirm).
        Names usually need the cPanel prefix, e.g. myuser_shop."""
        if e := _cp_ready():
            return e
        if action == "list":
            return {"databases": await _uapi("Mysql", "list_databases"), "users": await _uapi("Mysql", "list_users")}
        if action == "create_db":
            return await _uapi("Mysql", "create_database", {"name": name})
        if action == "create_user":
            return await _uapi("Mysql", "create_user", {"name": user, "password": password})
        if action == "grant":
            return await _uapi("Mysql", "set_privileges_on_database",
                               {"user": user, "database": name, "privileges": "ALL PRIVILEGES"})
        if action == "delete_db":
            if not confirm:
                return {"needs_confirmation": True, "message": f"Delete database {name} and all its data? Ask first."}
            return await _uapi("Mysql", "delete_database", {"name": name})
        return {"error": "action must be list, create_db, create_user, grant or delete_db"}

    @mcp.tool()
    async def cpanel_domains(action: str = "list", domain: str = "", target_dir: str = "",
                             confirm: bool = False) -> dict:
        """Domains: list | add_subdomain (domain='blog.example.com', target_dir='public_html/blog')
        | run_autossl (free SSL for all domains) | delete_subdomain (confirm)."""
        if e := _cp_ready():
            return e
        if action == "list":
            return await _uapi("DomainInfo", "domains_data")
        if action == "add_subdomain":
            sub, _, root = domain.partition(".")
            return await _uapi("SubDomain", "addsubdomain",
                               {"domain": sub, "rootdomain": root, "dir": target_dir or f"public_html/{sub}"})
        if action == "run_autossl":
            return await _uapi("SSL", "start_autossl_check")
        if action == "delete_subdomain":
            if not confirm:
                return {"needs_confirmation": True, "message": f"Delete subdomain {domain}? Ask first."}
            return await _uapi("SubDomain", "delsubdomain", {"domain": domain})
        return {"error": "action must be list, add_subdomain, run_autossl or delete_subdomain"}

    @mcp.tool()
    async def cpanel_uapi(module: str, function: str, params: dict | None = None,
                          confirm: bool = False) -> dict:
        """Call ANY cPanel UAPI function (docs: https://api.docs.cpanel.net/cpanel/introduction/),
        e.g. Email/add_pop, Cron/... , SSH/... , Ftp/add_ftp, Backup/fullbackup_to_homedir.
        Reads (list_/get_...) run directly; any change needs confirm=true after the user agrees."""
        if e := _cp_ready():
            return e
        if not READ_FN.search(function) and not confirm:
            return {"needs_confirmation": True,
                    "message": f"{module}/{function} changes the server. Ask the user, then retry with confirm=true."}
        return await _uapi(module, function, params)

    # ---------------- Vercel
    @mcp.tool()
    async def vercel_manage(action: str, project: str = "", key: str = "", value: str = "",
                            confirm: bool = False) -> dict:
        """Vercel (needs VERCEL_TOKEN): list_projects | list_deployments (project) | set_env (project, key,
        value) | redeploy (project: redeploys the latest production deployment) | delete_project (confirm)."""
        if not VERCEL_TOKEN:
            return {"error": "Set VERCEL_TOKEN on the server (https://vercel.com/account/tokens)."}
        if action == "list_projects":
            d = await _vc("GET", "/v9/projects", params={"limit": 50})
            return {"projects": [{"name": p["name"], "id": p["id"]} for p in d.get("projects", [])]} if "projects" in d else d
        if action == "list_deployments":
            d = await _vc("GET", "/v6/deployments", params={"projectId": project, "limit": 10})
            return {"deployments": [{"url": x.get("url"), "state": x.get("state"), "target": x.get("target"),
                                     "created": x.get("created")} for x in d.get("deployments", [])]} if "deployments" in d else d
        if action == "set_env":
            return await _vc("POST", f"/v10/projects/{project}/env", params={"upsert": "true"},
                             json={"key": key, "value": value, "type": "encrypted",
                                   "target": ["production", "preview"]})
        if action == "redeploy":
            d = await _vc("GET", "/v6/deployments", params={"projectId": project, "limit": 1, "target": "production"})
            deps = d.get("deployments") or []
            if not deps:
                return {"error": "No production deployment found", "details": d}
            return await _vc("POST", "/v13/deployments", params={"forceNew": "1"},
                             json={"name": project, "deploymentId": deps[0]["uid"], "target": "production"})
        if action == "delete_project":
            if not confirm:
                return {"needs_confirmation": True, "message": f"Delete Vercel project {project}? Ask first."}
            return await _vc("DELETE", f"/v9/projects/{project}")
        return {"error": "unknown action"}

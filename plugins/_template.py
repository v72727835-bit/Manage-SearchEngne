"""Template: copy this file to plugins/my_feature.py (no leading underscore),
edit it, push to GitHub. Vercel redeploys and the new tools appear by themselves.

Rules that keep the server fast:
- Use async + httpx with a short timeout (<= 10 s).
- Return small dicts: only the fields the model needs.
- Anything that sends / posts / deletes / pays must require confirm=true.
"""
import httpx


def register(mcp) -> None:
    @mcp.tool()
    async def hello_plugin(name: str = "world") -> dict:
        """Example tool. Replace me."""
        return {"hello": name}


# Optional: extra lead sources for find_freelance_leads.
async def src_example(client: httpx.AsyncClient, keywords: list[str]) -> list[dict]:
    # import server; return [server._lead("example", "Title", "https://...")]
    return []


# LEAD_SOURCES = {"example": src_example}

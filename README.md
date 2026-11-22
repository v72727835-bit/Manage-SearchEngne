# Freelance Lead Finder MCP (Python)

Do hisse hain:

| File | Kahan chalta hai | Kya karta hai |
|---|---|---|
| `server.py` | **Cloud** (Render / Railway / koi bhi) | 7 jagah se freelance leads dhoondta hai, bina database ke |
| `browser_mcp.py` | **Aapke computer par** | Claude ko aapka browser chalane deta hai: kholna, padhna, click, type, photo upload, screenshot |

Cloud server aapki screen nahi dekh sakta, isliye browser wala hissa local hai.

---

## 1. Cloud lead server (`server.py`)

**Sources (sab official API / RSS):** Remotive, RemoteOK, We Work Remotely, Jobicy, Himalayas,
Hacker News ("Freelancer? Seeking freelancer?" + "Who is hiring?"), Reddit (r/forhire, r/hiring,
r/freelance_forhire, r/jobbit, r/HireAWriter, r/DesignJobs, sirf `[Hiring]` posts).
Optional: Brave web search (puri web), apni key se.

**Tools**
- `find_freelance_leads(keywords, sources?, freelance_only?, posted_within_days?, max_results?)` – sab jagah ek saath search, score + date se sort, duplicate hatata hai
- `get_lead_details(url)` – ek lead ka pura text (robots.txt follow karta hai)
- `web_search_leads(query)` – open web search (BRAVE_API_KEY chahiye)
- `get_my_profile()` – aapki skills / rate (MY_PROFILE env se), pitch personalise karne ke liye
- `list_sources()`
- Prompts: `daily_lead_hunt`, `draft_outreach`

Koi DB nahi. 10 minute ka memory cache hai bas.

### Local chalana
```bash
pip install -r requirements.txt
python server.py          # http://localhost:8000/mcp
python -m pytest -q tests # offline tests
```

### Render par free deploy (sabse aasaan)
1. Ye folder ek GitHub repo me daalo.
2. https://render.com → New → **Blueprint** → repo chuno (`render.yaml` apne aap padh lega).
3. Environment me `MCP_PATH` ko apna secret bana do, jaise `/mcp-x7k2p9q` (ye password jaisa hai, kisi ko mat batana).
   `MY_PROFILE` me apni skills, experience, portfolio link, rate likho.
4. Deploy ke baad URL milega: `https://freelance-lead-finder-mcp.onrender.com`
   Aapka MCP URL = `https://freelance-lead-finder-mcp.onrender.com/mcp-x7k2p9q`

Railway: New Project → GitHub repo → wahi env vars → Start command `python server.py`.
Docker bhi hai (`Dockerfile`).

> Render free plan 15 min idle ke baad so jata hai, pehli call me ~30 sec lag sakte hain.

### Claude se connect karna
claude.ai → **Settings → Connectors → Add custom connector** → naam "Lead Finder" → URL upar wala → Add.
Phir chat me bolo: *"Python aur Django ke freelance leads dhoondo, pichhle 7 din ke, top 10 do aur 3 ke liye pitch likho."*

---

## 2. Browser agent (`browser_mcp.py`) – aapke PC par

Claude kisi bhi website par kaam kar sake: profile theek karna, photo badalna, research, usi site par client dhoondna.

**Tools:** `browser_open`, `browser_snapshot` (page ka text + numbered buttons/fields),
`browser_click`, `browser_type`, `browser_select`, `browser_upload` (photo), `browser_screenshot`,
`browser_scroll`, `browser_press_key`, `browser_back`, `browser_tabs`, `browser_find_text`, `browser_wait`.
Prompts: `improve_profile`, `find_clients_on_site`.

**Safety (jaan-boojh kar):**
- Send / Submit / Post / Save / Apply / Delete / Pay jaise buttons `confirm=true` ke bina click nahi honge – Claude pehle aapse poochega.
- Password field me kabhi type nahi karega. Login aap khud karo.
- Photo sirf `UPLOAD_ROOT` folder se upload hogi.

### Setup (Windows)
```bat
pip install -r requirements-browser.txt
python -m playwright install chromium
```
**Option A – apna Chrome (aapke logins ke saath):**
```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-claude"
```
Is window me ek baar Upwork/Fiverr/LinkedIn login kar lo (naya Chrome me alag profile folder zaroori hai). Config me `CHROME_CDP_URL=http://127.0.0.1:9222`.

**Option B – alag browser:** `CHROME_CDP_URL` khali chhodo, Chromium window khulegi, usme ek baar login karo, yaad rahega.

### Claude Desktop me jodna
Claude Desktop → Settings → Developer → **Edit Config** → `claude_desktop_config.example.json` jaisa likho (path apna) → Claude Desktop restart.

Phir bolo: *"upwork.com par meri profile kholo, padho aur better headline + overview suggest karo"*.

> Note: Claude ka apna **Claude in Chrome** extension aur Desktop app ka **computer use** bhi yahi kaam karte hain. Ye MCP aapka khud ka, Python wala version hai jise aap badal sakte ho.

---

## 3. GitHub, cPanel, Vercel control (`github_tools.py`, `hosting_tools.py`)
Ye `server.py` ke andar hi jude hain (kul 24 tools). Server ki env settings me ye daalo:

| Kya | Env var | Kahan se milega |
|---|---|---|
| GitHub | `GITHUB_TOKEN` | https://github.com/settings/personal-access-tokens (fine-grained, apne repos par Contents/Administration/Issues/Pull requests: Read & write) |
| cPanel | `CPANEL_HOST`, `CPANEL_USER`, `CPANEL_API_TOKEN` | cPanel → Security → Manage API Tokens (password ki jagah token behtar hai, kabhi bhi revoke ho sakta hai) |
| Vercel | `VERCEL_TOKEN` | https://vercel.com/account/tokens |

- GitHub: repo banana, private/public, naam badalna, files, branch, PR, issues, koi bhi API.
- cPanel: files padhna/likhna/upload, Git se deploy, MySQL database/user, subdomain, free SSL (AutoSSL), aur koi bhi UAPI function (email, FTP, cron, backup...).
- Vercel: projects, deployments, env vars, redeploy.
- **Delete / public karna / server badalne wale raw call → hamesha `confirm=true`, yaani Claude pehle poochega.**
- Token kabhi chat me mat bhejo, sirf server ki secret env settings me daalo.

## 4. Browser agent cloud me
`Dockerfile.browser` se Render par chalta hai (`render.yaml` me dusri service). `TRANSPORT=http` aur ek secret `MCP_PATH` zaroori hai.
- Login: apne browser me "Cookie-Editor" extension se site ki cookies Export (JSON) karo, aur Claude ko bolo "ye cookies import karo". `browser_clear_cookies` se logout.
- Photo: koi bhi public image link do, `browser_upload_from_url` upload kar dega.
- Limit: Upwork/LinkedIn jaisi kuch sites cloud browser par captcha ya block dikha sakti hain.
- Chromium ko ~1 GB RAM chahiye, isliye Render "starter" plan (free nahi).

---

## Niyam (account ban se bachne ke liye)
- Spam / mass messaging mat karo. Har pitch padh kar khud bhejo.
- Upwork/Fiverr par platform ke bahar contact maangna mana hai.
- Har site ke Terms follow karo. Ye tool sirf public API/RSS use karta hai.

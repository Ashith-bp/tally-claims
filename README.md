# Tally Claims

Expense claims that Claude files for you. **There is no AI inside this app**: no AI keys, no AI libraries, no AI calls. The customer's own Claude plan reads the receipt; Tally Claims validates, checks policy, stores, lets a person edit and approve, and exports CSV.

```
You + Claude (your plan)  --MCP-->  Tally Claims (this app)  -->  review, edit, approve, CSV
   reads the receipt               validates + policy checks       keeps working after the chat ends
```

## What it does

- **Claude tools (MCP):** `create_claim`, `list_claims`, `get_claim`, `update_claim`, `get_policy`, `expense_summary`.
- **Automatic policy checks (plain code, no AI):** per-category limits, possible duplicates, future dates, claims older than 60 days, weekend spend, alcohol mentions, large claims without a business purpose, foreign currency.
- **Web app:** dashboard with filters and search, claim page where anything can be edited, approve / reject / reopen with a note, full history of who changed what, manual add, CSV export, editable policy, private connector link you can revoke.
- **Safe by design:** passwords hashed, CSRF protection, per-user data isolation, parameterized SQL, escaped HTML, CSV formula-injection protection, Claude can only edit claims that are still pending.

## Reviewer quick start

| | |
|---|---|
| Live app | `<PASTE YOUR LIVE URL>` |
| Test login | `demo@tallyclaims.app` / `TallyDemo2026!` |
| MCP link | Log in, open **Connect & policy**, copy the link. It looks like `https://<your-app>/mcp/tc_...` |

### Connect it to Claude (about 1 minute)

1. Log in to the live app and open **Connect & policy**. Copy the connector link.
2. In Claude go to **Customize -> Connectors -> + Add -> Add custom connector**.
3. Name: `Tally Claims`. URL: paste the link. Authentication: **No sign in**. Save.
4. Start a new chat, enable the connector, attach `demo/receipt_1_coffee_meeting.png`, and send:
   > File this receipt as an expense claim for Priya. It was a coffee meeting with a client.
5. Refresh the app. The claim appears (marked "Claude"). Click it, change anything, approve it, export the CSV.

For a claim with flags, use `demo/receipt_2_client_dinner_flagged.png` (over the Client Entertainment limit, mentions alcohol, weekend/no-purpose checks may also fire).

Claude's custom connectors are available on all plans; the Free plan allows one custom connector.

## Run locally

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

Local test connector link: log in as the demo user, open **Connect & policy**. Claude needs a public HTTPS URL, so for local testing use a tunnel (for example `cloudflared tunnel --url http://localhost:8000`) and set `PUBLIC_URL` to the tunnel address.

Run the tests:

```bash
pip install pytest
python -m pytest tests -q        # 37 tests
```

## Deploy (Render, about 10 minutes)

1. Push this folder to a new GitHub repository.
2. On [render.com](https://render.com): **New -> Blueprint**, select the repository. `render.yaml` sets everything up (Docker, health check, secrets, secure cookies, demo login hint).
3. When the deploy is live, open the URL, log in with the demo account, and copy the link from **Connect & policy**.

The demo connector link is stable across redeploys because it is derived from the `DEMO_TOKEN` environment variable that Render generates once.

Other hosts work too: it is a standard Docker app. Set `PORT`, `SECRET_KEY`, `COOKIE_SECURE=1`, optionally `DATA_DIR` (where the SQLite file lives).

### Important: data persistence

Data is stored in SQLite under `DATA_DIR`. On Render's **free** plan the disk is wiped on every redeploy and the service sleeps after about 15 minutes idle (the first request then takes up to a minute). For the review window either:

- upgrade the service to a paid plan and attach a disk mounted at `/data`, or
- keep the free plan, open the app yourself right before you submit and a few times while reviewers are looking, and expect claims to reset if it redeploys.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `SECRET_KEY` | Session signing key | generated into `DATA_DIR` |
| `DATA_DIR` | Folder for the SQLite file | `./data` |
| `COOKIE_SECURE` | `1` when served over HTTPS | `0` |
| `DEMO_TOKEN` | Seed for the demo account's stable connector link | random |
| `DEMO_EMAIL` / `DEMO_PASSWORD` | Demo login | `demo@tallyclaims.app` / `TallyDemo2026!` |
| `SHOW_DEMO_HINT` | `1` shows the demo login on the login page | `0` |
| `PUBLIC_URL` | Overrides the host shown in the connector link | detected from request |

## How the MCP endpoint works

`POST /mcp/<private-token>` speaks JSON-RPC 2.0 over the MCP Streamable HTTP transport (stateless, JSON responses). The token in the URL identifies the account, so no OAuth is needed ("No sign in" in Claude). Regenerate it any time from **Connect & policy**; the old link stops working immediately. Verified against the official MCP Python client.

## Repository layout

```
app.py            Flask app: database, policy checks, MCP server, web UI routes
templates/        HTML pages
static/style.css  styling
tests/            automated tests
demo/             two sample receipts for the demo
Dockerfile, render.yaml, requirements.txt
PITCH.md          5-minute pitch script
```

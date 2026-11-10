import json
import os
import re
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_TOKEN", "seed123")
    sys.modules.pop("app", None)
    import app
    return app


@pytest.fixture
def client(mod):
    return mod.app.test_client()


def demo_token(mod):
    import hashlib
    return "tc_" + hashlib.sha256(b"seed123").hexdigest()[:40]


def rpc(client, token, method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/mcp/{token}", json=body)


def call(client, token, name, args):
    r = rpc(client, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.data
    return r.get_json()["result"]


def good(**kw):
    base = {"merchant": "Blue Bottle Coffee", "amount": 23.5, "date": date.today().isoformat(),
            "category": "Meals", "employee": "Priya"}
    base.update(kw)
    return base


def web_login(client, email="demo@tallyclaims.app", password="TallyDemo2026!"):
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    r = client.post("/login", data={"email": email, "password": password, "csrf": "tok"})
    return r


def post(client, url, **data):
    # login clears the session (fixation defence), so pin a known CSRF token for the test client
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    data["csrf"] = "tok"
    return client.post(url, data=data)


# ------------------------------------------------------------------ MCP protocol

def test_health(client):
    assert client.get("/healthz").get_json() == {"ok": True}


def test_initialize_and_tools(client, mod):
    tok = demo_token(mod)
    r = rpc(client, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "1"}})
    res = r.get_json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "tally-claims"
    assert "tools" in res["capabilities"]
    r = rpc(client, tok, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.get_json()["result"]["protocolVersion"] == mod.PROTOCOLS[0]
    tools = rpc(client, tok, "tools/list").get_json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["create_claim", "list_claims", "get_claim", "update_claim", "get_policy", "expense_summary"]
    create = tools[0]
    assert "Meals" in create["inputSchema"]["properties"]["category"]["enum"]
    assert create["inputSchema"]["required"] == ["merchant", "amount", "date", "category"]


def test_notification_and_misc(client, mod):
    tok = demo_token(mod)
    r = client.post(f"/mcp/{tok}", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.data == b""
    assert rpc(client, tok, "ping").get_json()["result"] == {}
    assert rpc(client, tok, "nope/nothing").get_json()["error"]["code"] == -32601
    assert rpc(client, tok, "tools/call", {"name": "ghost", "arguments": {}}).get_json()["error"]["code"] == -32602
    r = client.post(f"/mcp/{tok}", data="{not json", content_type="application/json")
    assert r.status_code == 400 and r.get_json()["error"]["code"] == -32700
    r = client.post(f"/mcp/{tok}", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                          {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert len(r.get_json()) == 1


def test_token_and_methods(client, mod):
    assert client.post("/mcp/not-a-token", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 404
    assert client.get(f"/mcp/{demo_token(mod)}").status_code == 405
    r = client.open(f"/mcp/{demo_token(mod)}", method="OPTIONS")
    assert r.status_code == 204 and r.headers["Access-Control-Allow-Origin"] == "*"


# ------------------------------------------------------------------ tools

def test_create_and_flags(client, mod):
    tok = demo_token(mod)
    res = call(client, tok, "create_claim", good())
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert "TC-1" in text and "http" in text
    payload = json.loads(text.splitlines()[-1])
    assert payload["amount"] == 23.5 and payload["status"] == "pending" and payload["filed_via"] == "Claude"
    # duplicate
    res = call(client, tok, "create_claim", good())
    assert "duplicate of TC-1" in res["content"][0]["text"]
    # over limit
    res = call(client, tok, "create_claim", good(amount=75, merchant="Fancy Place"))
    assert "limit" in res["content"][0]["text"]
    # foreign currency => limit not checked
    res = call(client, tok, "create_claim", good(amount=5000, currency="inr", merchant="Chai Stop"))
    t = res["content"][0]["text"]
    assert "limit was not checked" in t and "Over the" not in t
    # alcohol + future + no purpose
    future = (date.today() + timedelta(days=10)).isoformat()
    res = call(client, tok, "create_claim", good(amount=150, category="Client Entertainment",
                                                 date=future, receipt_notes="2x wine, 1x pasta", merchant="Trattoria"))
    t = res["content"][0]["text"]
    assert "future" in t and "alcohol" in t and "business purpose" in t
    # stale
    old = (date.today() - timedelta(days=90)).isoformat()
    res = call(client, tok, "create_claim", good(date=old, merchant="Old Cafe"))
    assert "older than 60 days" in res["content"][0]["text"]


@pytest.mark.parametrize("bad,needle", [
    ({"merchant": ""}, "merchant"),
    ({"amount": -5}, "greater than zero"),
    ({"amount": 0}, "greater than zero"),
    ({"amount": "abc"}, "number"),
    ({"amount": "nan"}, "number"),
    ({"amount": "inf"}, "number"),
    ({"amount": True}, "number"),
    ({"amount": 99999999}, "too large"),
    ({"date": "11/10/2026"}, "YYYY-MM-DD"),
    ({"date": "2026-02-31"}, "real calendar date"),
    ({"date": "20261110"}, "YYYY-MM-DD"),
    ({"category": "Yachts"}, "one of"),
    ({"currency": "DOLLARS"}, "3-letter"),
    ({"tax_amount": 999}, "cannot be more"),
    ({"merchant": "x" * 500}, "too long"),
    ({"bogus": 1}, "Unknown field"),
])
def test_create_validation(client, mod, bad, needle):
    tok = demo_token(mod)
    res = call(client, tok, "create_claim", good(**bad))
    assert res["isError"] is True
    assert needle in res["content"][0]["text"]
    assert "No claims found" in call(client, tok, "list_claims", {})["content"][0]["text"]


def test_missing_required(client, mod):
    res = call(client, demo_token(mod), "create_claim", {"merchant": "X"})
    assert res["isError"] and "'amount' is required" in res["content"][0]["text"]


def test_category_case_insensitive_and_amount_strings(client, mod):
    tok = demo_token(mod)
    res = call(client, tok, "create_claim", good(category="meals", amount="$1,234.567"))
    assert res["isError"] is False
    c = json.loads(res["content"][0]["text"].splitlines()[-1])
    assert c["category"] == "Meals" and c["amount"] == 1234.57


def test_list_get_update(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good())
    call(client, tok, "create_claim", good(merchant="Uber", category="Transport", amount=18))
    lst = call(client, tok, "list_claims", {"limit": 1})["content"][0]["text"]
    assert lst.startswith("1 claim(s)") and "Uber" in lst
    assert call(client, tok, "list_claims", {"status": "approved"})["content"][0]["text"] == "No claims found."
    assert call(client, tok, "list_claims", {"status": "weird"})["isError"]
    assert call(client, tok, "list_claims", {"limit": 0})["isError"]
    got = call(client, tok, "get_claim", {"number": "TC-1"})
    assert json.loads(got["content"][0]["text"])["merchant"] == "Blue Bottle Coffee"
    assert call(client, tok, "get_claim", {"number": 99})["isError"]
    assert call(client, tok, "get_claim", {})["isError"]
    up = call(client, tok, "update_claim", {"number": 1, "amount": 90, "description": "Team lunch"})
    assert up["isError"] is False and "Updated TC-1" in up["content"][0]["text"] and "limit" in up["content"][0]["text"]
    up = call(client, tok, "update_claim", {"number": 1, "amount": 20})
    assert "limit" not in up["content"][0]["text"].split("\n")[1]
    assert call(client, tok, "update_claim", {"number": 1})["isError"]
    assert call(client, tok, "update_claim", {"number": 1, "date": "bad"})["isError"]
    assert call(client, tok, "update_claim", {"number": 1, "status": "approved"})["isError"]
    same = call(client, tok, "update_claim", {"number": 1, "amount": 20})
    assert "already had" in same["content"][0]["text"]


def test_update_does_not_flag_itself_as_duplicate(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good())
    up = call(client, tok, "update_claim", {"number": 1, "description": "Coffee with client"})
    assert "duplicate" not in up["content"][0]["text"]


def test_policy_and_summary(client, mod):
    tok = demo_token(mod)
    assert "Meals: 60.00" in call(client, tok, "get_policy", {})["content"][0]["text"]
    assert call(client, tok, "expense_summary", {})["content"][0]["text"] == "No claims for that period."
    call(client, tok, "create_claim", good(amount=10))
    call(client, tok, "create_claim", good(amount=20, merchant="B"))
    s = json.loads(call(client, tok, "expense_summary", {})["content"][0]["text"])
    assert s["by_category_excluding_rejected"]["Meals (USD)"]["total"] == 30
    assert call(client, tok, "expense_summary", {"month": "2026-13"})["isError"]
    month = date.today().strftime("%Y-%m")
    assert not call(client, tok, "expense_summary", {"month": month})["isError"]


# ------------------------------------------------------------------ web UI

def test_auth_required_and_csrf(client):
    assert client.get("/").status_code == 302
    assert "/login" in client.get("/").headers["Location"]
    assert client.get("/export.csv").status_code == 302
    assert client.post("/login", data={"email": "a", "password": "b"}).status_code == 400
    r = web_login(client, password="wrong")
    assert r.status_code == 200 and b"Wrong email or password" in r.data
    r = web_login(client)
    assert r.status_code == 302
    assert client.get("/").status_code == 200
    r = client.post("/logout", data={})
    assert r.status_code == 400
    assert post(client, "/logout").status_code == 302


def test_open_redirect_blocked(client):
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    r = client.post("/login?next=//evil.com", data={"email": "demo@tallyclaims.app",
                                                    "password": "TallyDemo2026!", "csrf": "tok"})
    assert r.headers["Location"] == "/"


def test_signup(client):
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    r = client.post("/signup", data={"email": "bad", "password": "longenough", "csrf": "tok"})
    assert b"valid email" in r.data
    r = client.post("/signup", data={"email": "a@b.co", "password": "short", "csrf": "tok"})
    assert b"at least 8" in r.data
    r = client.post("/signup", data={"email": "New@Team.io", "password": "longenough", "csrf": "tok"})
    assert r.status_code == 302 and "/settings" in r.headers["Location"]
    page = client.get("/settings").get_data(as_text=True)
    assert "/mcp/tc_" in page
    again = client.application.test_client()
    with again.session_transaction() as s:
        s["csrf"] = "tok"
    r = again.post("/signup", data={"email": "new@team.io", "password": "longenough", "csrf": "tok"})
    assert r.status_code == 200 and b"already registered" in r.data


def test_ui_edit_flow(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good(merchant="Cafe <script>alert(1)</script>"))
    web_login(client)
    dash = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in dash and "&lt;script&gt;" in dash
    assert "TC-1" in dash and "Claude" in dash
    page = client.get("/claims/1")
    assert page.status_code == 200 and b"&lt;script&gt;" in page.data and b"<script>alert(1)" not in page.data
    form = {"merchant": "Cafe Edited", "employee": "Priya", "amount": "70.00", "currency": "USD", "tax_amount": "",
            "date": date.today().isoformat(), "category": "Meals", "payment_method": "Visa",
            "description": "Team lunch", "receipt_notes": ""}
    r = post(client, "/claims/1/edit", **form)
    assert r.status_code == 302
    page = client.get("/claims/1").get_data(as_text=True)
    assert "Cafe Edited" in page and "Over the Meals limit" in page and "edited" in page
    bad = dict(form, amount="-3")
    r = post(client, "/claims/1/edit", **bad)
    assert r.status_code == 400 and b"greater than zero" in r.data
    bad = dict(form, date="nonsense")
    assert post(client, "/claims/1/edit", **bad).status_code == 400
    assert post(client, "/claims/1/edit", **dict(form, category="Nope")).status_code == 400
    # claim from Claude reflects edit
    got = json.loads(call(client, tok, "get_claim", {"number": 1})["content"][0]["text"])
    assert got["merchant"] == "Cafe Edited" and got["amount"] == 70


def test_approve_reject_reopen_and_lock(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good())
    web_login(client)
    assert post(client, "/claims/1/status", action="approve", note="ok").status_code == 302
    assert b"approved" in client.get("/claims/1").data
    res = call(client, tok, "update_claim", {"number": 1, "amount": 5})
    assert res["isError"] and "reopen" in res["content"][0]["text"]
    post(client, "/claims/1/status", action="reopen")
    assert not call(client, tok, "update_claim", {"number": 1, "amount": 5})["isError"]
    post(client, "/claims/1/status", action="reject", note="no receipt")
    assert b"rejected" in client.get("/claims/1").data
    assert post(client, "/claims/1/status", action="explode").status_code == 400
    assert post(client, "/claims/1/status", action="approve", back="//evil.com").headers["Location"].endswith("/claims/1")


def test_manual_add_and_delete(client, mod):
    web_login(client)
    form = {"merchant": "Staples", "employee": "Sam", "amount": "42", "currency": "usd", "tax_amount": "3.5",
            "date": date.today().isoformat(), "category": "Office Supplies", "payment_method": "", "description": "Pens",
            "receipt_notes": ""}
    r = post(client, "/claims/new", **form)
    assert r.status_code == 302
    assert b"Staples" in client.get("/").data
    assert post(client, "/claims/new", **dict(form, amount="")).status_code == 200
    assert post(client, "/claims/1/delete").status_code == 302
    assert client.get("/claims/1").status_code == 404


def test_filters_search_and_csv(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good(merchant="=HYPERLINK(\"http://x\")"))
    call(client, tok, "create_claim", good(merchant="Plain", amount=500))
    web_login(client)
    for q in ["%", "_", "'; DROP TABLE claims;--", "\\", "Plain"]:
        assert client.get("/", query_string={"q": q}).status_code == 200
    assert b"Plain" in client.get("/", query_string={"q": "Plain"}).data
    assert b"=HYPERLINK" not in client.get("/", query_string={"q": "Plain"}).data
    assert client.get("/", query_string={"status": "flagged"}).status_code == 200
    assert client.get("/", query_string={"status": "bogus"}).status_code == 200
    csv_text = client.get("/export.csv").get_data(as_text=True)
    assert csv_text.startswith("﻿Claim,Date")
    assert "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text
    assert "TC-2" in csv_text and "Claude (MCP)" in csv_text
    assert "attachment" in client.get("/export.csv").headers["Content-Disposition"]


def test_isolation_between_users(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good(merchant="Secret Co"))
    other = mod.app.test_client()
    with other.session_transaction() as s:
        s["csrf"] = "tok"
    other.post("/signup", data={"email": "x@y.zz", "password": "longenough", "csrf": "tok"})
    page = other.get("/settings").get_data(as_text=True)
    other_tok = re.search(r"/mcp/(tc_[A-Za-z0-9_-]+)", page).group(1)
    assert other_tok != tok
    assert other.get("/claims/1").status_code == 404
    assert post(other, "/claims/1/delete").status_code == 404
    assert post(other, "/claims/1/status", action="approve").status_code == 404
    assert b"Secret Co" not in other.get("/").data and b"Secret Co" not in other.get("/export.csv").data
    assert call(other, other_tok, "get_claim", {"number": 1})["isError"]
    assert "No claims" in call(other, other_tok, "list_claims", {})["content"][0]["text"]
    res = call(other, other_tok, "create_claim", good())
    assert "TC-1" in res["content"][0]["text"]  # numbering is per user


def test_policy_settings_and_token_rotation(client, mod):
    tok = demo_token(mod)
    web_login(client)
    data = {"base_currency": "inr", "count": "3", "cat_name_0": "Meals", "cat_limit_0": "1000",
            "cat_name_1": "Gadgets", "cat_limit_1": "", "cat_name_2": "", "cat_limit_2": "5"}
    assert post(client, "/settings/policy", **data).status_code == 302
    tools = rpc(client, tok, "tools/list").get_json()["result"]["tools"]
    assert tools[0]["inputSchema"]["properties"]["category"]["enum"] == ["Meals", "Gadgets"]
    res = call(client, tok, "create_claim", good(amount=900, currency="INR"))
    assert "Over the" not in res["content"][0]["text"]
    for bad in [dict(data, base_currency="rupee"), dict(data, cat_limit_0="abc"),
                dict(data, cat_name_1="meals"), dict(data, cat_name_0="", cat_name_1="")]:
        r = post(client, "/settings/policy", **bad)
        assert r.status_code == 302
    tools = rpc(client, tok, "tools/list").get_json()["result"]["tools"]
    assert tools[0]["inputSchema"]["properties"]["category"]["enum"] == ["Meals", "Gadgets"]
    assert post(client, "/settings/token").status_code == 302
    assert client.post(f"/mcp/{tok}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 404


def test_edit_claim_with_removed_category_still_works(client, mod):
    tok = demo_token(mod)
    call(client, tok, "create_claim", good(category="Training", amount=100))
    web_login(client)
    post(client, "/settings/policy", base_currency="USD", count="1", cat_name_0="Meals", cat_limit_0="60")
    assert client.get("/claims/1").status_code == 200
    form = {"merchant": "Udemy", "employee": "", "amount": "100", "currency": "USD", "tax_amount": "",
            "date": date.today().isoformat(), "category": "Training", "payment_method": "", "description": "Course",
            "receipt_notes": ""}
    assert post(client, "/claims/1/edit", **form).status_code == 302


def test_no_ai_keys_in_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = re.compile(r"openai|anthropic|api[_-]?key|sk-[A-Za-z0-9]{10}", re.I)
    for folder, _dirs, files in os.walk(root):
        if any(p in folder for p in ("/tests", "/data", "__pycache__", "/.")):
            continue
        for name in files:
            if name.endswith((".py", ".html", ".txt", ".yaml", ".css", "Dockerfile")):
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    text = fh.read()
                assert not bad.search(text), f"AI reference found in {name}"

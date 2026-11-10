"""Tally Claims: expense claims that Claude files for you. There is no AI inside this app.

Claude (running on the customer's own plan) reads the receipt and calls the MCP tools
below. This app validates, runs deterministic policy checks, stores the claim, and lets a
person review, edit, approve and export it.
"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "tally.db")

DEFAULT_LIMITS = {
    "Meals": 60,
    "Travel": 500,
    "Lodging": 250,
    "Transport": 80,
    "Software": 200,
    "Office Supplies": 100,
    "Client Entertainment": 150,
    "Training": 500,
    "Other": 100,
}
STATUSES = ("pending", "approved", "rejected")
FIELD_KEYS = ["merchant", "amount", "currency", "date", "category", "description",
              "payment_method", "receipt_notes", "employee", "tax_amount"]
TEXT_LIMITS = {"merchant": 120, "description": 500, "payment_method": 40,
               "receipt_notes": 1000, "employee": 80}
MAX_AMOUNT = Decimal("1000000")
STALE_DAYS = 60
ALCOHOL_RE = re.compile(
    r"\b(beer|beers|wine|wines|liquor|whisk(?:e)?y|vodka|cocktail|cocktails|champagne|"
    r"alcohol|brewery|pub|sake|rum|tequila)\b", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@tallyclaims.app")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "TallyDemo2026!")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


def _load_secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret.key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _load_secret_key()

# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    limits_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    employee TEXT NOT NULL DEFAULT '',
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    tax_cents INTEGER,
    currency TEXT NOT NULL,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT '',
    receipt_notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_note TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, number)
);
CREATE INDEX IF NOT EXISTS idx_claims_user_status ON claims (user_id, status);
CREATE TABLE IF NOT EXISTS claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_token():
    return "tc_" + secrets.token_urlsafe(24)


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT id FROM users WHERE email=?", (DEMO_EMAIL,)).fetchone()
        if row is None:
            seed = os.environ.get("DEMO_TOKEN")
            token = ("tc_" + hashlib.sha256(seed.encode()).hexdigest()[:40]) if seed else new_token()
            conn.execute(
                "INSERT OR IGNORE INTO users (email, password_hash, token, base_currency, limits_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD), token, "USD",
                 json.dumps(DEFAULT_LIMITS), now_iso()))
        conn.commit()
    finally:
        conn.close()


init_db()

# --------------------------------------------------------------------------- validation


def parse_money(value, field, allow_zero=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f"'{field}' must be a number")
    text = str(value).strip().replace(",", "")
    text = text.lstrip("$€£₹")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"'{field}' must be a number like 23.50")
    if not amount.is_finite():
        raise ValueError(f"'{field}' must be a number like 23.50")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError(f"'{field}' must be greater than zero")
    if amount > MAX_AMOUNT:
        raise ValueError(f"'{field}' is too large")
    return int(amount * 100)


def user_limits(user):
    try:
        data = json.loads(user["limits_json"])
        if isinstance(data, dict) and data:
            return data
    except (TypeError, ValueError):
        pass
    return dict(DEFAULT_LIMITS)


def clean_fields(data, categories, required=()):
    """Validate claim fields. Returns (clean_columns, errors). Missing/None keys are skipped."""
    clean, errors = {}, []
    for key in required:
        val = data.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"'{key}' is required")
    for key in FIELD_KEYS:
        if key not in data or data[key] is None:
            continue
        val = data[key]
        blank_skip = ("amount", "date", "category", "currency", "tax_amount")
        if isinstance(val, str) and not val.strip() and key in blank_skip:
            continue
        try:
            if key in TEXT_LIMITS:
                if not isinstance(val, str):
                    raise ValueError(f"'{key}' must be text")
                val = val.replace("\x00", "").strip()
                if key == "merchant" and not val:
                    raise ValueError("'merchant' is required")
                if len(val) > TEXT_LIMITS[key]:
                    raise ValueError(f"'{key}' is too long (max {TEXT_LIMITS[key]} characters)")
                clean[key] = val
            elif key == "amount":
                clean["amount_cents"] = parse_money(val, "amount")
            elif key == "tax_amount":
                clean["tax_cents"] = parse_money(val, "tax_amount", allow_zero=True)
            elif key == "currency":
                if not isinstance(val, str) or not re.fullmatch(r"[A-Za-z]{3}", val.strip()):
                    raise ValueError("'currency' must be a 3-letter code like USD or INR")
                clean["currency"] = val.strip().upper()
            elif key == "date":
                if not isinstance(val, str) or not DATE_RE.match(val.strip()):
                    raise ValueError("'date' must be in YYYY-MM-DD format")
                try:
                    parsed = date.fromisoformat(val.strip())
                except ValueError:
                    raise ValueError("'date' is not a real calendar date")
                if parsed.year < 2000:
                    raise ValueError("'date' is too far in the past")
                clean["expense_date"] = parsed.isoformat()
            elif key == "category":
                if not isinstance(val, str):
                    raise ValueError("'category' must be text")
                match = next((c for c in categories if c.lower() == val.strip().lower()), None)
                if match is None:
                    raise ValueError("'category' must be one of: " + ", ".join(categories))
                clean["category"] = match
        except ValueError as exc:
            errors.append(str(exc))
    if "amount_cents" in clean and "tax_cents" in clean and clean["tax_cents"] > clean["amount_cents"]:
        errors.append("'tax_amount' cannot be more than 'amount'")
    return clean, errors


def evaluate_flags(db, user, c, exclude_number=None):
    """Deterministic policy checks. `c` holds claim columns. No AI involved."""
    flags = []
    base = user["base_currency"]
    limits = user_limits(user)
    amount = c["amount_cents"]
    cur = c["currency"]

    limit = limits.get(c["category"])
    if cur != base:
        flags.append({"code": "foreign_currency", "severity": "info",
                      "message": f"Paid in {cur}; policy limits are in {base}, so the category limit was not checked."})
    elif limit and amount > int(round(float(limit) * 100)):
        flags.append({"code": "over_limit", "severity": "high",
                      "message": f"Over the {c['category']} limit of {base} {float(limit):,.2f}."})

    dup = db.execute(
        "SELECT number FROM claims WHERE user_id=? AND lower(merchant)=lower(?) AND amount_cents=? "
        "AND currency=? AND expense_date=? AND status!='rejected' AND (? IS NULL OR number!=?) "
        "ORDER BY number LIMIT 1",
        (user["id"], c["merchant"], amount, cur, c["expense_date"], exclude_number, exclude_number)).fetchone()
    if dup:
        flags.append({"code": "duplicate", "severity": "high",
                      "message": f"Possible duplicate of TC-{dup['number']} (same merchant, amount and date)."})

    try:
        spent = date.fromisoformat(c["expense_date"])
    except ValueError:
        spent = None
    if spent:
        if spent > date.today() + timedelta(days=1):
            flags.append({"code": "future_date", "severity": "high",
                          "message": "Expense date is in the future."})
        elif (date.today() - spent).days > STALE_DAYS:
            flags.append({"code": "stale", "severity": "medium",
                          "message": f"Expense is older than {STALE_DAYS} days."})
        if spent.weekday() >= 5 and c["category"] not in ("Travel", "Lodging"):
            flags.append({"code": "weekend", "severity": "low",
                          "message": "Expense was on a weekend."})

    haystack = " ".join([c.get("merchant", ""), c.get("description", ""), c.get("receipt_notes", "")])
    if ALCOHOL_RE.search(haystack):
        flags.append({"code": "alcohol", "severity": "medium",
                      "message": "Receipt text mentions alcohol."})
    if amount >= 10000 and not (c.get("description") or "").strip():
        flags.append({"code": "no_description", "severity": "low",
                      "message": "Large claim with no business purpose given."})
    return flags


# --------------------------------------------------------------------------- data access


def claim_dict(row):
    d = dict(row)
    d["flags"] = json.loads(d.pop("flags_json") or "[]")
    return d


def get_claim_row(user_id, number):
    return get_db().execute("SELECT * FROM claims WHERE user_id=? AND number=?",
                            (user_id, number)).fetchone()


def add_event(db, claim_id, actor, action, detail=""):
    db.execute("INSERT INTO claim_events (claim_id, at, actor, action, detail) VALUES (?,?,?,?,?)",
               (claim_id, now_iso(), actor, action, detail))


def insert_claim(db, user, clean, source, actor):
    """Insert a validated claim and return its per-user number."""
    cols = {
        "employee": clean.get("employee", ""),
        "merchant": clean["merchant"],
        "amount_cents": clean["amount_cents"],
        "tax_cents": clean.get("tax_cents"),
        "currency": clean.get("currency", user["base_currency"]),
        "expense_date": clean["expense_date"],
        "category": clean["category"],
        "description": clean.get("description", ""),
        "payment_method": clean.get("payment_method", ""),
        "receipt_notes": clean.get("receipt_notes", ""),
    }
    flags = evaluate_flags(db, user, cols)
    stamp = now_iso()
    cur = db.execute(
        "INSERT INTO claims (user_id, number, employee, merchant, amount_cents, tax_cents, currency, "
        "expense_date, category, description, payment_method, receipt_notes, flags_json, source, "
        "created_at, updated_at) VALUES (?, (SELECT COALESCE(MAX(number),0)+1 FROM claims WHERE user_id=?), "
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], user["id"], cols["employee"], cols["merchant"], cols["amount_cents"], cols["tax_cents"],
         cols["currency"], cols["expense_date"], cols["category"], cols["description"],
         cols["payment_method"], cols["receipt_notes"], json.dumps(flags), source, stamp, stamp))
    claim_id = cur.lastrowid
    add_event(db, claim_id, actor, "created", "Claim filed")
    number = db.execute("SELECT number FROM claims WHERE id=?", (claim_id,)).fetchone()["number"]
    db.commit()
    return number


def update_claim_row(db, user, row, changes, actor):
    """Apply validated column changes to an existing claim row and recompute flags."""
    merged = dict(row)
    merged.update(changes)
    if merged["tax_cents"] is not None and merged["tax_cents"] > merged["amount_cents"]:
        raise ValueError("'tax_amount' cannot be more than 'amount'")
    flags = evaluate_flags(db, user, merged, exclude_number=row["number"])
    col_to_field = {"amount_cents": "amount", "tax_cents": "tax amount", "expense_date": "date"}
    changed = [col_to_field.get(k, k).replace("_", " ") for k, v in changes.items() if row[k] != v]
    sets = list(changes.keys()) + ["flags_json", "updated_at"]
    params = list(changes.values()) + [json.dumps(flags), now_iso(), row["id"]]
    db.execute("UPDATE claims SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", params)
    if changed:
        add_event(db, row["id"], actor, "edited", "Changed: " + ", ".join(sorted(changed)))
    db.commit()
    return changed


def fmt_money(cents, currency="USD"):
    if cents is None:
        return ""
    return f"{currency} {cents / 100:,.2f}"


def fmt_totals(totals):
    if not totals:
        return " "
    return " + ".join(fmt_money(cents, cur) for cur, cents in sorted(totals.items()))


app.jinja_env.filters["money"] = fmt_money
app.jinja_env.filters["totals"] = fmt_totals

# --------------------------------------------------------------------------- web auth


@app.before_request
def load_user_and_csrf():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None:
            session.clear()
    if request.method == "POST" and not request.path.startswith("/mcp/"):
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.context_processor
def inject_helpers():
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]
    return {"csrf_token": csrf_token,
            "show_demo_hint": os.environ.get("SHOW_DEMO_HINT", "0") == "1",
            "demo_email": DEMO_EMAIL, "demo_password": DEMO_PASSWORD}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("page_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("page_dashboard")


@app.get("/healthz")
def healthz():
    get_db().execute("SELECT 1").fetchone()
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def page_login():
    if g.user:
        return redirect(url_for("page_dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session.permanent = True
            session["uid"] = row["id"]
            return redirect(safe_next(request.args.get("next")))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def page_signup():
    if g.user:
        return redirect(url_for("page_dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}", email) or len(email) > 254:
            flash("Enter a valid email address.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, token, base_currency, limits_json, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (email, generate_password_hash(password), new_token(), "USD",
                     json.dumps(DEFAULT_LIMITS), now_iso()))
                db.commit()
            except sqlite3.IntegrityError:
                flash("That email is already registered. Try logging in.", "error")
            else:
                session.clear()
                session.permanent = True
                session["uid"] = cur.lastrowid
                flash("Welcome! Connect Claude using the steps below.", "ok")
                return redirect(url_for("page_settings"))
    return render_template("signup.html")


@app.post("/logout")
def page_logout():
    session.clear()
    return redirect(url_for("page_login"))


# --------------------------------------------------------------------------- web pages


def status_filter_sql(status):
    if status in STATUSES:
        return " AND status=?", [status]
    if status == "flagged":
        return " AND status='pending' AND flags_json LIKE '%\"severity\": \"high\"%'", []
    return "", []


@app.get("/")
@login_required
def page_dashboard():
    db = get_db()
    uid = g.user["id"]
    status = request.args.get("status", "all")
    q = request.args.get("q", "").strip()[:80]
    clause, params = status_filter_sql(status)
    sql = "SELECT * FROM claims WHERE user_id=?" + clause
    args = [uid] + params
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (" AND (merchant LIKE ? ESCAPE '\\' OR employee LIKE ? ESCAPE '\\' "
                "OR description LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')")
        args += [like] * 4
    sql += " ORDER BY number DESC LIMIT 300"
    claims = [claim_dict(r) for r in db.execute(sql, args).fetchall()]

    stats = {"pending": 0, "pending_totals": {}, "approved": 0, "approved_totals": {}, "rejected": 0, "flagged": 0}
    for r in db.execute("SELECT status, flags_json, amount_cents, currency FROM claims WHERE user_id=?", (uid,)):
        stats[r["status"]] += 1
        if r["status"] in ("pending", "approved"):
            bucket = stats[r["status"] + "_totals"]
            bucket[r["currency"]] = bucket.get(r["currency"], 0) + r["amount_cents"]
        if r["status"] == "pending" and '"severity": "high"' in r["flags_json"]:
            stats["flagged"] += 1
    total = db.execute("SELECT COUNT(*) AS n FROM claims WHERE user_id=?", (uid,)).fetchone()["n"]
    return render_template("dashboard.html", claims=claims, stats=stats, status=status, q=q, total=total)


def form_data():
    return {k: request.form.get(k, "") for k in FIELD_KEYS}


@app.route("/claims/new", methods=["GET", "POST"])
@login_required
def page_claim_new():
    cats = list(user_limits(g.user).keys())
    form = {"currency": g.user["base_currency"], "date": date.today().isoformat()}
    errors = []
    if request.method == "POST":
        form = form_data()
        clean, errors = clean_fields(form, cats, required=("merchant", "amount", "date", "category"))
        if not errors:
            number = insert_claim(get_db(), g.user, clean, "web", g.user["email"])
            flash(f"Claim TC-{number} created.", "ok")
            return redirect(url_for("page_claim", number=number))
    return render_template("claim_new.html", categories=cats, form=form, errors=errors)


@app.route("/claims/<int:number>", methods=["GET"])
@login_required
def page_claim(number):
    row = get_claim_row(g.user["id"], number)
    if row is None:
        abort(404)
    return render_claim(row)


def render_claim(row, form=None, errors=None):
    claim = claim_dict(row)
    events = get_db().execute("SELECT * FROM claim_events WHERE claim_id=? ORDER BY id DESC",
                              (row["id"],)).fetchall()
    if form is None:
        form = {
            "merchant": claim["merchant"], "amount": f"{claim['amount_cents'] / 100:.2f}",
            "currency": claim["currency"], "date": claim["expense_date"], "category": claim["category"],
            "description": claim["description"], "payment_method": claim["payment_method"],
            "receipt_notes": claim["receipt_notes"], "employee": claim["employee"],
            "tax_amount": "" if claim["tax_cents"] is None else f"{claim['tax_cents'] / 100:.2f}",
        }
    cats = list(user_limits(g.user).keys())
    if claim["category"] not in cats:
        cats.append(claim["category"])
    return render_template("claim.html", claim=claim, events=events, form=form,
                           errors=errors or [], categories=cats)


@app.post("/claims/<int:number>/edit")
@login_required
def page_claim_edit(number):
    row = get_claim_row(g.user["id"], number)
    if row is None:
        abort(404)
    cats = list(user_limits(g.user).keys())
    if row["category"] not in cats:
        cats.append(row["category"])
    form = form_data()
    clean, errors = clean_fields(form, cats, required=("merchant", "amount", "date", "category"))
    if not errors:
        if not form.get("tax_amount", "").strip():
            clean["tax_cents"] = None
        clean.setdefault("currency", row["currency"])
        try:
            changed = update_claim_row(get_db(), g.user, row, clean, g.user["email"])
        except ValueError as exc:
            errors.append(str(exc))
        else:
            flash("Saved." if changed else "No changes to save.", "ok")
            return redirect(url_for("page_claim", number=number))
    return render_claim(row, form=form, errors=errors), 400


@app.post("/claims/<int:number>/status")
@login_required
def page_claim_status(number):
    row = get_claim_row(g.user["id"], number)
    if row is None:
        abort(404)
    action = request.form.get("action", "")
    note = request.form.get("note", "").replace("\x00", "").strip()[:300]
    new_status = {"approve": "approved", "reject": "rejected", "reopen": "pending"}.get(action)
    if new_status is None:
        abort(400, "Unknown action.")
    db = get_db()
    db.execute("UPDATE claims SET status=?, reviewer_note=?, updated_at=? WHERE id=?",
               (new_status, "" if action == "reopen" else note, now_iso(), row["id"]))
    add_event(db, row["id"], g.user["email"], new_status if action != "reopen" else "reopened", note)
    db.commit()
    flash(f"Claim TC-{number} {new_status if action != 'reopen' else 'reopened'}.", "ok")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/")
                    and not (request.form.get("back") or "").startswith("//")
                    else url_for("page_claim", number=number))


@app.post("/claims/<int:number>/delete")
@login_required
def page_claim_delete(number):
    row = get_claim_row(g.user["id"], number)
    if row is None:
        abort(404)
    db = get_db()
    db.execute("DELETE FROM claims WHERE id=?", (row["id"],))
    db.commit()
    flash(f"Claim TC-{number} deleted.", "ok")
    return redirect(url_for("page_dashboard"))


def csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/export.csv")
@login_required
def page_export():
    status = request.args.get("status", "all")
    clause, params = status_filter_sql(status)
    rows = get_db().execute("SELECT * FROM claims WHERE user_id=?" + clause + " ORDER BY number",
                            [g.user["id"]] + params).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Claim", "Date", "Employee", "Merchant", "Category", "Description", "Amount", "Tax",
                     "Currency", "Payment method", "Status", "Policy flags", "Reviewer note",
                     "Filed via", "Created (UTC)"])
    for r in rows:
        c = claim_dict(r)
        writer.writerow([csv_safe(x) for x in [
            f"TC-{c['number']}", c["expense_date"], c["employee"], c["merchant"], c["category"],
            c["description"], f"{c['amount_cents'] / 100:.2f}",
            "" if c["tax_cents"] is None else f"{c['tax_cents'] / 100:.2f}", c["currency"],
            c["payment_method"], c["status"], "; ".join(f["message"] for f in c["flags"]),
            c["reviewer_note"], "Claude (MCP)" if c["source"] == "claude" else "Web", c["created_at"]]])
    filename = f"tally-claims-{date.today().strftime('%Y%m%d')}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def mcp_url_for(user):
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    path = url_for("mcp_endpoint", token=user["token"])
    return (base + path) if base else url_for("mcp_endpoint", token=user["token"], _external=True)


@app.get("/settings")
@login_required
def page_settings():
    limits = user_limits(g.user)
    return render_template("settings.html", mcp_url=mcp_url_for(g.user), limits=list(limits.items()))


@app.post("/settings/policy")
@login_required
def page_settings_policy():
    currency = request.form.get("base_currency", "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        flash("Currency must be a 3-letter code like USD.", "error")
        return redirect(url_for("page_settings"))
    try:
        count = min(int(request.form.get("count", "0")), 40)
    except ValueError:
        count = 0
    limits, seen = {}, set()
    for i in range(count):
        name = request.form.get(f"cat_name_{i}", "").strip()
        if not name:
            continue
        if len(name) > 40:
            flash(f"Category name '{name[:20]}…' is too long (max 40).", "error")
            return redirect(url_for("page_settings"))
        if name.lower() in seen:
            flash(f"Category '{name}' appears twice.", "error")
            return redirect(url_for("page_settings"))
        raw = request.form.get(f"cat_limit_{i}", "").strip()
        try:
            cents = parse_money(raw, f"limit for {name}", allow_zero=True) if raw else 0
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("page_settings"))
        seen.add(name.lower())
        limits[name] = cents / 100
    if not limits:
        flash("Keep at least one category.", "error")
        return redirect(url_for("page_settings"))
    if len(limits) > 20:
        flash("Maximum 20 categories.", "error")
        return redirect(url_for("page_settings"))
    db = get_db()
    db.execute("UPDATE users SET base_currency=?, limits_json=? WHERE id=?",
               (currency, json.dumps(limits), g.user["id"]))
    db.commit()
    flash("Policy saved. New and edited claims use these limits.", "ok")
    return redirect(url_for("page_settings"))


@app.post("/settings/token")
@login_required
def page_settings_token():
    db = get_db()
    db.execute("UPDATE users SET token=? WHERE id=?", (new_token(), g.user["id"]))
    db.commit()
    flash("New connector link created. The old link no longer works; update it in Claude.", "ok")
    return redirect(url_for("page_settings"))


# --------------------------------------------------------------------------- MCP server

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INSTRUCTIONS = (
    "Tally Claims files expense claims for a small team. When the user gives you a receipt (photo, PDF or "
    "text), read it yourself, then call create_claim once per receipt with the values printed on it. "
    "Never invent values: if the date, amount or merchant is unreadable, ask the user. Put anything "
    "uncertain in receipt_notes. Amounts are plain numbers (no currency symbols) and dates are YYYY-MM-DD. "
    "The app runs its own policy checks and returns flags; relay them to the user and give them the review link."
)


def tool_definitions(user):
    cats = list(user_limits(user).keys())
    claim_props = {
        "merchant": {"type": "string", "description": "Business name as printed on the receipt."},
        "amount": {"type": "number", "description": "Total paid including tax, as a plain number, e.g. 23.50."},
        "currency": {"type": "string",
                     "description": f"3-letter currency code. Defaults to {user['base_currency']} if omitted."},
        "date": {"type": "string", "description": "Date of the expense, YYYY-MM-DD."},
        "category": {"type": "string", "enum": cats, "description": "Best matching expense category."},
        "description": {"type": "string",
                        "description": "Short business purpose, e.g. 'Lunch with client from Acme'."},
        "payment_method": {"type": "string", "description": "e.g. Visa 4242, cash, company card."},
        "receipt_notes": {"type": "string",
                          "description": "Line items or anything unclear or notable on the receipt."},
        "employee": {"type": "string", "description": "Name of the employee who paid."},
        "tax_amount": {"type": "number", "description": "Tax/VAT portion if printed on the receipt."},
    }
    ro = {"readOnlyHint": True, "openWorldHint": False}
    return [
        {"name": "create_claim",
         "description": "File a new expense claim from a receipt. The app validates it, runs policy checks "
                        "(category limits, duplicates, dates, alcohol) and returns any flags plus a review link.",
         "inputSchema": {"type": "object", "properties": claim_props,
                         "required": ["merchant", "amount", "date", "category"]},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                         "openWorldHint": False}},
        {"name": "list_claims",
         "description": "List recent claims, newest first. Optionally filter by status.",
         "inputSchema": {"type": "object", "properties": {
             "status": {"type": "string", "enum": list(STATUSES)},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."}}},
         "annotations": ro},
        {"name": "get_claim",
         "description": "Get one claim with its policy flags and review link.",
         "inputSchema": {"type": "object", "properties": {
             "number": {"type": "integer", "description": "Claim number, e.g. 12 for TC-12."}},
             "required": ["number"]},
         "annotations": ro},
        {"name": "update_claim",
         "description": "Correct fields on a claim that is still pending. Only send the fields to change. "
                        "Policy flags are recalculated.",
         "inputSchema": {"type": "object",
                         "properties": dict({"number": {"type": "integer",
                                                        "description": "Claim number, e.g. 12 for TC-12."}},
                                            **claim_props),
                         "required": ["number"]},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                         "openWorldHint": False}},
        {"name": "get_policy",
         "description": "Show the expense policy: categories, per-claim limits and the automatic checks.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": ro},
        {"name": "expense_summary",
         "description": "Totals by category and status, optionally for one month (YYYY-MM). Rejected claims "
                        "are excluded from category totals.",
         "inputSchema": {"type": "object", "properties": {
             "month": {"type": "string", "description": "Optional month filter, YYYY-MM."}}},
         "annotations": ro},
    ]


def claim_public(c):
    return {
        "id": f"TC-{c['number']}", "number": c["number"], "status": c["status"], "merchant": c["merchant"],
        "amount": c["amount_cents"] / 100, "currency": c["currency"], "date": c["expense_date"],
        "category": c["category"], "description": c["description"], "employee": c["employee"],
        "payment_method": c["payment_method"],
        "tax_amount": None if c["tax_cents"] is None else c["tax_cents"] / 100,
        "flags": [f["message"] for f in c["flags"]], "filed_via": "Claude" if c["source"] == "claude" else "Web",
        "reviewer_note": c["reviewer_note"],
        "review_url": url_for("page_claim", number=c["number"], _external=True),
    }


def parse_number_arg(args):
    val = args.get("number")
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip().upper().removeprefix("TC-").isdigit():
        return int(val.strip().upper().removeprefix("TC-"))
    if isinstance(val, float) and val.is_integer():
        return int(val)
    return None


def tool_create_claim(user, args):
    cats = list(user_limits(user).keys())
    unknown = [k for k in args if k not in FIELD_KEYS]
    if unknown:
        return "Unknown field(s): " + ", ".join(unknown) + ". Allowed: " + ", ".join(FIELD_KEYS), True
    clean, errors = clean_fields(args, cats, required=("merchant", "amount", "date", "category"))
    if errors:
        return "Claim not created. Fix these and try again:\n- " + "\n- ".join(errors), True
    db = get_db()
    number = insert_claim(db, user, clean, "claude", "Claude (MCP)")
    c = claim_dict(get_claim_row(user["id"], number))
    pub = claim_public(c)
    lines = [f"Filed claim {pub['id']}: {c['merchant']}, {fmt_money(c['amount_cents'], c['currency'])}, "
             f"{c['category']}, {c['expense_date']}. Status: pending review."]
    if pub["flags"]:
        lines.append("Policy flags: " + " | ".join(pub["flags"]))
    else:
        lines.append("No policy flags.")
    lines.append("Review or edit it here (login required): " + pub["review_url"])
    lines.append(json.dumps(pub))
    return "\n".join(lines), False


def tool_list_claims(user, args):
    status = args.get("status")
    if status is not None and status not in STATUSES:
        return "'status' must be one of: " + ", ".join(STATUSES), True
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return "'limit' must be an integer from 1 to 50", True
    sql, params = "SELECT * FROM claims WHERE user_id=?", [user["id"]]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY number DESC LIMIT ?"
    params.append(limit)
    rows = [claim_public(claim_dict(r)) for r in get_db().execute(sql, params).fetchall()]
    if not rows:
        return "No claims found.", False
    return f"{len(rows)} claim(s), newest first.\n" + json.dumps(rows), False


def tool_get_claim(user, args):
    number = parse_number_arg(args)
    if number is None:
        return "'number' must be an integer such as 12 for TC-12", True
    row = get_claim_row(user["id"], number)
    if row is None:
        return f"No claim TC-{number}.", True
    return json.dumps(claim_public(claim_dict(row))), False


def tool_update_claim(user, args):
    number = parse_number_arg(args)
    if number is None:
        return "'number' must be an integer such as 12 for TC-12", True
    allowed = ["number"] + FIELD_KEYS
    unknown = [k for k in args if k not in allowed]
    if unknown:
        return "Unknown field(s): " + ", ".join(unknown) + ". Allowed: " + ", ".join(allowed), True
    row = get_claim_row(user["id"], number)
    if row is None:
        return f"No claim TC-{number}.", True
    if row["status"] != "pending":
        return (f"TC-{number} is {row['status']}. Claude can only change pending claims; "
                "ask the reviewer to reopen it in the app."), True
    data = {k: v for k, v in args.items() if k in FIELD_KEYS}
    if not data:
        return "Nothing to update. Send at least one field to change.", True
    cats = list(user_limits(user).keys())
    if row["category"] not in cats:
        cats.append(row["category"])
    clean, errors = clean_fields(data, cats)
    if errors:
        return "Claim not changed. Fix these and try again:\n- " + "\n- ".join(errors), True
    try:
        changed = update_claim_row(get_db(), user, row, clean, "Claude (MCP)")
    except ValueError as exc:
        return f"Claim not changed: {exc}", True
    c = claim_dict(get_claim_row(user["id"], number))
    pub = claim_public(c)
    head = (f"Updated TC-{number} ({', '.join(sorted(changed))})." if changed
            else f"TC-{number} already had those values.")
    flags = ("Policy flags: " + " | ".join(pub["flags"])) if pub["flags"] else "No policy flags."
    return f"{head}\n{flags}\n{json.dumps(pub)}", False


def tool_get_policy(user, _args):
    base = user["base_currency"]
    limits = user_limits(user)
    lines = [f"Policy currency: {base}. Per-claim limits (0 = no limit):"]
    lines += [f"- {name}: {float(v):,.2f}" for name, v in limits.items()]
    lines += ["Automatic checks: over category limit, possible duplicate (same merchant, amount, date), "
              f"future date, older than {STALE_DAYS} days, weekend expense, alcohol mentioned, "
              "large claim without a business purpose, foreign currency (limit not checked)."]
    return "\n".join(lines), False


def tool_expense_summary(user, args):
    month = args.get("month")
    sql = "SELECT category, status, currency, COUNT(*) AS n, SUM(amount_cents) AS total FROM claims WHERE user_id=?"
    params = [user["id"]]
    if month is not None:
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            return "'month' must look like 2026-11", True
        sql += " AND expense_date LIKE ?"
        params.append(month + "-%")
    sql += " GROUP BY category, status, currency ORDER BY category"
    rows = get_db().execute(sql, params).fetchall()
    if not rows:
        return "No claims for that period.", False
    by_status, by_category = {}, {}
    for r in rows:
        key = f"{r['status']} ({r['currency']})"
        entry = by_status.setdefault(key, {"claims": 0, "total": 0})
        entry["claims"] += r["n"]
        entry["total"] += r["total"]
        if r["status"] != "rejected":
            ckey = f"{r['category']} ({r['currency']})"
            centry = by_category.setdefault(ckey, {"claims": 0, "total": 0})
            centry["claims"] += r["n"]
            centry["total"] += r["total"]
    for table in (by_status, by_category):
        for entry in table.values():
            entry["total"] = entry["total"] / 100
    return json.dumps({"period": month or "all time", "by_status": by_status,
                       "by_category_excluding_rejected": by_category}), False


TOOL_HANDLERS = {
    "create_claim": tool_create_claim, "list_claims": tool_list_claims, "get_claim": tool_get_claim,
    "update_claim": tool_update_claim, "get_policy": tool_get_policy, "expense_summary": tool_expense_summary,
}


def rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def mcp_dispatch(user, msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid Request")
    method = msg.get("method")
    if method is None or "id" not in msg:
        return None  # notification, or a response from the client: nothing to send back
    mid = msg["id"]
    params = msg.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return rpc_error(mid, -32600, "Invalid Request")
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOLS else PROTOCOLS[0]
        return rpc_result(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tally-claims", "title": "Tally Claims", "version": "1.0.0"},
            "instructions": SERVER_INSTRUCTIONS})
    if method == "ping":
        return rpc_result(mid, {})
    if method == "tools/list":
        return rpc_result(mid, {"tools": tool_definitions(user)})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return rpc_error(mid, -32602, "'arguments' must be an object")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return rpc_error(mid, -32602, f"Unknown tool: {name}")
        try:
            text, is_error = handler(user, args)
        except Exception:  # never leak internals to the client
            app.logger.exception("tool %s failed", name)
            get_db().rollback()
            text, is_error = "Something went wrong inside Tally Claims. Please try again.", True
        return rpc_result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = method.split("/")[0]
        return rpc_result(mid, {key: []})
    return rpc_error(mid, -32601, f"Method not found: {method}")


@app.route("/mcp/<token>", methods=["POST", "GET", "DELETE", "OPTIONS"])
def mcp_endpoint(token):
    if request.method == "OPTIONS":
        return Response(status=204)
    user = get_db().execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if user is None:
        return jsonify(error="Unknown connector link"), 404
    if request.method != "POST":
        return Response("This is a Model Context Protocol endpoint. Add it to Claude as a custom connector.\n",
                        status=405, headers={"Allow": "POST"}, mimetype="text/plain")
    payload = request.get_json(silent=True, force=True)
    if payload is None:
        return jsonify(rpc_error(None, -32700, "Parse error")), 400
    if isinstance(payload, list):
        if not payload:
            return jsonify(rpc_error(None, -32600, "Invalid Request")), 400
        responses = [r for r in (mcp_dispatch(user, m) for m in payload) if r is not None]
        if not responses:
            return Response(status=202)
        return jsonify(responses)
    response = mcp_dispatch(user, payload)
    if response is None:
        return Response(status=202)
    return jsonify(response)


@app.after_request
def add_headers(resp):
    if request.path.startswith("/mcp/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Accept, Mcp-Protocol-Version, Mcp-Session-Id, Authorization")
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/mcp/"):
        return jsonify(error="Not found"), 404
    return render_template("error.html", code=404, message="That page doesn't exist."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413, message="That request is too large."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)

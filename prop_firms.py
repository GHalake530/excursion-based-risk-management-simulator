"""
Prop firm tracker: accounts, cash entries (expenses, refunds, payout requests),
payout receipts and an audit trail - ported from Trade Journal's prop-firm
section - plus the payout-eligibility and risk rules used to check simulated
results.

Cash is stored in integer minor units (cents) and is independent of trading
P&L: account sizes are descriptive and never count as money spent. Records
are never deleted; incorrect ones are voided (and can be restored), and every
change is written to an audit log with a reason.
"""

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

PROP_PROGRAMS = ("one_step", "two_step", "evaluation", "verification", "funded", "instant_funded", "live")
FUNDED_PROGRAMS = ("funded", "instant_funded", "live")
EVAL_PROGRAMS = ("one_step", "two_step", "evaluation", "verification")
PROP_STATES = ("active", "passed", "breached", "closed")
PAYOUT_STATES = ("requested", "approved", "completed", "rejected", "cancelled")
OPEN_PAYOUT_STATES = ("requested", "approved")
EXPENSE_CATEGORIES = (
    "evaluation", "reset", "activation", "subscription",
    "platform", "market_data", "transfer", "other",
)
CSV_COLUMNS = [
    "id", "kind", "firm", "account_id", "currency", "date",
    "amount", "category", "expense_id", "reference", "notes",
]
EXPORT_COLUMNS = [
    "id", "entry_id", "account_id", "firm", "currency", "date",
    "kind", "amount", "category", "reference",
]

_ZERO_DECIMAL = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}
_THREE_DECIMAL = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "AUD": "A$", "CAD": "C$"}


class PropError(Exception):
    """A user-facing validation error."""


def require(condition, message):
    if not condition:
        raise PropError(message)


_LABELS = {"one_step": "1-Step evaluation", "two_step": "2-Step evaluation"}


def label(value):
    if value in _LABELS:
        return _LABELS[value]
    return value.replace("_", " ").capitalize() if value else ""


def today_iso():
    return date.today().isoformat()


def new_id():
    return uuid.uuid4().hex[:16]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Money in integer minor units
# --------------------------------------------------------------------------
def currency_digits(code):
    require(isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code), "Choose a supported three-letter currency code.")
    if code in _ZERO_DECIMAL:
        return 0
    return 3 if code in _THREE_DECIMAL else 2


def to_minor(value, currency):
    """Parse decimal input straight into minor units; never silently round."""
    digits = currency_digits(currency)
    if isinstance(value, (int, float)):
        value = f"{value:.{digits}f}"
    require(isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip()), "Enter a nonnegative decimal amount.")
    whole, _, fraction = value.strip().partition(".")
    fraction = fraction.rstrip("0") if len(fraction) > digits else fraction
    require(len(fraction) <= digits, f"{currency} accepts {digits} decimal places.")
    result = int(whole) * 10**digits + int(fraction.ljust(digits, "0") or 0)
    require(result <= 10_000_000_000, "Amount is too large.")
    return result


def from_minor(value, currency):
    digits = currency_digits(currency)
    return f"{value / 10**digits:.{digits}f}"


def fmt_money(value, currency, signed=False):
    if value is None or not currency:
        return "—"
    digits = currency_digits(currency)
    amount = abs(value) / 10**digits
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    symbol = _SYMBOLS.get(currency)
    body = f"{amount:,.{digits}f}"
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{currency} {body}"


def expected_payout(entry):
    """Trader share of a payout request, minus fees withheld by the firm."""
    return (entry["amount_minor"] * entry["split_bps"] + 5000) // 10000 - entry["fee_minor"]


def received_payout(payout_id, receipts):
    return sum(
        r["amount_minor"] * (-1 if r["kind"] == "reversal" else 1)
        for r in receipts
        if not r["voided"] and r["payout_id"] == payout_id
    )


# --------------------------------------------------------------------------
# Cash views
# --------------------------------------------------------------------------
def cash_movements(entries, receipts):
    """Settled cash only: expenses, refunds, and money actually received or
    reversed on payouts (a payout request itself moves no cash)."""
    by_id = {e["id"]: e for e in entries if not e["voided"]}
    rows = [
        {
            "id": e["id"], "entry_id": e["id"], "account_id": e["account_id"], "firm": e["firm"],
            "currency": e["currency"], "date": e["occurred_on"], "kind": e["kind"],
            "amount_minor": e["amount_minor"], "category": e["category"], "reference": e["reference"],
        }
        for e in by_id.values()
        if e["kind"] != "payout"
    ]
    for r in receipts:
        entry = by_id.get(r["payout_id"])
        if entry is None or r["voided"]:
            continue
        rows.append({
            "id": r["id"], "entry_id": entry["id"], "account_id": entry["account_id"], "firm": entry["firm"],
            "currency": entry["currency"], "date": r["occurred_on"], "kind": r["kind"],
            "amount_minor": r["amount_minor"], "category": "payout", "reference": r["reference"],
        })
    return sorted(rows, key=lambda r: (r["date"], r["id"]))


def cash_summary(rows):
    require(len({r["currency"] for r in rows}) <= 1, "Select one currency before combining cash amounts.")
    spent = refunds = received = 0
    for r in rows:
        if r["kind"] == "expense":
            spent += r["amount_minor"]
        elif r["kind"] == "refund":
            refunds += r["amount_minor"]
        else:
            received += r["amount_minor"] * (-1 if r["kind"] == "reversal" else 1)
    net_spend = spent - refunds
    net = received - net_spend
    return {
        "spent": spent, "refunds": refunds, "net_spend": net_spend, "received": received,
        "net": net, "roi": net / net_spend if net_spend > 0 else None,
    }


def cash_timeline(rows):
    cash_summary(rows)  # Fail closed on mixed currencies.
    days = {}
    for r in rows:
        sign = -1 if r["kind"] in ("expense", "reversal") else 1
        days[r["date"]] = days.get(r["date"], 0) + r["amount_minor"] * sign
    net, out = 0, []
    for day in sorted(days):
        net += days[day]
        out.append({"date": day, "net": net})
    return out


def expense_rows(rows):
    groups = {}
    for r in rows:
        if r["kind"] == "expense":
            groups[r["category"]] = groups.get(r["category"], 0) + r["amount_minor"]
    return sorted(({"category": k, "amount": v} for k, v in groups.items()), key=lambda r: -r["amount"])


def payout_progress(entries, receipts, today):
    received = {}
    for r in receipts:
        if not r["voided"]:
            received[r["payout_id"]] = received.get(r["payout_id"], 0) + r["amount_minor"] * (-1 if r["kind"] == "reversal" else 1)
    out = []
    for e in entries:
        if e["kind"] != "payout" or e["voided"]:
            continue
        actual = received.get(e["id"], 0)
        expected = expected_payout(e)
        is_open = e["status"] in OPEN_PAYOUT_STATES
        out.append({
            "entry": e, "actual": actual, "expected": expected,
            "remaining": max(0, expected - actual) if is_open else 0,
            "variance": actual - expected,
            "partial": is_open and 0 < actual < expected,
            "overdue": is_open and bool(e["due_on"] and e["due_on"] < today) and actual < expected,
        })
    return out


def in_dates(day, start, end):
    return (not start or day >= start) and (not end or day <= end)


# --------------------------------------------------------------------------
# Payout eligibility and risk rules (defaults: Thunderbolt Turbo)
# --------------------------------------------------------------------------
DEFAULT_RULES = {
    "qualifying_days": 5,          # days with realized profit >= qualifying_day_pct
    "qualifying_day_pct": 0.5,     # % of account
    "min_total_profit_pct": 1.0,   # % of account before a payout
    "best_day_pct": 20.0,          # best day can't exceed this % of the withdrawal
    "daily_drawdown_pct": 3.0,     # max loss in one trading day
    "max_drawdown_pct": 6.0,       # trails the highest equity reached
    "max_trade_loss_pct": 1.5,     # max loss on one trade
}
RULE_LABELS = {
    "qualifying_days": "Qualifying trading days",
    "qualifying_day_pct": "Min profit per qualifying day (%)",
    "min_total_profit_pct": "Min total profit before payout (%)",
    "best_day_pct": "Best Day Rule (% of withdrawal)",
    "daily_drawdown_pct": "Daily drawdown (%)",
    "max_drawdown_pct": "Max drawdown, trailing (%)",
    "max_trade_loss_pct": "Max loss on one trade (%)",
}
ACCOUNT_SIZES = [5000, 10000, 25000, 50000, 100000, 200000]


def rule_limits(size, rules):
    """Dollar thresholds for an account size, as on the payout requirements sheet."""
    pct = lambda key: size * rules[key] / 100.0
    return {
        "qualifying_days": int(rules["qualifying_days"]),
        "per_day": pct("qualifying_day_pct"),
        "total": pct("min_total_profit_pct"),
        "best_day_pct": rules["best_day_pct"],
        "daily_dd": pct("daily_drawdown_pct"),
        "max_dd": pct("max_drawdown_pct"),
        "floor": size - pct("max_drawdown_pct"),
        "trade_loss": pct("max_trade_loss_pct"),
    }


def trailing_floor(size, high_water, rules):
    peak = max(size, high_water or 0)
    return peak - peak * rules["max_drawdown_pct"] / 100.0


def min_withdrawal_for_best_day(best_day, rules):
    return best_day * 100.0 / rules["best_day_pct"] if rules["best_day_pct"] > 0 else None


def evaluate_rules(trades, size, rules):
    """Check a chronological list of (timestamp, pnl) trades against the rules.

    Equity starts at `size`. The max drawdown floor trails the highest equity
    reached; the daily limit is checked against each day's running intraday
    loss, not just its close."""
    limits = rule_limits(size, rules)
    daily_close, daily_low = {}, {}
    equity = peak = float(size)
    min_cushion, dd_breach_on = float("inf"), None
    trade_breaches, worst_trade = 0, 0.0
    for ts, pnl in trades:
        day = ts.date()
        running = daily_close.get(day, 0.0) + pnl
        daily_close[day] = running
        daily_low[day] = min(daily_low.get(day, 0.0), running)
        equity += pnl
        floor = peak - peak * rules["max_drawdown_pct"] / 100.0
        min_cushion = min(min_cushion, equity - floor)
        if equity < floor and dd_breach_on is None:
            dd_breach_on = day
        peak = max(peak, equity)
        worst_trade = min(worst_trade, pnl)
        if -pnl > limits["trade_loss"]:
            trade_breaches += 1

    total = sum(daily_close.values())
    best_day = max(daily_close.values(), default=0.0)
    qualifying = sum(1 for v in daily_close.values() if v >= limits["per_day"])
    daily_breach_days = sorted(d for d, low in daily_low.items() if -low >= limits["daily_dd"])
    worst_day_low = min(daily_low.values(), default=0.0)
    best_day_share = (best_day / total * 100.0) if total > 0 else None
    best_day_ok = best_day_share is not None and best_day_share <= rules["best_day_pct"]
    breached = dd_breach_on is not None or bool(daily_breach_days) or trade_breaches > 0
    eligible = (
        not breached
        and qualifying >= limits["qualifying_days"]
        and total >= limits["total"]
        and best_day_ok
    )
    return {
        "limits": limits, "total": total, "best_day": best_day, "best_day_share": best_day_share,
        "best_day_ok": best_day_ok, "min_withdrawal": min_withdrawal_for_best_day(best_day, rules) if best_day > 0 else None,
        "qualifying": qualifying, "trading_days": len(daily_close),
        "worst_day_low": worst_day_low, "daily_breach_days": daily_breach_days,
        "dd_breach_on": dd_breach_on, "min_cushion": min_cushion if trades else None,
        "trade_breaches": trade_breaches, "worst_trade": worst_trade,
        "breached": breached, "eligible": eligible, "end_equity": equity, "peak": peak,
    }


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS prop_accounts (
    id TEXT PRIMARY KEY, firm TEXT NOT NULL, name TEXT NOT NULL, program TEXT NOT NULL,
    status TEXT NOT NULL, currency TEXT NOT NULL, size_minor INTEGER,
    parent_id TEXT REFERENCES prop_accounts(id), journal_account_id TEXT,
    opened_on TEXT NOT NULL, closed_on TEXT, renewal_on TEXT, renewal_minor INTEGER,
    notes TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prop_entries (
    id TEXT PRIMARY KEY, account_id TEXT REFERENCES prop_accounts(id), firm TEXT NOT NULL,
    kind TEXT NOT NULL, category TEXT NOT NULL, currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL, split_bps INTEGER NOT NULL, fee_minor INTEGER NOT NULL,
    occurred_on TEXT NOT NULL, due_on TEXT, status TEXT NOT NULL,
    parent_id TEXT REFERENCES prop_entries(id), reference TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', voided INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prop_entries_account_date ON prop_entries(account_id, occurred_on);
CREATE INDEX IF NOT EXISTS prop_entries_date ON prop_entries(occurred_on);
CREATE TABLE IF NOT EXISTS prop_receipts (
    id TEXT PRIMARY KEY, payout_id TEXT NOT NULL REFERENCES prop_entries(id), kind TEXT NOT NULL,
    amount_minor INTEGER NOT NULL, occurred_on TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', voided INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prop_receipts_payout ON prop_receipts(payout_id);
CREATE TABLE IF NOT EXISTS prop_audit (
    id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, before_json TEXT,
    after_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prop_audit_entity ON prop_audit(entity_type, entity_id);
CREATE TABLE IF NOT EXISTS prop_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_BOOL_COLS = {"archived", "voided"}


class _Rollback(Exception):
    def __init__(self, result):
        super().__init__("preview")
        self.result = result


class PropStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.executescript(_SCHEMA)

    # ---- low-level helpers -------------------------------------------------
    def _rows(self, sql, params=()):
        out = []
        for row in self.con.execute(sql, params).fetchall():
            d = dict(row)
            for k in _BOOL_COLS & d.keys():
                d[k] = bool(d[k])
            out.append(d)
        return out

    def _one(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _upsert(self, table, values):
        cols = list(values)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c != 'id')}"
        )
        self.con.execute(sql, [int(v) if isinstance(v, bool) else v for v in values.values()])

    def _audit(self, entity_type, entity_id, before, after, reason):
        self.con.execute(
            "INSERT INTO prop_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id(), entity_type, entity_id, json.dumps(before) if before else None, json.dumps(after), reason, now_iso()),
        )

    def _transaction(self, fn):
        with self._lock:
            self.con.execute("BEGIN")
            try:
                result = fn()
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            self.con.execute("COMMIT")
            return result

    # ---- reads ---------------------------------------------------------------
    def data(self):
        with self._lock:
            return {
                "accounts": self._rows("SELECT * FROM prop_accounts"),
                "entries": self._rows("SELECT * FROM prop_entries"),
                "receipts": self._rows("SELECT * FROM prop_receipts"),
                "today": today_iso(),
            }

    def history(self, entity_type, entity_id):
        require(entity_type in ("account", "entry"), "Choose an account or entry history.")
        with self._lock:
            return self._rows(
                "SELECT * FROM prop_audit WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC LIMIT 100",
                (entity_type, entity_id),
            )

    def rules(self):
        with self._lock:
            row = self._one("SELECT value FROM prop_settings WHERE key = 'rules'")
        saved = json.loads(row["value"]) if row else {}
        return {k: saved.get(k, v) for k, v in DEFAULT_RULES.items()}

    def save_rules(self, rules):
        for k in DEFAULT_RULES:
            require(isinstance(rules.get(k), (int, float)) and rules[k] >= 0, f"Enter a valid value for {RULE_LABELS[k]}.")
        with self._lock:
            self.con.execute(
                "INSERT INTO prop_settings (key, value) VALUES ('rules', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps({k: rules[k] for k in DEFAULT_RULES}),),
            )

    # ---- mutations -------------------------------------------------------------
    def mutate(self, body):
        return self._transaction(lambda: self._mutate(body))

    def _mutate(self, body):
        today = today_iso()

        def text(v, title, max_len=200, required=True):
            require(
                isinstance(v, str) and len(v.strip()) <= max_len and (not required or v.strip()),
                f"Enter {title}.",
            )
            return v.strip()

        def id_value(v):
            value = text(v, "a record ID", 100)
            require(re.fullmatch(r"[a-zA-Z0-9_-]+", value), "Invalid record ID.")
            return value

        def optional_id(v):
            return None if v in ("", None) else id_value(v)

        def choice(v, values, name):
            require(v in values, f"Choose {name}.")
            return v

        def a_date(v, title, future=False):
            if isinstance(v, date):
                v = v.isoformat()
            day = text(v, title, 10)
            try:
                valid = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)) and date.fromisoformat(day).isoformat() == day
            except ValueError:
                valid = False
            require(valid, f"Enter a valid {title}.")
            require(future or day <= today, f"{title[0].upper()}{title[1:]} cannot be in the future.")
            return day

        def maybe_date(v, title, future=False):
            return None if v in ("", None) else a_date(v, title, future)

        def money(v, code):
            return to_minor(v, code)

        def currency(v):
            code = text(v, "a currency", 3).upper()
            currency_digits(code)
            return code

        def details():
            return {
                "reference": text(body.get("reference") or "", "a reference up to 200 characters", 200, False),
                "notes": text(body.get("notes") or "", "notes up to 5,000 characters", 5000, False),
            }

        def check_revision(old, revision):
            if revision != (old["revision"] if old else 0):
                raise PropError("This record changed in another view. Refresh and review it before saving.")

        def same(old, values):
            return all(old.get(k) == v for k, v in values.items())

        def edit_reason(old):
            return text(body.get("reason"), "a reason for this change", 500) if old else "Created"

        action = body.get("action")
        rid = id_value(body.get("id"))

        if action == "account.save":
            old = self._one("SELECT * FROM prop_accounts WHERE id = ?", (rid,))
            code = currency(body.get("currency"))
            parent_id = optional_id(body.get("parent_id"))
            values = {
                "firm": text(body.get("firm"), "a firm name"),
                "name": text(body.get("name"), "an account or attempt name"),
                "program": choice(body.get("program"), PROP_PROGRAMS, "an account program"),
                "status": choice(body.get("status"), PROP_STATES, "an account status"),
                "currency": code,
                "size_minor": None if body.get("size") in ("", None) else money(body.get("size"), code),
                "parent_id": parent_id,
                "journal_account_id": old["journal_account_id"] if old else None,
                "opened_on": a_date(body.get("opened_on"), "opening date"),
                "closed_on": maybe_date(body.get("closed_on"), "closing date"),
                "renewal_on": maybe_date(body.get("renewal_on"), "next renewal date", True),
                "renewal_minor": None if body.get("renewal_amount") in ("", None) else money(body.get("renewal_amount"), code),
                "notes": text(body.get("notes") or "", "notes up to 5,000 characters", 5000, False),
            }
            require(not values["closed_on"] or values["closed_on"] >= values["opened_on"], "Closing date must follow opening date.")
            require(
                (not values["closed_on"]) if values["status"] == "active" else bool(values["closed_on"]),
                "Active accounts have no closing date; resolved accounts need a closing date.",
            )
            require(
                values["status"] != "passed" or values["program"] in EVAL_PROGRAMS,
                "Only an evaluation phase (1-step, 2-step, evaluation or verification) can be marked passed. Track funding as a new linked phase.",
            )
            require(not values["renewal_on"] or values["renewal_minor"] is not None, "Enter the expected renewal amount.")
            require(not values["renewal_on"] or values["status"] == "active", "Clear the renewal reminder for a resolved account.")
            if parent_id:
                parent = self._one("SELECT * FROM prop_accounts WHERE id = ?", (parent_id,))
                require(
                    parent and parent["id"] != rid and parent["firm"].lower() == values["firm"].lower()
                    and parent["currency"] == code and parent["opened_on"] <= values["opened_on"],
                    "Choose an earlier account or attempt at the same firm and currency.",
                )
                visited, ancestor = {rid}, parent
                while ancestor:
                    require(ancestor["id"] not in visited, "Account lineage cannot contain a cycle.")
                    visited.add(ancestor["id"])
                    ancestor = (
                        self._one("SELECT * FROM prop_accounts WHERE id = ?", (ancestor["parent_id"],))
                        if ancestor["parent_id"] else None
                    )
            if old:
                phases = self._rows("SELECT * FROM prop_accounts WHERE parent_id = ?", (rid,))
                require(
                    all(
                        a["firm"].lower() == values["firm"].lower() and a["currency"] == code and a["opened_on"] >= values["opened_on"]
                        for a in phases
                    ),
                    "This change conflicts with a linked phase or reset.",
                )
                linked = self._rows("SELECT * FROM prop_entries WHERE account_id = ?", (rid,))
                require(
                    not linked or (old["currency"] == code and old["firm"] == values["firm"] and old["program"] == values["program"]),
                    "An account with cash records keeps its firm, currency and phase. Create a linked phase for funding or a reset.",
                )
                require(all(e["occurred_on"] >= values["opened_on"] for e in linked), "Opening date cannot be after this account's cash records.")
            else:
                require(self.con.execute("SELECT count(*) FROM prop_accounts").fetchone()[0] < 2000, "Account limit reached (2,000).")
            if old and body.get("revision") == 0 and same(old, values):
                return {"id": rid}
            check_revision(old, body.get("revision"))
            updated = {
                "id": rid, **values,
                "archived": old["archived"] if old else False,
                "revision": (old["revision"] if old else 0) + 1,
                "created_at": old["created_at"] if old else now_iso(),
                "updated_at": now_iso(),
            }
            self._upsert("prop_accounts", updated)
            self._audit("account", rid, old, updated, edit_reason(old))
            return {"id": rid}

        if action == "account.archive":
            old = self._one("SELECT * FROM prop_accounts WHERE id = ?", (rid,))
            require(old, "Account not found.")
            check_revision(old, body.get("revision"))
            require(isinstance(body.get("archived"), bool), "Choose archive or restore.")
            updated = {**old, "archived": body["archived"], "revision": old["revision"] + 1, "updated_at": now_iso()}
            self._upsert("prop_accounts", updated)
            self._audit("account", rid, old, updated, text(body.get("reason"), "a reason", 500))
            return {"id": rid}

        if action == "entry.save":
            old = self._one("SELECT * FROM prop_entries WHERE id = ?", (rid,))
            require(not (old and old["voided"]), "Restore this entry before editing it.")
            account_id = optional_id(body.get("account_id"))
            account = self._one("SELECT * FROM prop_accounts WHERE id = ?", (account_id,)) if account_id else None
            require(not account_id or account, "Prop account not found.")
            code = currency(body.get("currency"))
            kind = choice(body.get("kind"), ("expense", "refund", "payout"), "an entry type")
            amount_minor = money(body.get("amount"), code)
            parent_id = optional_id(body.get("parent_id"))
            split_bps = money(body.get("split_percent"), "USD") if kind == "payout" else 10000
            fee_minor = money(body.get("fee") or "0", code) if kind == "payout" else 0
            require(amount_minor > 0 or kind == "expense", "Enter an amount greater than zero.")
            require(0 < split_bps <= 10000, "Trader share must be greater than 0 and at most 100 percent.")
            values = {
                "account_id": account_id,
                "firm": text(body.get("firm"), "a firm name"),
                "kind": kind,
                "currency": code,
                "amount_minor": amount_minor,
                "split_bps": split_bps,
                "fee_minor": fee_minor,
                "category": choice(body.get("category"), EXPENSE_CATEGORIES, "an expense category") if kind == "expense" else kind,
                "occurred_on": a_date(body.get("occurred_on"), "request date" if kind == "payout" else "cash date"),
                "due_on": maybe_date(body.get("due_on"), "expected payment date", True) if kind == "payout" else None,
                "status": choice(body.get("status"), PAYOUT_STATES, "a payout status") if kind == "payout" else "completed",
                "parent_id": parent_id if kind == "refund" else None,
                **details(),
            }
            if account:
                require(
                    account["firm"] == values["firm"] and account["currency"] == code and values["occurred_on"] >= account["opened_on"],
                    "Use the selected account's firm, currency and a date on or after it opened.",
                )
            if kind == "payout":
                require(account and account["program"] in FUNDED_PROGRAMS, "Payouts need a funded, instant-funded or live prop account.")
                require(expected_payout(values) >= 0, "Withheld fees cannot exceed your share of the payout.")
                require(not values["due_on"] or values["due_on"] >= values["occurred_on"], "Expected payment date must follow the request date.")
            if kind == "refund":
                expense = self._one("SELECT * FROM prop_entries WHERE id = ?", (parent_id,)) if parent_id else None
                require(
                    expense and expense["id"] != rid and not expense["voided"] and expense["kind"] == "expense"
                    and expense["currency"] == code and expense["firm"] == values["firm"]
                    and expense["account_id"] == account_id and expense["occurred_on"] <= values["occurred_on"],
                    "Link this refund to a matching expense in the same account, firm and currency.",
                )
                other = sum(
                    e["amount_minor"] for e in self._rows("SELECT * FROM prop_entries WHERE parent_id = ?", (expense["id"],))
                    if not e["voided"] and e["id"] != rid
                )
                require(other + amount_minor <= expense["amount_minor"], "Refunds cannot exceed the original expense.")
            if old:
                require(
                    old["kind"] == kind and old["currency"] == code and old["account_id"] == account_id
                    and old["firm"] == values["firm"] and old["parent_id"] == values["parent_id"],
                    "Account, currency, firm, entry type and refund link are fixed. Void an incorrect entry and add a replacement.",
                )
                children = [e for e in self._rows("SELECT * FROM prop_entries WHERE parent_id = ?", (rid,)) if not e["voided"]]
                require(
                    not children or (
                        sum(e["amount_minor"] for e in children) <= amount_minor
                        and all(e["occurred_on"] >= values["occurred_on"] for e in children)
                    ),
                    "This change conflicts with recorded refunds.",
                )
                receipts = self._rows("SELECT * FROM prop_receipts WHERE payout_id = ?", (rid,))
                require(
                    all(r["occurred_on"] >= values["occurred_on"] for r in receipts if not r["voided"]),
                    "Request date cannot follow a recorded payment.",
                )
                net = received_payout(rid, receipts)
                require(values["status"] not in ("rejected", "cancelled") or net == 0, "Record a reversal before rejecting or cancelling a paid payout.")
                require(values["status"] != "completed" or kind != "payout" or net > 0, "Record money received before completing a payout.")
            else:
                require(
                    kind != "payout" or values["status"] != "completed",
                    "Add the payout request first, then record the actual receipt.",
                )
                require(self.con.execute("SELECT count(*) FROM prop_entries").fetchone()[0] < 20_000, "Entry limit reached (20,000).")
            if old and body.get("revision") == 0 and same(old, values):
                return {"id": rid}
            check_revision(old, body.get("revision"))
            updated = {
                "id": rid, **values,
                "voided": False,
                "revision": (old["revision"] if old else 0) + 1,
                "created_at": old["created_at"] if old else now_iso(),
                "updated_at": now_iso(),
            }
            self._upsert("prop_entries", updated)
            self._audit("entry", rid, old, updated, edit_reason(old))
            return {"id": rid}

        if action == "entry.void":
            old = self._one("SELECT * FROM prop_entries WHERE id = ?", (rid,))
            require(old, "Entry not found.")
            check_revision(old, body.get("revision"))
            require(isinstance(body.get("voided"), bool), "Choose void or restore.")
            refunds = [e for e in self._rows("SELECT * FROM prop_entries WHERE parent_id = ?", (rid,)) if not e["voided"]]
            require(not body["voided"] or not refunds, "Void linked refunds before voiding their expense.")
            if not body["voided"] and old["kind"] == "refund":
                parent = self._one("SELECT * FROM prop_entries WHERE id = ?", (old["parent_id"],))
                others = [
                    e for e in self._rows("SELECT * FROM prop_entries WHERE parent_id = ?", (old["parent_id"],))
                    if not e["voided"] and e["id"] != rid
                ]
                require(
                    parent and not parent["voided"] and parent["occurred_on"] <= old["occurred_on"]
                    and sum(e["amount_minor"] for e in others) + old["amount_minor"] <= parent["amount_minor"],
                    "Restore the expense first and ensure refunds do not exceed its amount.",
                )
            updated = {**old, "voided": body["voided"], "revision": old["revision"] + 1, "updated_at": now_iso()}
            self._upsert("prop_entries", updated)
            self._audit("entry", rid, old, updated, text(body.get("reason"), "a reason", 500))
            return {"id": rid}

        if action in ("receipt.add", "receipt.void"):
            payout_id = id_value(body.get("payout_id"))
            payout = self._one("SELECT * FROM prop_entries WHERE id = ?", (payout_id,))
            require(payout and payout["kind"] == "payout" and not payout["voided"], "Choose an active payout record.")
            old = self._one("SELECT * FROM prop_receipts WHERE id = ?", (rid,))
            rows = self._rows("SELECT * FROM prop_receipts WHERE payout_id = ?", (payout_id,))
            if action == "receipt.add":
                require(payout["status"] not in ("rejected", "cancelled"), "Reopen the payout before recording money received or reversed.")
                updated = {
                    "id": rid, "payout_id": payout_id,
                    "kind": choice(body.get("kind"), ("receipt", "reversal"), "receipt or reversal"),
                    "amount_minor": money(body.get("amount"), payout["currency"]),
                    "occurred_on": a_date(body.get("occurred_on"), "settlement date"),
                    **details(),
                    "voided": False,
                    "created_at": old["created_at"] if old else now_iso(),
                }
                require(
                    updated["amount_minor"] > 0 and updated["occurred_on"] >= payout["occurred_on"],
                    "Enter a positive amount and a settlement date on or after the payout request.",
                )
                if old and same(old, updated):
                    return {"id": rid}
                require(not old, "Receipt ID already exists. Void an incorrect receipt and add a replacement.")
                require(self.con.execute("SELECT count(*) FROM prop_receipts").fetchone()[0] < 50_000, "Receipt limit reached (50,000).")
            else:
                require(old and old["payout_id"] == payout_id and isinstance(body.get("voided"), bool), "Choose a receipt to void or restore.")
                updated = {**old, "voided": body["voided"]}
            check_revision(payout, body.get("revision"))
            active = sorted(
                (r for r in [*(r for r in rows if r["id"] != rid), updated] if not r["voided"]),
                key=lambda r: (r["occurred_on"], 0 if r["kind"] == "receipt" else 1),
            )
            balance = 0
            for r in active:
                require(r["occurred_on"] >= payout["occurred_on"], "A receipt cannot precede its payout request.")
                balance += r["amount_minor"] * (-1 if r["kind"] == "reversal" else 1)
                require(balance >= 0, "A reversal cannot exceed the money received by that date.")
            require(payout["status"] not in ("rejected", "cancelled") or balance == 0, "Reopen the payout before restoring received money.")
            self._upsert("prop_receipts", updated)
            updated_payout = {
                **payout, "revision": payout["revision"] + 1, "updated_at": now_iso(),
                "status": "approved" if balance == 0 and payout["status"] == "completed" else payout["status"],
            }
            self._upsert("prop_entries", updated_payout)
            self._audit(
                "entry", payout_id, {"payout": payout, "receipt": old}, {"payout": updated_payout, "receipt": updated},
                f"Recorded {updated['kind']}" if action == "receipt.add" else text(body.get("reason"), "a reason", 500),
            )
            return {"id": rid}

        raise PropError("Choose a supported prop tracker action.")

    # ---- CSV -----------------------------------------------------------------
    def import_csv(self, content, preview):
        """Generic settled-cash CSV (see CSV_COLUMNS). Payout rows are actual
        cash received and are stored as a completed payout with its receipt.
        Re-importing identical rows is a no-op."""
        require(len(content.encode("utf-8")) <= 2 * 1024 * 1024, "CSV must be 2 MB or smaller.")
        require(content.count('"') % 2 == 0, "CSV has an unterminated quoted field.")
        rows = [r for r in csv.reader(io.StringIO(content.lstrip("﻿"))) if any(c.strip() for c in r)]
        require(rows, "Use the generic prop cash CSV header template.")
        header, records = [h.strip() for h in rows[0]], rows[1:]
        require(
            len(header) == len(CSV_COLUMNS) and len(set(header)) == len(header) and all(c in header for c in CSV_COLUMNS),
            "Use the generic prop cash CSV header template.",
        )
        require(0 < len(records) <= 1000, "Import 1–1,000 rows at a time.")
        parsed = []
        for i, row in enumerate(records):
            require(len(row) == len(header), f"Row {i + 2}: column count differs from the header.")
            parsed.append({c: row[header.index(c)].strip() for c in CSV_COLUMNS})
        seen = set()
        for row in parsed:
            require(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", row["id"]) and row["id"] not in seen, "Each CSV row needs a unique stable ID.")
            seen.add(row["id"])
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()

        def run():
            imported = skipped = 0
            for index, row in enumerate(parsed):
                try:
                    require(row["kind"] in ("expense", "refund", "payout"), "Kind must be expense, refund or payout (actual cash received).")
                    rid = f"csv-{digest(row['id'])}"[:100]
                    fingerprint = f"CSV import {digest(json.dumps(row, sort_keys=True))}"
                    if self._one("SELECT id FROM prop_entries WHERE id = ?", (rid,)):
                        require(
                            self._one("SELECT id FROM prop_audit WHERE entity_id = ? AND reason = ?", (rid, fingerprint)),
                            "This CSV ID was already imported with different data. Edit the existing record or use a new ID for a separate transaction.",
                        )
                        skipped += 1
                        continue
                    parent = None
                    if row["expense_id"]:
                        parent = row["expense_id"] if self._one("SELECT id FROM prop_entries WHERE id = ?", (row["expense_id"],)) else f"csv-{digest(row['expense_id'])}"[:100]
                    command = {
                        "action": "entry.save", "id": rid, "revision": 0, "kind": row["kind"],
                        "account_id": row["account_id"], "firm": row["firm"], "currency": row["currency"],
                        "occurred_on": row["date"], "amount": row["amount"], "category": row["category"],
                        "parent_id": parent, "reference": row["reference"], "notes": row["notes"],
                        "split_percent": "100", "fee": "0", "status": "requested",
                    }
                    self._mutate(command)
                    if row["kind"] == "payout":
                        self._mutate({
                            "action": "receipt.add", "id": f"{rid}-cash"[:100], "payout_id": rid, "revision": 1,
                            "kind": "receipt", "amount": row["amount"], "occurred_on": row["date"],
                            "reference": row["reference"], "notes": row["notes"],
                        })
                        self._mutate({**command, "revision": 2, "status": "completed", "reason": "Imported settled payout"})
                    self._audit("entry", rid, None, row, fingerprint)
                    imported += 1
                except PropError as e:
                    raise PropError(f"Row {index + 2}: {e}") from None
            result = {"imported": imported, "skipped": skipped, "sample": parsed[:5]}
            if preview:
                raise _Rollback(result)
            return result

        try:
            return self._transaction(run)
        except _Rollback as r:
            return r.result

    @staticmethod
    def csv_template():
        return ",".join(CSV_COLUMNS) + "\r\n" + (
            "exp-001,expense,Example Firm,,USD,2026-01-05,99.00,evaluation,,INV-1,50K evaluation fee\r\n"
        )

    @staticmethod
    def export_cash_csv(rows):
        def cell(value):
            value = "" if value is None else str(value)
            safe = f"'{value}" if re.match(r"^\s*[=+@-]", value) else value
            return '"' + safe.replace('"', '""') + '"'

        lines = [",".join(EXPORT_COLUMNS)]
        for r in rows:
            lines.append(",".join(cell(v) for v in (
                r["id"], r["entry_id"], r["account_id"] or "", r["firm"], r["currency"], r["date"],
                r["kind"], from_minor(r["amount_minor"], r["currency"]), r["category"], r["reference"],
            )))
        return "\r\n".join(lines)

    # ---- migration -------------------------------------------------------------
    def import_from_trade_journal(self, journal_db_path):
        """Copy prop accounts, entries, receipts and audit rows from a Trade
        Journal database (opened read-only). Rows already present are left
        untouched, so running it again only brings in new records."""
        src = sqlite3.connect(f"file:{journal_db_path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        tables = ("prop_accounts", "prop_entries", "prop_receipts", "prop_audit")
        try:
            data = {t: [dict(r) for r in src.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}
        finally:
            src.close()

        def run():
            counts = {}
            for table in tables:
                cols = [r[1] for r in self.con.execute(f"PRAGMA table_info({table})").fetchall()]
                added = 0
                rows = data[table]
                if table in ("prop_accounts", "prop_entries"):
                    # Parents first so self-references satisfy foreign keys.
                    rows = sorted(rows, key=lambda r: (r.get("parent_id") is not None, r.get("created_at") or ""))
                for row in rows:
                    if self._one(f"SELECT id FROM {table} WHERE id = ?", (row["id"],)):
                        continue
                    values = {c: row.get(c) for c in cols}
                    if table == "prop_accounts":
                        values["journal_account_id"] = None  # Journal accounts don't exist here.
                    self.con.execute(
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                        [values[c] for c in cols],
                    )
                    added += 1
                counts[table] = added
            return counts

        return self._transaction(run)


def add_days(day_iso, days):
    return (date.fromisoformat(day_iso) + timedelta(days=days)).isoformat()

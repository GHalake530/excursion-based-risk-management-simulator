"""Prop Firms page: the Trade Journal prop tracker (accounts, costs, payout
requests and receipts, audit history, cash CSV import/export) plus the payout
requirements calculator, rendered with the dashboard's card styling."""

import json
import os
from datetime import date

import plotly.graph_objects as go
import streamlit as st

from prop_firms import (
    ACCOUNT_SIZES,
    DEFAULT_RULES,
    EVAL_PROGRAMS,
    EXPENSE_CATEGORIES,
    FUNDED_PROGRAMS,
    OPEN_PAYOUT_STATES,
    PAYOUT_STATES,
    PROP_PROGRAMS,
    PROP_STATES,
    RULE_LABELS,
    PropError,
    PropStore,
    add_days,
    cash_movements,
    cash_summary,
    cash_timeline,
    currency_digits,
    expense_rows,
    fmt_money,
    from_minor,
    in_dates,
    label,
    new_id,
    payout_progress,
    rule_limits,
    trailing_floor,
)
from ui_kit import kpi_card, pnl_class, render_banner, render_card_grid

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".simulator_state")
PROP_DB_PATH = os.environ.get("SIM_PROP_DB") or os.path.join(STATE_DIR, "prop.db")
TRADE_JOURNAL_DB = os.path.expanduser("~/Documents/Trade_Journal/trade-journal/apps/web/data/journal.db")
PAGE_SIZE = 40
STATUS_BADGE = {
    "active": "info", "passed": "ok", "breached": "bad", "closed": "",
    "requested": "info", "approved": "warn", "completed": "ok", "rejected": "bad", "cancelled": "",
}


@st.cache_resource
def get_store():
    os.makedirs(STATE_DIR, exist_ok=True)
    return PropStore(PROP_DB_PATH)


def badge(text, kind=""):
    return f'<span class="tj-badge {kind}">{text}</span>'


def section_title(text, note=""):
    note_html = f'<span class="tj-note">{note}</span>' if note else ""
    st.markdown(f'<div class="tj-card-title"><span class="tj-label">{text}</span>{note_html}</div>', unsafe_allow_html=True)


def empty(text):
    st.markdown(f'<div class="tj-empty">{text}</div>', unsafe_allow_html=True)


def html_table(headers, rows, numeric=()):
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{h}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    st.markdown(f'<table class="tj-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>', unsafe_allow_html=True)


def md(text):
    """Escape "$" for Streamlit markdown, where a pair of them starts LaTeX."""
    return text.replace("$", "\\$")


def iso(d):
    return d.isoformat() if isinstance(d, date) else (d or "")


def parse_day(value):
    return date.fromisoformat(value) if value else None


def minor_text(value, currency):
    return "" if value is None else from_minor(value, currency)


def run_action(store, body, success=None):
    """Apply a tracker action; show the validation message inline on failure."""
    try:
        store.mutate(body)
    except PropError as e:
        st.error(str(e))
        return False
    if success:
        st.session_state["pf_flash"] = success
    st.rerun()


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------
@st.dialog("Prop account", width="large")
def account_dialog(store, data, account=None, parent=None):
    a = account or {}
    editing = bool(account)
    c1, c2 = st.columns(2)
    firm = c1.text_input("Firm", value=a.get("firm") or (parent or {}).get("firm", ""))
    name = c2.text_input("Account / attempt name", value=a.get("name", ""))
    c1, c2, c3 = st.columns(3)
    program = c1.selectbox(
        "Program", PROP_PROGRAMS, format_func=label,
        index=PROP_PROGRAMS.index(a.get("program", "funded" if parent else "evaluation")),
    )
    status = c2.selectbox("Status", PROP_STATES, format_func=label, index=PROP_STATES.index(a.get("status", "active")))
    currency = c3.text_input("Currency", value=a.get("currency") or (parent or {}).get("currency", "USD"), max_chars=3).upper()
    c1, c2, c3 = st.columns(3)
    size = c1.text_input("Account size (optional)", value=minor_text(a.get("size_minor"), a.get("currency", "USD")) if editing else minor_text((parent or {}).get("size_minor"), currency or "USD"), placeholder="e.g. 50000")
    opened_on = c2.date_input("Opened on", value=parse_day(a.get("opened_on")) or date.today(), max_value=date.today())
    closed_on = c3.date_input("Resolved on", value=parse_day(a.get("closed_on")), max_value=date.today(), help="Leave empty while active; required once passed, breached or closed.")
    c1, c2 = st.columns(2)
    renewal_on = c1.date_input("Next renewal (optional)", value=parse_day(a.get("renewal_on")))
    renewal_amount = c2.text_input("Renewal amount", value=minor_text(a.get("renewal_minor"), a.get("currency", "USD")) if editing else "")
    candidates = [
        x for x in data["accounts"]
        if x["id"] != a.get("id") and not x["archived"]
    ]
    parent_ids = [""] + [x["id"] for x in candidates]
    names = {x["id"]: f'{x["firm"]} · {x["name"]} ({label(x["program"])})' for x in candidates}
    default_parent = (parent or {}).get("id") or a.get("parent_id") or ""
    parent_id = st.selectbox(
        "Previous attempt / phase", parent_ids,
        index=parent_ids.index(default_parent) if default_parent in parent_ids else 0,
        format_func=lambda i: names.get(i, "None"),
        help="Link a funded phase to the evaluation it came from, or a reset to the attempt it replaces.",
    )
    notes = st.text_area("Notes / rules / breach reason", value=a.get("notes", ""))
    reason = st.text_input("Reason for this change") if editing else ""
    if st.button("Save account", type="primary"):
        run_action(store, {
            "action": "account.save", "id": a.get("id") or new_id(), "revision": a.get("revision", 0),
            "firm": firm, "name": name, "program": program, "status": status, "currency": currency,
            "size": size.replace(",", "").strip(), "parent_id": parent_id, "opened_on": iso(opened_on),
            "closed_on": iso(closed_on), "renewal_on": iso(renewal_on),
            "renewal_amount": renewal_amount.replace(",", "").strip(), "notes": notes, "reason": reason,
        }, "Account saved.")


@st.dialog("Cash entry", width="large")
def entry_dialog(store, data, kind, entry=None, expense=None, account_id=None, category=None):
    e = entry or {}
    editing = bool(entry)
    st.markdown(f"**{'Edit' if editing else 'New'} {'payout request' if kind == 'payout' else kind}**")
    accounts = [a for a in data["accounts"] if not a["archived"] or a["id"] == e.get("account_id")]
    if kind == "payout":
        accounts = [a for a in accounts if a["program"] in FUNDED_PROGRAMS]
    by_id = {a["id"]: a for a in accounts}
    if expense:
        acct_id = expense["account_id"] or ""
        st.caption(md(f"Refund of {expense['firm']} {label(expense['category'])} expense on {expense['occurred_on']} · {fmt_money(expense['amount_minor'], expense['currency'])}"))
    else:
        options = ([] if kind == "payout" else [""]) + list(by_id)
        default = e.get("account_id") or account_id or ""
        if not options:
            st.warning("Add a funded, instant-funded or live account before recording payouts.")
            return
        acct_id = st.selectbox(
            "Prop account", options, index=options.index(default) if default in options else 0,
            format_func=lambda i: f'{by_id[i]["firm"]} · {by_id[i]["name"]}' if i else "Shared firm cost (no account)",
            disabled=editing,
        )
    account = by_id.get(acct_id) if acct_id else None
    c1, c2 = st.columns(2)
    firm_default = (expense or {}).get("firm") or (account or {}).get("firm") or e.get("firm", "")
    currency_default = (expense or {}).get("currency") or (account or {}).get("currency") or e.get("currency") or "USD"
    firm = c1.text_input("Firm", value=firm_default, disabled=bool(account or expense or editing))
    currency = c2.text_input("Currency", value=currency_default, max_chars=3, disabled=bool(account or expense or editing)).upper()
    c1, c2 = st.columns(2)
    amount = c1.text_input(
        "Gross payout requested" if kind == "payout" else "Amount",
        value=minor_text(e.get("amount_minor"), currency_default) if editing else "",
        placeholder="e.g. 299.00",
    )
    occurred_on = c2.date_input(
        "Request date" if kind == "payout" else "Cash date",
        value=parse_day(e.get("occurred_on")) or date.today(), max_value=date.today(),
    )
    body = {}
    if kind == "expense":
        cats = list(EXPENSE_CATEGORIES)
        body["category"] = st.selectbox(
            "Expense category", cats, format_func=label,
            index=cats.index(e.get("category") or category or "evaluation"),
        )
    if kind == "payout":
        c1, c2, c3 = st.columns(3)
        body["split_percent"] = c1.text_input("Your share (%)", value=f'{e["split_bps"] / 100:g}' if editing else "80")
        body["fee"] = c2.text_input("Fees withheld", value=minor_text(e.get("fee_minor"), currency_default) if editing else "0")
        body["due_on"] = iso(c3.date_input("Expected payment date (optional)", value=parse_day(e.get("due_on"))))
        statuses = list(PAYOUT_STATES) if editing else list(OPEN_PAYOUT_STATES)
        body["status"] = st.selectbox("Payout status", statuses, format_func=label, index=statuses.index(e.get("status", "requested")))
    c1, c2 = st.columns(2)
    reference = c1.text_input("Reference", value=e.get("reference", ""))
    notes = c2.text_input("Notes", value=e.get("notes", ""))
    reason = st.text_input("Reason for this change") if editing else ""
    if st.button("Save", type="primary"):
        run_action(store, {
            "action": "entry.save", "id": e.get("id") or new_id(), "revision": e.get("revision", 0),
            "kind": kind, "account_id": acct_id or "", "firm": firm_default if (account or expense or editing) else firm,
            "currency": currency_default if (account or expense or editing) else currency,
            "amount": amount.replace(",", "").strip(), "occurred_on": iso(occurred_on),
            "parent_id": (expense or {}).get("id") or e.get("parent_id") or "",
            "reference": reference, "notes": notes, "reason": reason, **body,
        }, "Entry saved.")


@st.dialog("Record payout cash")
def receipt_dialog(store, progress):
    payout = progress["entry"]
    st.caption(md(
        f'{payout["firm"]} · requested {payout["occurred_on"]} · expected {fmt_money(progress["expected"], payout["currency"])} · '
        f'received {fmt_money(progress["actual"], payout["currency"])}'
    ))
    kind = st.radio("Movement", ["receipt", "reversal"], horizontal=True,
                    format_func=lambda k: "Money received" if k == "receipt" else "Money returned / reversed")
    c1, c2 = st.columns(2)
    amount = c1.text_input("Amount", value=from_minor(progress["remaining"], payout["currency"]) if progress["remaining"] else "")
    occurred_on = c2.date_input("Settlement date", value=date.today(), max_value=date.today())
    reference = st.text_input("Bank reference")
    notes = st.text_input("Notes")
    if st.button("Record", type="primary"):
        run_action(store, {
            "action": "receipt.add", "id": new_id(), "payout_id": payout["id"], "revision": payout["revision"],
            "kind": kind, "amount": amount.replace(",", "").strip(), "occurred_on": iso(occurred_on),
            "reference": reference, "notes": notes,
        }, f"{'Receipt' if kind == 'receipt' else 'Reversal'} recorded.")


@st.dialog("Confirm change")
def change_dialog(store, title, body):
    st.markdown(f"**{title}**")
    reason = st.text_input("Reason")
    if st.button(title, type="primary"):
        run_action(store, {**body, "reason": reason}, f"{title} done.")


@st.dialog("Record details", width="large")
def details_dialog(store, entity_type, record, extra_rows=None):
    rows = [(label(k), str(v)) for k, v in record.items() if k not in ("revision",) and v not in (None, "")]
    html_table(["Field", "Value"], rows + (extra_rows or []))
    section_title("Change history")
    history = store.history(entity_type, record["id"])
    if not history:
        empty("No history recorded.")
        return
    out = []
    for h in history:
        before = json.loads(h["before_json"]) if h["before_json"] else {}
        after = json.loads(h["after_json"])
        if "payout" in after:
            before, after = before.get("payout") or {}, after["payout"]
        changed = [k for k in after if k not in ("revision", "updated_at", "created_at") and before.get(k) != after.get(k)]
        out.append((h["created_at"][:19].replace("T", " "), h["reason"], ", ".join(label(k) for k in changed) if before else "Created"))
    html_table(["When (UTC)", "Reason", "Changed"], out)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def render():
    store = get_store()
    data = store.data()
    today = data["today"]

    st.markdown(
        '<div class="tj-page-head"><div class="tj-page-title">Prop Firms</div>'
        '<div class="tj-page-sub">Accounts, costs and payouts · real cash only, never trading P&L</div></div>',
        unsafe_allow_html=True,
    )
    flash = st.session_state.pop("pf_flash", None)
    if flash:
        st.success(flash)

    if not data["accounts"] and not data["entries"] and os.path.exists(TRADE_JOURNAL_DB):
        render_banner("Your Trade Journal has prop records. Use <b>Import from Trade Journal</b> below to bring them in.")

    b1, b2, b3, b4, _ = st.columns([1, 1, 1.2, 1.3, 2])
    if b1.button("Add account", width="stretch"):
        account_dialog(store, data)
    if b2.button("Add expense", width="stretch"):
        entry_dialog(store, data, "expense")
    if b3.button("Add payout request", width="stretch"):
        entry_dialog(store, data, "payout")
    with b4.popover("Import from Trade Journal", width="stretch"):
        path = st.text_input("Trade Journal database", value=TRADE_JOURNAL_DB)
        st.caption("Copies prop accounts, cash entries, receipts and history. Records already here are left unchanged; the journal itself is only read.")
        if st.button("Import", key="pf_tj_import"):
            try:
                counts = store.import_from_trade_journal(path)
                st.session_state["pf_flash"] = (
                    f"Imported {counts['prop_accounts']} account(s), {counts['prop_entries']} entr(ies), "
                    f"{counts['prop_receipts']} receipt(s) from Trade Journal."
                )
                st.rerun()
            except Exception as e:  # sqlite errors, missing file, validation
                st.error(f"Couldn't import: {e}")

    # ---- filters ----------------------------------------------------------------
    firms = sorted({a["firm"] for a in data["accounts"]} | {e["firm"] for e in data["entries"]})
    currencies = sorted({a["currency"] for a in data["accounts"]} | {e["currency"] for e in data["entries"]})
    account_names = {a["id"]: f'{a["firm"]} · {a["name"]}' for a in data["accounts"]}
    with st.expander("Filters"):
        c1, c2, c3, c4, c5 = st.columns(5)
        firm = c1.selectbox("Firm", [""] + firms, format_func=lambda v: v or "All firms", key="pf_firm")
        account_id = c2.selectbox("Prop account", [""] + list(account_names), format_func=lambda v: account_names.get(v, "All accounts"), key="pf_account")
        currency = c3.selectbox("Currency", [""] + currencies, format_func=lambda v: v or "All currencies", key="pf_currency")
        start = iso(c4.date_input("Cash from", value=None, key="pf_from"))
        end = iso(c5.date_input("Cash through", value=None, key="pf_to"))
        st.caption("Dates apply to cash received or paid. Account history and pending requests cover all dates.")
    if start and end and start > end:
        st.error("The start date must be on or before the end date.")

    def matches(firm_value, acct, cur):
        return (not firm or firm_value == firm) and (not account_id or acct == account_id) and (not currency or cur == currency)

    selected_currency = currency or (currencies[0] if len(currencies) == 1 else "")
    all_cash = cash_movements(data["entries"], data["receipts"])
    cash = [r for r in all_cash if matches(r["firm"], r["account_id"], r["currency"]) and in_dates(r["date"], start, end)]
    summary_cash = [r for r in cash if r["currency"] == selected_currency]
    summary = cash_summary(summary_cash)
    payouts = [p for p in payout_progress(data["entries"], data["receipts"], today) if matches(p["entry"]["firm"], p["entry"]["account_id"], p["entry"]["currency"])]
    outstanding = sum(p["remaining"] for p in payouts if p["entry"]["currency"] == selected_currency)
    overdue = [p for p in payouts if p["overdue"]]
    account_rows = [a for a in data["accounts"] if matches(a["firm"], a["id"], a["currency"])]
    active = [a for a in account_rows if a["status"] == "active" and not a["archived"]]
    money = lambda v, cur=None: fmt_money(v, cur or selected_currency)

    if len(currencies) > 1 and not selected_currency:
        per = " · ".join(f"<b>{c}</b> net {fmt_money(cash_summary([r for r in cash if r['currency'] == c])['net'], c)}" for c in currencies)
        render_banner(f"Currencies are never added together. Choose one in Filters for totals and charts. {per}")

    roi = summary["roi"]
    render_card_grid([
        kpi_card("Money spent", "wallet",
                 f'<div class="tj-value {"neg" if summary["spent"] else ""}">{money(summary["spent"])}</div>'
                 f'<div class="tj-sub">refunded {money(summary["refunds"])} · net cost {money(summary["net_spend"])}</div>'),
        kpi_card("Paid out", "dollar",
                 f'<div class="tj-value {pnl_class(summary["received"])}">{money(summary["received"])}</div>'
                 '<div class="tj-sub">money received, after any reversals</div>'),
        kpi_card("Net after costs", "trend",
                 f'<div class="tj-value {pnl_class(summary["net"])}">{fmt_money(summary["net"], selected_currency, signed=True)}</div>'
                 f'<div class="tj-sub">return on net cost {"—" if roi is None or not selected_currency else f"{roi * 100:.1f}%"}</div>'),
        kpi_card("Awaiting payout", "clock",
                 f'<div class="tj-value">{money(outstanding)}</div>'
                 f'<div class="tj-sub">{f"{len(overdue)} overdue · " if overdue else ""}all dates · not in received cash</div>'),
        kpi_card("Active accounts", "users",
                 f'<div class="tj-value">{len(active)}</div>'
                 f'<div class="tj-sub">{sum(a["program"] in FUNDED_PROGRAMS for a in active)} funded / live · '
                 f'{sum(a["program"] not in FUNDED_PROGRAMS for a in active)} in evaluation</div>'),
    ])

    tab_overview, tab_accounts, tab_payouts, tab_ledger, tab_rules = st.tabs(
        ["Overview", "Accounts", "Payouts", "Transactions", "Payout Rules"]
    )
    with tab_overview:
        _overview(store, data, account_rows, active, summary, summary_cash, selected_currency, money, today)
    with tab_accounts:
        _accounts(store, data, account_rows, cash, today)
    with tab_payouts:
        _payouts(store, data, payouts, start, end, account_names)
    with tab_ledger:
        _ledger(store, data, cash, start, end, matches, account_names)
    with tab_rules:
        render_rules_calculator(store, data)


def _overview(store, data, account_rows, active, summary, summary_cash, selected_currency, money, today):
    months = sorted({r["date"][:7] for r in summary_cash}, reverse=True)
    monthly = [{"month": m, **cash_summary([r for r in summary_cash if r["date"].startswith(m)])} for m in months]
    digits = 10 ** currency_digits(selected_currency) if selected_currency else 1

    with st.container(border=True, key="tjc-pf-monthly"):
        section_title("Spending vs payouts", f"monthly cash flow{f' · {selected_currency}' if selected_currency else ''}")
        if not selected_currency:
            empty("Choose a currency in Filters to compare spending and payouts.")
        elif not monthly:
            empty("Record an expense or a payout receipt to start your comparison.")
        else:
            rows = list(reversed(monthly))
            fig = go.Figure([
                go.Bar(x=[r["month"] for r in rows], y=[r["spent"] / digits for r in rows], name="Money spent", marker_color="#ef4444"),
                go.Bar(x=[r["month"] for r in rows], y=[r["received"] / digits for r in rows], name="Payouts received", marker_color="#1d9bf0"),
            ])
            _style(fig, 280)
            fig.update_layout(barmode="group", legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1.0))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    c1, c2 = st.columns([2, 1])
    with c1:
        with st.container(border=True, key="tjc-pf-net"):
            section_title(f"Net cash over time{f' · {selected_currency}' if selected_currency else ''}", "payouts + refunds − expenses − reversals")
            if not selected_currency:
                empty("Choose a currency to plot cash returns.")
            elif not summary_cash:
                empty("Record spending or a payout receipt to build your cash history.")
            else:
                tl = cash_timeline(summary_cash)
                fig = go.Figure(go.Scatter(
                    x=[p["date"] for p in tl], y=[p["net"] / digits for p in tl], mode="lines", line_shape="hv",
                    line=dict(color="#1d9bf0", width=2), fill="tozeroy", fillcolor="rgba(29,155,240,0.10)", name="Net cash",
                ))
                _style(fig, 240)
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    with c2:
        with st.container(border=True, key="tjc-pf-progress"):
            section_title("Account progress", "all dates")
            resolved = [a for a in account_rows if a["program"] in EVAL_PROGRAMS and a["status"] in ("passed", "breached")]
            passed = sum(a["status"] == "passed" for a in resolved)
            st.markdown(
                f'<div class="tj-rule"><span>Active evaluations (1-step, 2-step, verification)</span><b>{sum(a["program"] in EVAL_PROGRAMS for a in active)}</b></div>'
                f'<div class="tj-rule"><span>Active funded / live</span><b>{sum(a["program"] in FUNDED_PROGRAMS for a in active)}</b></div>'
                f'<div class="tj-rule"><span>Resolved-phase pass rate<small>{passed} passed / {len(resolved)} passed or breached evaluation phases</small></span>'
                f'<b>{f"{round(passed / len(resolved) * 100)}%" if resolved else "—"}</b></div>',
                unsafe_allow_html=True,
            )
            st.caption("Active and voluntarily closed phases are excluded; this is not a whole-challenge success rate. Account sizes are descriptive and never counted as cash invested.")

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True, key="tjc-pf-firms"):
            section_title(f"Returns by firm{f' · {selected_currency}' if selected_currency else ''}")
            by_firm = sorted(
                ({"firm": f, **cash_summary([r for r in summary_cash if r["firm"] == f])} for f in {r["firm"] for r in summary_cash}),
                key=lambda r: -r["net"],
            )
            if by_firm:
                html_table(
                    ["Firm", "Net spend", "Payout cash", "Net return"],
                    [(r["firm"], money(r["net_spend"]), money(r["received"]), f'<span class="{pnl_class(r["net"])}">{fmt_money(r["net"], selected_currency, True)}</span>') for r in by_firm],
                    numeric=(1, 2, 3),
                )
            else:
                empty("No cash movements in this selection.")
    with c2:
        with st.container(border=True, key="tjc-pf-spend"):
            section_title("Spending breakdown", "gross, before refunds")
            cats = expense_rows(summary_cash)
            if cats:
                st.markdown("".join(
                    f'<div style="margin-bottom:10px"><div class="tj-rule" style="border:none;padding:0"><span>{label(c["category"])}</span><b>{money(c["amount"])}</b></div>'
                    f'<div class="tj-bar"><div style="width:{c["amount"] / summary["spent"] * 100 if summary["spent"] else 0:.1f}%"></div></div></div>'
                    for c in cats
                ), unsafe_allow_html=True)
            else:
                empty("No recorded expenses in this selection.")

    with st.container(border=True, key="tjc-pf-renewals"):
        section_title("Upcoming renewals", "next 30 days & overdue")
        horizon = add_days(today, 30)
        renewals = sorted((a for a in active if a["renewal_on"] and a["renewal_on"] <= horizon), key=lambda a: a["renewal_on"])
        if not renewals:
            empty("No renewals scheduled.")
        for a in renewals:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            late = " · " + badge("Review overdue renewal", "bad") if a["renewal_on"] < today else ""
            c1.markdown(
                f'<b>{a["firm"]} · {a["name"]}</b><div class="tj-sub" style="margin-top:2px">{a["renewal_on"]} · '
                f'{"Amount not set" if a["renewal_minor"] is None else fmt_money(a["renewal_minor"], a["currency"])}{late}</div>',
                unsafe_allow_html=True,
            )
            if c2.button("Record charge", key=f"pf_renew_{a['id']}"):
                entry_dialog(store, data, "expense", account_id=a["id"], category="subscription")
        st.caption("Reminders don't create expenses or charge your card. Update the next date after reviewing a renewal.")

    with st.container(border=True, key="tjc-pf-months"):
        section_title("Monthly cash review")
        if monthly:
            html_table(
                ["Month", "Spent", "Refunded", "Payout cash", "Net"],
                [(m["month"], money(m["spent"]), money(m["refunds"]), money(m["received"]),
                  f'<span class="{pnl_class(m["net"])}">{fmt_money(m["net"], selected_currency, True)}</span>') for m in monthly],
                numeric=(1, 2, 3, 4),
            )
        else:
            empty("No settled cash yet.")


def _accounts(store, data, account_rows, cash, today):
    c1, c2 = st.columns([3, 1])
    query = c1.text_input("Search accounts", key="pf_acct_search", placeholder="Firm, name, program or status").lower().strip()
    show_archived = c2.toggle("Show archived", key="pf_show_archived")
    rows = [
        a for a in account_rows
        if (show_archived or not a["archived"])
        and (not query or query in f'{a["firm"]} {a["name"]} {a["program"]} {a["status"]}'.lower())
    ]
    rows.sort(key=lambda a: (a["archived"], a["status"] != "active", a["firm"].lower(), a["opened_on"]))
    names = {a["id"]: a["name"] for a in data["accounts"]}
    if not rows:
        empty("No prop accounts yet. Add an evaluation, funded account or reset to start tracking.")
    for a in _page(rows, "pf_acct_page"):
        acct_cash = [r for r in cash if r["account_id"] == a["id"] and r["currency"] == a["currency"]]
        s = cash_summary(acct_cash)
        with st.container(border=True, key=f"tjc-pf-acct-{a['id']}"):
            c1, c2, c3, c4 = st.columns([3.2, 2.2, 2.2, 1], vertical_alignment="center")
            tags = badge(label(a["program"]), "info") + " " + badge(label(a["status"]), STATUS_BADGE.get(a["status"], ""))
            if a["archived"]:
                tags += " " + badge("Archived")
            lineage = f' · after {names.get(a["parent_id"], "earlier phase")}' if a["parent_id"] else ""
            resolved = f' · resolved {a["closed_on"]}' if a["closed_on"] else ""
            c1.markdown(
                f'<b>{a["firm"]} · {a["name"]}</b><div style="margin-top:4px">{tags}</div>'
                f'<div class="tj-sub" style="margin-top:4px">Opened {a["opened_on"]}{resolved}{lineage}</div>',
                unsafe_allow_html=True,
            )
            renewal = ""
            if a["renewal_on"]:
                renewal = f'<div class="tj-sub {"neg" if a["renewal_on"] < today else ""}">renews {a["renewal_on"]} · {fmt_money(a["renewal_minor"], a["currency"])}</div>'
            size_text = fmt_money(a["size_minor"], a["currency"]) if a["size_minor"] is not None else "—"
            c2.markdown(
                f'<div class="tj-label">Account size</div><div style="font-weight:600;margin-top:2px">{size_text}</div>{renewal}',
                unsafe_allow_html=True,
            )
            c3.markdown(
                f'<div class="tj-label">Net cash</div><div class="{pnl_class(s["net"])}" style="font-weight:600;margin-top:2px">{fmt_money(s["net"], a["currency"], True)}</div>'
                f'<div class="tj-sub">spent {fmt_money(s["net_spend"], a["currency"])} · paid out {fmt_money(s["received"], a["currency"])}</div>',
                unsafe_allow_html=True,
            )
            with c4.popover("Actions", width="stretch"):
                if st.button("Edit", key=f"pf_ae_{a['id']}", width="stretch"):
                    account_dialog(store, data, account=a)
                if st.button("Add linked phase / reset", key=f"pf_ap_{a['id']}", width="stretch"):
                    account_dialog(store, data, parent=a)
                if st.button("Record expense", key=f"pf_ax_{a['id']}", width="stretch"):
                    entry_dialog(store, data, "expense", account_id=a["id"])
                if a["program"] in FUNDED_PROGRAMS and st.button("Request payout", key=f"pf_ay_{a['id']}", width="stretch"):
                    entry_dialog(store, data, "payout", account_id=a["id"])
                if st.button("Restore" if a["archived"] else "Archive", key=f"pf_aa_{a['id']}", width="stretch"):
                    change_dialog(store, "Restore account" if a["archived"] else "Archive account", {
                        "action": "account.archive", "id": a["id"], "revision": a["revision"], "archived": not a["archived"],
                    })
                if st.button("Details & history", key=f"pf_ah_{a['id']}", width="stretch"):
                    details_dialog(store, "account", a)
            if a["notes"]:
                st.caption(md(a["notes"]))


def _payouts(store, data, payouts, start, end, account_names):
    query = st.text_input("Search payouts", key="pf_pay_search", placeholder="Firm, account, status or reference").lower().strip()
    rows = [
        p for p in payouts
        if (p["entry"]["status"] in OPEN_PAYOUT_STATES or in_dates(p["entry"]["occurred_on"], start, end))
        and (not query or query in f'{p["entry"]["firm"]} {account_names.get(p["entry"]["account_id"], "")} {p["entry"]["status"]} {p["entry"]["reference"]}'.lower())
    ]
    # Overdue first, then newest requests.
    rows.sort(key=lambda p: p["entry"]["occurred_on"], reverse=True)
    rows.sort(key=lambda p: not p["overdue"])
    receipts_by = {}
    for r in data["receipts"]:
        receipts_by.setdefault(r["payout_id"], []).append(r)
    if not rows:
        empty("No payout requests. Add one from a funded account, then record the money when it arrives.")
    for p in _page(rows, "pf_pay_page"):
        e, cur = p["entry"], p["entry"]["currency"]
        with st.container(border=True, key=f"tjc-pf-pay-{e['id']}"):
            c1, c2, c3, c4, c5 = st.columns([3, 1.6, 1.6, 1.6, 1], vertical_alignment="center")
            flags = badge(label(e["status"]), STATUS_BADGE.get(e["status"], ""))
            if p["overdue"]:
                flags += " " + badge("Overdue", "bad")
            elif p["partial"]:
                flags += " " + badge("Partially received", "warn")
            due = f' · expected by {e["due_on"]}' if e["due_on"] else ""
            fees = f' − fees {fmt_money(e["fee_minor"], cur)}' if e["fee_minor"] else ""
            c1.markdown(
                f'<b>{account_names.get(e["account_id"], e["firm"])}</b><div style="margin-top:4px">{flags}</div>'
                f'<div class="tj-sub" style="margin-top:4px">Requested {e["occurred_on"]}{due}'
                f' · gross {fmt_money(e["amount_minor"], cur)} × {e["split_bps"] / 100:g}%{fees}</div>',
                unsafe_allow_html=True,
            )
            c2.markdown(f'<div class="tj-label">Expected net</div><div style="font-weight:600">{fmt_money(p["expected"], cur)}</div>', unsafe_allow_html=True)
            c3.markdown(f'<div class="tj-label">Received</div><div class="pos" style="font-weight:600">{fmt_money(p["actual"], cur)}</div>', unsafe_allow_html=True)
            c4.markdown(f'<div class="tj-label">Remaining</div><div style="font-weight:600">{fmt_money(p["remaining"], cur)}</div>', unsafe_allow_html=True)
            with c5.popover("Actions", width="stretch"):
                if e["status"] not in ("rejected", "cancelled") and st.button("Record cash", key=f"pf_pr_{e['id']}", width="stretch"):
                    receipt_dialog(store, p)
                if st.button("Edit / change status", key=f"pf_pe_{e['id']}", width="stretch"):
                    entry_dialog(store, data, "payout", entry=e)
                if st.button("Void", key=f"pf_pv_{e['id']}", width="stretch"):
                    change_dialog(store, "Void entry", {"action": "entry.void", "id": e["id"], "revision": e["revision"], "voided": True})
                if st.button("Details & history", key=f"pf_ph_{e['id']}", width="stretch"):
                    details_dialog(store, "entry", e)
            for r in sorted(receipts_by.get(e["id"], []), key=lambda r: r["occurred_on"]):
                rc1, rc2 = st.columns([6, 1], vertical_alignment="center")
                sign = -1 if r["kind"] == "reversal" else 1
                ref = f' · {r["reference"]}' if r["reference"] else ""
                voided = " · " + badge("Voided") if r["voided"] else ""
                rc1.markdown(
                    f'<div class="tj-sub" style="margin:0">{r["occurred_on"]} · {"Money received" if r["kind"] == "receipt" else "Reversal"} '
                    f'<span class="{pnl_class(sign)}">{fmt_money(sign * r["amount_minor"], cur, True)}</span>{ref}{voided}</div>',
                    unsafe_allow_html=True,
                )
                if rc2.button("Restore" if r["voided"] else "Void", key=f"pf_rv_{r['id']}"):
                    change_dialog(store, "Restore receipt" if r["voided"] else "Void receipt", {
                        "action": "receipt.void", "id": r["id"], "payout_id": e["id"], "revision": e["revision"], "voided": not r["voided"],
                    })


def _ledger(store, data, cash, start, end, matches, account_names):
    c1, c2 = st.columns([3, 1])
    query = c1.text_input("Search transactions", key="pf_led_search", placeholder="Firm, account, type, category or reference").lower().strip()
    show_voided = c2.toggle("Show voided", key="pf_show_voided")
    entries = sorted(
        (
            e for e in data["entries"]
            if matches(e["firm"], e["account_id"], e["currency"]) and (show_voided or not e["voided"])
            and in_dates(e["occurred_on"], start, end)
            and (not query or query in f'{e["firm"]} {account_names.get(e["account_id"], "shared firm cost")} {e["kind"]} {e["category"]} {e["reference"]}'.lower())
        ),
        key=lambda e: (e["occurred_on"], e["created_at"]), reverse=True,
    )
    if not entries:
        empty("No transactions in this selection.")
    for e in _page(entries, "pf_led_page"):
        cur = e["currency"]
        sign = -1 if e["kind"] == "expense" else 1
        with st.container(border=True, key=f"tjc-pf-led-{e['id']}"):
            c1, c2, c3 = st.columns([4.5, 1.8, 1], vertical_alignment="center")
            kind_badge = badge(label(e["kind"]), {"expense": "bad", "refund": "ok", "payout": "info"}[e["kind"]])
            extra = f' · {label(e["category"])}' if e["kind"] == "expense" else (f' · {label(e["status"])}' if e["kind"] == "payout" else "")
            ref = f' · {e["reference"]}' if e["reference"] else ""
            note = f' · {e["notes"]}' if e["notes"] else ""
            c1.markdown(
                f'{kind_badge} {badge("Voided") if e["voided"] else ""} <b style="margin-left:4px">{account_names.get(e["account_id"], f"{e['firm']} · Shared firm cost")}</b>'
                f'<div class="tj-sub" style="margin-top:4px">{e["occurred_on"]}{extra}{ref}{note}</div>',
                unsafe_allow_html=True,
            )
            if e["kind"] == "payout":
                amount_html = f'<div style="font-weight:600;text-align:right">{fmt_money(e["amount_minor"], cur)} requested</div>'
            else:
                amount_html = f'<div class="{pnl_class(sign)}" style="font-weight:600;text-align:right">{fmt_money(sign * e["amount_minor"], cur, True)}</div>'
            c2.markdown(amount_html, unsafe_allow_html=True)
            with c3.popover("Actions", width="stretch"):
                if st.button("Details & history", key=f"pf_ld_{e['id']}", width="stretch"):
                    details_dialog(store, "entry", e)
                if not e["voided"] and st.button("Edit", key=f"pf_le_{e['id']}", width="stretch"):
                    entry_dialog(store, data, e["kind"], entry=e)
                if not e["voided"] and e["kind"] == "expense" and st.button("Refund", key=f"pf_lr_{e['id']}", width="stretch"):
                    entry_dialog(store, data, "refund", expense=e)
                if st.button("Restore" if e["voided"] else "Void", key=f"pf_lv_{e['id']}", width="stretch"):
                    change_dialog(store, "Restore entry" if e["voided"] else "Void entry", {
                        "action": "entry.void", "id": e["id"], "revision": e["revision"], "voided": not e["voided"],
                    })

    with st.container(border=True, key="tjc-pf-csv"):
        section_title("Import / export cash", "generic settled-cash CSV")
        c1, c2 = st.columns(2)
        c1.download_button("Download cash CSV (current filters)", PropStore.export_cash_csv(cash), "prop_cash.csv", "text/csv", width="stretch")
        c2.download_button("Download import template", PropStore.csv_template(), "prop_cash_template.csv", "text/csv", width="stretch")
        upload = st.file_uploader(
            "Import cash CSV", type=["csv"], key="pf_csv_upload",
            help="One row per settled transaction. kind = expense, refund or payout (money actually received). Up to 1,000 rows; re-importing the same rows is skipped.",
        )
        if upload is not None:
            content = upload.getvalue().decode("utf-8-sig", errors="replace")
            b1, b2 = st.columns(2)
            if b1.button("Preview import", width="stretch"):
                try:
                    res = store.import_csv(content, preview=True)
                    st.info(f"Would import {res['imported']} row(s), skip {res['skipped']} already imported. Nothing has been saved yet.")
                    if res["sample"]:
                        html_table(list(res["sample"][0].keys()), [tuple(r.values()) for r in res["sample"]])
                except PropError as e:
                    st.error(str(e))
            if b2.button("Import", type="primary", width="stretch"):
                try:
                    res = store.import_csv(content, preview=False)
                    st.session_state["pf_flash"] = f"Imported {res['imported']} row(s); skipped {res['skipped']} already imported."
                    st.rerun()
                except PropError as e:
                    st.error(str(e))


def _page(rows, key):
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    if pages == 1:
        return rows
    page = st.number_input(f"Page (of {pages})", min_value=1, max_value=pages, step=1, key=key)
    return rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]


def _style(fig, height):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#71717a", size=11),
        margin=dict(t=8, b=8, l=8, r=8), height=height, hovermode="x unified",
        hoverlabel=dict(bgcolor="#18181b", bordercolor="#27272a", font=dict(color="#fafafa")),
    )
    fig.update_xaxes(showgrid=False, linecolor="#1f1f23")
    fig.update_yaxes(gridcolor="#1f1f23", zerolinecolor="#3f3f46", tickformat="~s", tickprefix="$")


# --------------------------------------------------------------------------
# Payout requirements calculator (from "Prop Firm Payout Requirements")
# --------------------------------------------------------------------------
def account_size_options(data):
    """(label, size in dollars) for active prop accounts that have a size."""
    rows = [a for a in data["accounts"] if a["status"] == "active" and not a["archived"] and a["size_minor"]]
    rows.sort(key=lambda a: (a["program"] not in FUNDED_PROGRAMS, a["opened_on"]))
    return [
        (f'{a["firm"]} · {a["name"]} ({fmt_money(a["size_minor"], a["currency"])})', a["size_minor"] / 10 ** currency_digits(a["currency"]))
        for a in rows
    ]


def render_rules_calculator(store, data):
    rules = store.rules()
    options = account_size_options(data)
    st.session_state.setdefault("pf_rule_size", options[0][1] if options else 50000.0)

    def pick_size():
        choice = st.session_state.get("pf_rule_chip")
        if choice:
            st.session_state["pf_rule_size"] = float(choice)

    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    c1.segmented_control(
        "Account size", ACCOUNT_SIZES, format_func=lambda s: f"{s // 1000}K", key="pf_rule_chip", on_change=pick_size,
    )
    size = c2.number_input("Custom size ($)", min_value=0.0, step=1000.0, key="pf_rule_size")
    lim = rule_limits(size, rules)
    usd = lambda v: f"${v:,.2f}".replace(".00", "")

    c1, c2, c3 = st.columns(3)
    with c1:
        with st.container(border=True, key="tjc-rules-payout"):
            section_title("Before a payout you need")
            st.markdown(
                f'<div class="tj-rule"><span>Qualifying trading days<small>days with realized profit of at least {rules["qualifying_day_pct"]:g}%</small></span><b>{lim["qualifying_days"]}</b></div>'
                f'<div class="tj-rule"><span>Min profit per qualifying day<small>{rules["qualifying_day_pct"]:g}% of account, closed trades only</small></span><b>{usd(lim["per_day"])}</b></div>'
                f'<div class="tj-rule"><span>Min total profit before payout<small>{rules["min_total_profit_pct"]:g}% of account</small></span><b>{usd(lim["total"])}</b></div>'
                f'<div class="tj-rule"><span>Best Day Rule<small>best day can\'t exceed {rules["best_day_pct"]:g}% of the withdrawal</small></span><b>{rules["best_day_pct"]:g}%</b></div>',
                unsafe_allow_html=True,
            )
    with c2:
        with st.container(border=True, key="tjc-rules-risk"):
            section_title("Risk limits")
            st.markdown(
                f'<div class="tj-rule"><span>Daily drawdown ({rules["daily_drawdown_pct"]:g}%)<small>max loss in one trading day</small></span><b class="neg">{usd(lim["daily_dd"])}</b></div>'
                f'<div class="tj-rule"><span>Max drawdown ({rules["max_drawdown_pct"]:g}%, dynamic)<small>trails your highest equity</small></span><b class="neg">{usd(lim["max_dd"])}</b></div>'
                f'<div class="tj-rule"><span>Starting equity floor</span><b>{usd(lim["floor"])}</b></div>'
                f'<div class="tj-rule"><span>Max loss on one trade ({rules["max_trade_loss_pct"]:g}%)<small>same instrument and direction combined</small></span><b class="neg">{usd(lim["trade_loss"])}</b></div>',
                unsafe_allow_html=True,
            )
            hwm = st.number_input("Highest equity reached (optional)", min_value=0.0, step=500.0, value=None, key="pf_rule_hwm", placeholder="e.g. 52000")
            st.markdown(
                f'<div class="tj-rule"><span>Your current equity floor</span><b>{usd(trailing_floor(size, hwm, rules)) if hwm else "—"}</b></div>',
                unsafe_allow_html=True,
            )
    with c3:
        with st.container(border=True, key="tjc-rules-bestday"):
            section_title("Best Day Rule checker")
            best = st.number_input("Your best day profit ($)", min_value=0.0, step=50.0, value=None, key="pf_rule_best", placeholder="e.g. 400")
            wd = st.number_input("Amount you want to withdraw ($)", min_value=0.0, step=100.0, value=None, key="pf_rule_wd", placeholder="e.g. 1000")
            share = best / wd * 100 if best is not None and wd else None
            factor = 100 / rules["best_day_pct"] if rules["best_day_pct"] else 0
            st.markdown(
                f'<div class="tj-rule"><span>Best day as % of withdrawal</span><b>{f"{share:.1f}%" if share is not None else "—"}</b></div>'
                f'<div class="tj-rule"><span>Smallest withdrawal that passes<small>best day × {factor:g}</small></span><b>{usd(best * factor) if best else "—"}</b></div>',
                unsafe_allow_html=True,
            )
            if share is not None:
                ok = share <= rules["best_day_pct"]
                st.markdown(f'<div class="tj-verdict {"pos" if ok else "warn"}">{"✓ Passes" if ok else "Payout may be delayed"}</div>', unsafe_allow_html=True)
            st.caption("Failing the Best Day Rule usually only delays the payout until other days build more profit; it does not breach the account.")

    with st.expander("Edit rule percentages"):
        st.caption(
            "Defaults are the Thunderbolt Turbo rules. Confirm them for your account size in your firm's Help Center, "
            "since limits can differ between programs. These rules also drive the Payout Eligibility check on the Simulator dashboard."
        )
        cols = st.columns(4)
        edited = {}
        for i, k in enumerate(DEFAULT_RULES):
            edited[k] = cols[i % 4].number_input(
                RULE_LABELS[k], min_value=0.0, value=float(rules[k]), step=1.0 if k == "qualifying_days" else 0.1,
                format="%.0f" if k == "qualifying_days" else "%.2f", key=f"pf_rule_edit_{k}",
            )
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Save rules", type="primary"):
            edited["qualifying_days"] = int(edited["qualifying_days"])
            store.save_rules(edited)
            st.session_state["pf_flash"] = "Payout rules saved."
            st.rerun()
        if b2.button("Reset to defaults"):
            store.save_rules(dict(DEFAULT_RULES))
            for k in DEFAULT_RULES:
                st.session_state.pop(f"pf_rule_edit_{k}", None)
            st.session_state["pf_flash"] = "Payout rules reset to defaults."
            st.rerun()

"""
Excursion-Based Risk Management Simulator
------------------------------------------
A Streamlit app that takes a TradingView "List of Trades" CSV export and
re-simulates each trade's PnL under a configurable partial-take-profit /
stop-loss-guard strategy, driven by the trade's recorded favorable excursion.
Includes optional daily profit/loss caps (applied on top of the guard
simulation), date range filtering, and monthly/weekly/daily aggregations.

The guard is checked FIRST, on the raw trade: if a trade's favorable
excursion reaches the trigger threshold, the guard has already moved the
stop (or banked a partial) at that point, so the trade's real worst-case
outcome from then on is bounded by the guard's own math - it can never
actually reach its raw adverse (or favorable) excursion in a world where
the guard intervened. Per-trade caps represent the ORIGINAL, un-adjusted
stop/target, so they only get to act on trades the guard never touched at
all. If the guard never triggers, the per-trade caps are checked against
the raw excursion data exactly as they would have fired live.

Per-trade resolution happens first for every trade; daily caps are then
applied on top of that already-resolved result, in chronological order -
so the real sequence is guard -> per-trade cap -> daily cap, matching how
these three mechanisms would actually interact live.
"""

import calendar as cal_module
import io
import json
import os
from datetime import time as dt_time, timedelta
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# --------------------------------------------------------------------------
# Page config
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="Excursion-Based Risk Management Simulator",
    layout="wide",
)

# --------------------------------------------------------------------------
# Dark, card-based dashboard styling (Trade Journal-style KPI cards/calendar)
# --------------------------------------------------------------------------
st.html(
    """
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
    html, body, .stApp, [class^="st-"]:not([data-testid="stIconMaterial"]), [class*=" st-"]:not([data-testid="stIconMaterial"]) {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
    }
    .stApp { background-color: #09090b; }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stAppDeployButton"] { display: none; }
    .block-container { padding-top: 2.2rem; max-width: 1680px; }
    section[data-testid="stSidebar"] { background: #09090b; border-right: 1px solid #1f1f23; }

    /* Bordered Streamlit containers used as cards (keyed "tjc-...") */
    [class*="st-key-tjc-"] {
        background: #111113 !important;
        border: 1px solid #1f1f23 !important;
        border-radius: 12px !important;
        padding: 16px 18px !important;
    }

    .tj-page-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
    .tj-page-title { font-size: 22px; font-weight: 600; color: #fafafa; }
    .tj-page-sub { font-size: 13px; color: #71717a; }

    .tj-grid {
        display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 12px; margin-bottom: 12px;
    }
    @media (max-width: 1250px) { .tj-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
    @media (max-width: 800px) { .tj-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 520px) { .tj-grid { grid-template-columns: minmax(0, 1fr); } }
    .tj-card {
        background: #111113; border: 1px solid #1f1f23; border-radius: 12px;
        padding: 16px 18px; min-height: 132px; display: flex; flex-direction: column; min-width: 0;
    }
    .tj-card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 12px; }
    .tj-card-head svg { width: 16px; height: 16px; stroke: #71717a; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }
    .tj-card-body { flex: 1; display: flex; flex-direction: column; justify-content: flex-start; min-width: 0; }
    .tj-card-head .tj-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }

    .tj-label {
        font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
        color: #a1a1aa; font-weight: 500;
    }
    .tj-value {
        font-size: 24px; font-weight: 600; color: #fafafa; line-height: 1.2;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -0.01em;
    }
    .pos { color: #22c55e !important; }
    .neg { color: #ef4444 !important; }
    .tj-sub { font-size: 12px; color: #71717a; margin-top: 6px; line-height: 1.5; }
    .tj-pill {
        display: inline-block; font-size: 11px; font-weight: 500; border-radius: 6px;
        padding: 2px 7px; margin-top: 6px; width: fit-content;
    }
    .tj-pill.pos { background: rgba(34,197,94,0.12); }
    .tj-pill.neg { background: rgba(239,68,68,0.12); }

    .tj-gauge-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .tj-gauge { position: relative; width: 96px; height: 56px; flex: none; }
    .tj-gauge svg { width: 96px; height: 56px; }
    .tj-gauge span { position: absolute; left: 0; right: 0; bottom: 0; text-align: center; font-size: 17px; font-weight: 600; color: #fafafa; }
    .tj-gauge-side { font-size: 12px; color: #a1a1aa; text-align: right; line-height: 1.6; white-space: nowrap; }
    .tj-ratio-bar { display: flex; gap: 3px; height: 5px; margin-top: 10px; }
    .tj-ratio-bar div { border-radius: 3px; }

    .tj-banner {
        font-size: 13px; color: #a1a1aa; background: #111113;
        border: 1px solid #1f1f23; border-left: 3px solid #3b82f6;
        border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;
    }
    .tj-card-title { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
    .tj-card-title .tj-note { font-size: 11px; color: #71717a; }

    .tj-cal-head { font-size: 12px; color: #a1a1aa; padding: 0 4px 4px; }
    .tj-cal-head.week { text-align: right; }
    .tj-cal-week { min-height: 76px; display: flex; flex-direction: column; justify-content: center; align-items: flex-end; font-size: 12px; font-weight: 600; }
    .tj-cal-week .tj-sub { margin-top: 2px; font-weight: 400; }
    .tj-cal-foot { display: flex; justify-content: space-between; font-size: 13px; color: #a1a1aa; margin-top: 10px; }
    .tj-cal-foot b { font-weight: 600; }
    [class*="st-key-calday_"] button {
        min-height: 76px; width: 100%; border-radius: 8px; padding: 6px 8px;
        background: #0d0d0f; border: 1px solid #1f1f23;
        justify-content: flex-start; align-items: flex-start; text-align: left;
    }
    [class*="st-key-calday_"] button > div { width: 100%; justify-content: flex-start; }
    [class*="st-key-calday_"] button p { font-size: 12px; line-height: 1.45; text-align: left; white-space: normal; overflow: visible; }
    [class*="st-key-calday_"] button p strong { font-size: 13px; color: #fafafa; }

    .tj-activity { max-height: 640px; overflow-y: auto; }
    .tj-act-row { display: flex; align-items: center; gap: 10px; padding: 7px 2px; border-bottom: 1px solid #18181b; font-size: 13px; }
    .tj-act-row:last-child { border-bottom: none; }
    .tj-act-tag { font-size: 11px; font-weight: 600; border-radius: 5px; padding: 2px 0; width: 48px; text-align: center; white-space: nowrap; flex: none; }
    .tj-act-tag.pos { background: rgba(34,197,94,0.12); }
    .tj-act-tag.neg { background: rgba(239,68,68,0.12); }
    .tj-act-tag.flat { background: #27272a; color: #a1a1aa; }
    .tj-act-main { flex: 1; color: #e4e4e7; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tj-act-time { color: #71717a; font-size: 12px; }
    .tj-act-pnl { font-weight: 600; white-space: nowrap; }

    section[data-testid="stFileUploaderDropzone"] {
        background: #111113 !important;
        border: 1px solid #1f1f23 !important;
        border-radius: 12px !important;
    }
    </style>
    """
)

# Minimal line icons for card headers (24x24 viewBox, stroked).
ICONS = {
    "dollar": '<circle cx="12" cy="12" r="9"/><path d="M15 9.5c0-1.4-1.3-2.5-3-2.5s-3 1-3 2.3c0 3.2 6 1.8 6 5 0 1.4-1.3 2.7-3 2.7s-3-1.1-3-2.5M12 5.5v13"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "scale": '<path d="M12 4v16M7 20h10M5 8h14M5 8l-3 6a3 3 0 0 0 6 0zM19 8l-3 6a3 3 0 0 0 6 0z"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "arrows": '<path d="M8 4v16M4 8l4-4 4 4M16 20V4M12 16l4 4 4-4"/>',
    "list": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "trend": '<path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-6"/>',
    "down": '<path d="M3 7l6 6 4-4 8 8M21 11v6h-6"/>',
    "pulse": '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
}


def money(v, signed=False):
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${abs(v):,.2f}"


def pnl_class(v):
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def kpi_card(label, icon, body):
    svg = f'<svg viewBox="0 0 24 24">{ICONS[icon]}</svg>' if icon else ""
    return (
        f'<div class="tj-card"><div class="tj-card-head"><span class="tj-label" title="{label}">{label}</span>{svg}</div>'
        f'<div class="tj-card-body">{body}</div></div>'
    )


def gauge(pct, side_html):
    pct = max(0.0, min(100.0, pct))
    arc = "M10 50 A38 38 0 0 1 86 50"
    return (
        '<div class="tj-gauge-row"><div class="tj-gauge"><svg viewBox="0 0 96 56">'
        f'<path d="{arc}" fill="none" stroke="#27272a" stroke-width="9" stroke-linecap="round" pathLength="100"/>'
        f'<path d="{arc}" fill="none" stroke="#1d9bf0" stroke-width="9" stroke-linecap="round" pathLength="100" stroke-dasharray="{pct:.1f} 100"/>'
        f'</svg><span>{pct:.1f}%</span></div><div class="tj-gauge-side">{side_html}</div></div>'
    )


def render_card_grid(cards):
    st.markdown(f'<div class="tj-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_banner(text):
    st.markdown(f'<div class="tj-banner">{text}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Persistence: remember the last uploaded CSV and every sidebar setting
# across a browser refresh (a refresh starts a brand-new Streamlit session,
# which would otherwise wipe st.session_state).
# --------------------------------------------------------------------------
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".simulator_state")
os.makedirs(STATE_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
LAST_CSV_PATH = os.path.join(STATE_DIR, "last_upload.csv")
LAST_CSV_NAME_PATH = os.path.join(STATE_DIR, "last_upload_name.txt")

PERSISTED_KEYS = [
    "threshold_pct_raw", "partial_pct_raw", "guard_mode",
    "use_second_guard", "threshold_pct_2_raw", "guard_mode_2",
    "use_trade_profit_cap", "trade_profit_cap_value",
    "use_trade_loss_cap", "trade_loss_cap_value",
    "use_profit_cap", "profit_cap_value",
    "use_loss_cap", "loss_cap_value",
    "exclude_weekend_held", "target_lot_size", "testing_period",
    "_sizing_file_id", "_lot_size_anchor",
    "custom_start_date", "custom_end_date",
    "use_time_filter", "time_filter_start", "time_filter_end",
]

if "_settings_restored" not in st.session_state:
    st.session_state["_settings_restored"] = True
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                saved_settings = json.load(f)
            for k in PERSISTED_KEYS:
                if k not in saved_settings:
                    continue
                v = saved_settings[k]
                if k in ("custom_start_date", "custom_end_date") and v:
                    v = pd.Timestamp(v).date()
                if k in ("time_filter_start", "time_filter_end") and v:
                    v = dt_time.fromisoformat(v)
                st.session_state[k] = v
        except (json.JSONDecodeError, OSError):
            pass

# Widget defaults live in session state rather than in each widget's value=
# argument, since restored settings are also written there and Streamlit
# warns when a keyed widget gets both.
WIDGET_DEFAULTS = {
    "threshold_pct_raw": 50,
    "partial_pct_raw": 50,
    "use_second_guard": False,
    "threshold_pct_2_raw": 90,
    "use_trade_profit_cap": False,
    "trade_profit_cap_value": 200.0,
    "use_trade_loss_cap": False,
    "trade_loss_cap_value": 200.0,
    "use_profit_cap": False,
    "profit_cap_value": 500.0,
    "use_loss_cap": False,
    "loss_cap_value": 500.0,
    "exclude_weekend_held": False,
    "use_time_filter": False,
    "time_filter_start": dt_time(0, 0),
    "time_filter_end": dt_time(12, 0),
}
for k, v in WIDGET_DEFAULTS.items():
    st.session_state.setdefault(k, v)


def save_settings():
    data = {k: st.session_state[k] for k in PERSISTED_KEYS if k in st.session_state}
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, default=str)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Trade Journal integration: push the simulated trades into a dedicated
# account in the local Trade Journal app (localhost:3000) via its own
# import API, so its real Dashboard/Calendar pages render them. Only ever
# touches the one account this creates for itself - every other account
# (including any MT5 one) is left alone.
# --------------------------------------------------------------------------
TRADE_JOURNAL_URL = "http://localhost:3000"
TRADE_JOURNAL_ACCOUNT_NAME = "Excursion Simulator"


def push_to_trade_journal(csv_text, file_name="simulated_trades.csv"):
    accounts_resp = requests.get(f"{TRADE_JOURNAL_URL}/api/accounts", params={"summary": "1"}, timeout=5)
    accounts_resp.raise_for_status()
    accounts = accounts_resp.json().get("accounts", [])
    account = next((a for a in accounts if a["name"] == TRADE_JOURNAL_ACCOUNT_NAME), None)

    if account is None:
        create_resp = requests.post(
            f"{TRADE_JOURNAL_URL}/api/accounts",
            json={"name": TRADE_JOURNAL_ACCOUNT_NAME, "kind": "import", "currency": "USD"},
            timeout=5,
        )
        create_resp.raise_for_status()
        account_id = create_resp.json()["id"]
    else:
        account_id = account["id"]

    # Re-importing the same trades with a new simulated PnL would otherwise
    # dedupe as identical fills (the content hash ignores reported PnL), so
    # clear this account's own data before every push instead of appending.
    clear_resp = requests.post(
        f"{TRADE_JOURNAL_URL}/api/accounts/{account_id}/actions",
        json={"action": "clear"},
        timeout=5,
    )
    clear_resp.raise_for_status()

    # Trade Journal's TradingView parser detects the symbol from the
    # exchange+ticker pattern in the *filename* (there's no Symbol column in
    # this format) - so it needs the original uploaded filename, not a
    # generic one, or it refuses the import with "choose a symbol."
    import_resp = requests.post(
        f"{TRADE_JOURNAL_URL}/api/import",
        json={
            "mode": "commit",
            "content": csv_text,
            "accountId": account_id,
            "fileName": file_name,
        },
        timeout=30,
    )
    if not import_resp.ok:
        try:
            detail = import_resp.json().get("error", import_resp.text)
        except ValueError:
            detail = import_resp.text
        raise RuntimeError(f"Trade Journal rejected the import: {detail}")
    return import_resp.json()


GUARD_BE = "Break-Even (BE)"
GUARD_QUARTER = "Quarter-Stop (1/4)"
GUARD_HALF = "Half-Stop (1/2)"
GUARD_THREE_QUARTER = "Three-Quarter-Stop (3/4)"
GUARD_FULL = "Full Runner"

GUARD_RISK_FRACTIONS = {
    GUARD_BE: 0.0,
    GUARD_QUARTER: 0.25,
    GUARD_HALF: 0.5,
    GUARD_THREE_QUARTER: 0.75,
    GUARD_FULL: 1.0,
}


def find_col(columns, keywords, exclude=None):
    exclude = exclude or []
    for col in columns:
        lc = col.lower()
        if all(k in lc for k in keywords) and not any(x in lc for x in exclude):
            return col
    return None


def is_dropped_export_column(col):
    """Every raw upload column passes through to the export untouched
    except the raw dollar cumulative-PnL column: the app recomputes its
    own baseline/simulated cumulative total and exports it under that
    same TradingView column name, so keeping the raw one too would create
    a duplicate 'Cumulative PnL USD' header."""
    lc = col.lower()
    return "cumulative" in lc and "usd" in lc


EXPORT_RENAME = {
    "Trade": "Trade number",
    "Date and Time": "Date and time",
    "Baseline PnL USD": "Net PnL USD",
    "Favorable Excursion USD": "Favorable excursion USD",
    "Adverse Excursion USD": "Adverse excursion USD",
    "Baseline Cumulative PnL": "Cumulative PnL USD",
    "Position Size": "Size (qty)",
}

# Net PnL USD / Cumulative PnL USD are populated with the simulated (or
# capped, when daily caps are active) result before export - see the
# download section, which overwrites "Baseline PnL USD" / "Baseline
# Cumulative PnL" with the effective column prior to this rename.
FULL_EXPORT_ORDER = [
    "Trade number", "Type", "Date and time", "Signal", "Price USD",
    "Size (qty)", "Size (value)", "Net PnL USD", "Return %", "Commission USD",
    "Favorable excursion USD", "Favorable excursion %",
    "Adverse excursion USD", "Adverse excursion %",
    "Cumulative PnL USD", "Cumulative PnL %", "Duration (bars)",
]

SIMULATED_ONLY_EXPORT_ORDER = [
    "Trade number", "Date and time", "Net PnL USD", "Cumulative PnL USD",
]


def prepare_export(df_subset, columns_order):
    renamed = df_subset.rename(columns=EXPORT_RENAME)
    cols = [c for c in columns_order if c in renamed.columns]
    return renamed[cols]


def to_numeric(series):
    if series.dtype.kind in "fi":
        return series.astype(float)
    cleaned = (
        series.astype(str)
        .str.replace(r"[\$,]", "", regex=True)
        .str.replace(r"^\((.*)\)$", r"-\1", regex=True)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def load_trades(uploaded_file):
    raw = pd.read_csv(uploaded_file)
    raw.columns = [c.strip() for c in raw.columns]

    trade_col = find_col(raw.columns, ["trade"])
    type_col = find_col(raw.columns, ["type"])
    pnl_col = find_col(raw.columns, ["net", "pnl"]) or find_col(raw.columns, ["profit"], exclude=["%", "cumulative"])
    fav_col = find_col(raw.columns, ["favorable"])
    adv_col = find_col(raw.columns, ["adverse"])
    date_col = find_col(raw.columns, ["date"]) or find_col(raw.columns, ["time"])
    size_col = (
        find_col(raw.columns, ["position", "size"])
        or find_col(raw.columns, ["qty"])
        or find_col(raw.columns, ["quantity"])
        or find_col(raw.columns, ["contracts"])
    )
    cum_col = find_col(raw.columns, ["cumulative", "pnl"], exclude=["%"])
    price_col = find_col(raw.columns, ["price"])
    missing = [
        name
        for name, col in [
            ("Trade number", trade_col),
            ("Net PnL USD", pnl_col),
            ("Favorable excursion USD", fav_col),
        ]
        if col is None
    ]
    if missing:
        raise ValueError(
            "Could not find the following required column(s) in the CSV: "
            + ", ".join(missing)
            + ". Found columns: "
            + ", ".join(raw.columns)
        )

    raw[trade_col] = to_numeric(raw[trade_col])
    if date_col is not None:
        raw[date_col] = pd.to_datetime(raw[date_col], errors="coerce")

    # Unfiltered upload (Entry + Exit rows, original columns/order) kept for
    # the full CSV export, so journal-import tools that need a paired entry
    # row per exit still work - see the download section for the per-trade
    # Net PnL / Cumulative PnL overwrite.
    raw_export_df = raw.copy()

    entry_dates = {}
    if type_col is not None and date_col is not None:
        entry_rows = raw[raw[type_col].astype(str).str.lower().str.contains("entry")]
        entry_dates = entry_rows.dropna(subset=[trade_col]).set_index(trade_col)[date_col].to_dict()

    df = raw.copy()

    if type_col is not None and df[type_col].astype(str).str.lower().str.contains("exit").any():
        df = df[df[type_col].astype(str).str.lower().str.contains("exit")].copy()

    df[pnl_col] = to_numeric(df[pnl_col])
    df[fav_col] = to_numeric(df[fav_col])
    if adv_col is not None:
        df[adv_col] = to_numeric(df[adv_col])
    if size_col is not None:
        df[size_col] = to_numeric(df[size_col])

    df = df.dropna(subset=[pnl_col]).copy()
    df = df.sort_values(trade_col).reset_index(drop=True)

    if date_col is not None and df[date_col].notna().any():
        dates = df[date_col].reset_index(drop=True)
    else:
        dates = pd.Series(pd.date_range("2025-01-01", periods=len(df), freq="h"))

    out = pd.DataFrame(
        {
            "Trade": df[trade_col],
            "Date and Time": dates,
            "Baseline PnL USD": df[pnl_col],
            "Favorable Excursion USD": df[fav_col].fillna(0.0),
        }
    )
    if adv_col is not None:
        out["Adverse Excursion USD"] = df[adv_col].reset_index(drop=True)
    if size_col is not None:
        out["Position Size"] = df[size_col].reset_index(drop=True)

    special_cols = {c for c in [trade_col, pnl_col, fav_col, adv_col, date_col, size_col] if c}
    extra_cols = [
        c for c in df.columns
        if c not in special_cols and not is_dropped_export_column(c)
    ]
    for c in extra_cols:
        out[c] = df[c].reset_index(drop=True)

    entry_dt_series = out["Trade"].map(entry_dates) if entry_dates else pd.Series([pd.NaT] * len(out))
    entry_dt_series = entry_dt_series.where(entry_dt_series.notna(), out["Date and Time"])
    out["Entry Time"] = pd.to_datetime(entry_dt_series, errors="coerce").reset_index(drop=True)
    out["Weekend Held"] = [
        is_weekend_held(e, x) for e, x in zip(entry_dt_series, out["Date and Time"])
    ]
    out.attrs["raw_export_df"] = raw_export_df
    out.attrs["export_trade_col"] = trade_col
    out.attrs["export_pnl_col"] = pnl_col
    out.attrs["export_cum_col"] = cum_col
    out.attrs["export_type_col"] = type_col
    out.attrs["export_size_col"] = size_col
    out.attrs["export_price_col"] = price_col
    return out


def is_weekend_held(entry_dt, exit_dt):
    if pd.isna(entry_dt) or pd.isna(exit_dt):
        return False
    start = min(entry_dt, exit_dt).normalize()
    end = max(entry_dt, exit_dt).normalize()
    return any(d.weekday() >= 5 for d in pd.date_range(start, end, freq="D"))


def simulate_trade(
    baseline_pnl,
    fav_exc,
    threshold_pct,
    partial_pct,
    guard_mode,
    threshold_pct_2=0.0,
    guard_mode_2=None,
):
    """Guard-only simulation, run on the RAW trade. Assumes the caller has
    already confirmed the guard actually triggers (see resolve_trade).

    The second guard stage, when enabled, re-tightens the remaining
    runner's stop once favorable excursion reaches a further (higher)
    threshold - it never re-touches the partial already banked by stage
    one, only which risk fraction the remainder is exposed to."""
    fav_exc = 0.0 if pd.isna(fav_exc) else fav_exc
    threshold_value = threshold_pct * abs(baseline_pnl)
    partial_pnl = partial_pct * threshold_value

    if baseline_pnl > 0:
        remainder_pnl = (1 - partial_pct) * baseline_pnl
    else:
        fraction = GUARD_RISK_FRACTIONS.get(guard_mode, 1.0)
        threshold_value_2 = threshold_pct_2 * abs(baseline_pnl)
        if guard_mode_2 is not None and threshold_pct_2 > 0 and fav_exc >= threshold_value_2:
            fraction = GUARD_RISK_FRACTIONS.get(guard_mode_2, fraction)
        remainder_pnl = (1 - partial_pct) * baseline_pnl * fraction

    return partial_pnl + remainder_pnl


RESOLUTION_LOSS_CAP = "Loss Cap"
RESOLUTION_PROFIT_CAP = "Profit Cap"
RESOLUTION_GUARD = "Guard"


def resolve_trade(
    baseline_pnl,
    fav_exc,
    adv_exc,
    has_adverse,
    threshold_pct,
    partial_pct,
    guard_mode,
    use_trade_profit_cap,
    trade_profit_cap_value,
    use_trade_loss_cap,
    trade_loss_cap_value,
    threshold_pct_2=0.0,
    guard_mode_2=None,
):
    """Resolve a single trade's simulated PnL.

    The loss cap is checked FIRST, against the raw adverse excursion,
    regardless of whether the trade eventually became a winner or loser and
    regardless of whether the guard would also trigger. This matters for a
    real, easy-to-miss case: a trade that dipped past the loss cap level at
    some point, then recovered into a genuine WINNER. A real hard stop-loss
    doesn't know or care what the trade eventually became - if price
    reached that level, the position would have closed right there, live,
    and never gotten the chance to recover at all. Checking only trades
    that end up negative (guard-adjusted or raw) misses this entirely.

    This single unconditional check is also sufficient on its own: for a
    losing trade, the magnitude of its final loss can never exceed its own
    adverse excursion (it closed at some point along that same price path,
    so the close can't be worse than the worst point reached). So if raw
    AE never reaches the cap, nothing derived from that trade afterward -
    guard-adjusted or not - can reach the cap either, and no second
    cap-check is needed once this first one has passed.

    Only once the loss cap has NOT fired does the guard get a chance to
    act: if the trade's favorable excursion reached the trigger threshold,
    the guard's own result is used; otherwise the trade's raw outcome
    stands (with the profit cap still checked against it, same reasoning
    as the loss cap but on the favorable side).

    Returns a (pnl, resolved_by) tuple so callers can tell exactly which
    mechanism determined each trade's outcome.
    """
    if pd.isna(baseline_pnl):
        return baseline_pnl, RESOLUTION_GUARD

    fav_exc = 0.0 if pd.isna(fav_exc) else fav_exc
    adv_exc_abs = abs(adv_exc) if (has_adverse and adv_exc is not None and pd.notna(adv_exc)) else None

    # Hard loss cap, checked first, unconditionally, against the raw
    # excursion - a real stop-loss order that fires independent of
    # anything the guard would have done, and catches winners that dipped
    # too deep before recovering, not just trades that end up as losers.
    if use_trade_loss_cap and adv_exc_abs is not None and adv_exc_abs >= trade_loss_cap_value:
        return -trade_loss_cap_value, RESOLUTION_LOSS_CAP

    threshold_value = threshold_pct * abs(baseline_pnl)
    guard_would_trigger = fav_exc >= threshold_value and threshold_value > 0

    if guard_would_trigger:
        guard_pnl = simulate_trade(
            baseline_pnl, fav_exc, threshold_pct, partial_pct, guard_mode,
            threshold_pct_2, guard_mode_2,
        )
        if use_trade_profit_cap and guard_pnl > 0 and guard_pnl > trade_profit_cap_value:
            return trade_profit_cap_value, RESOLUTION_PROFIT_CAP
        return guard_pnl, RESOLUTION_GUARD

    # Guard never engaged - the original stop/target was what actually
    # protected (or exposed) this trade. The loss cap already passed above,
    # so only the profit cap remains to check, against the raw excursion.
    profit_cap_breached = (
        use_trade_profit_cap
        and fav_exc >= trade_profit_cap_value
    )
    if profit_cap_breached:
        return trade_profit_cap_value, RESOLUTION_PROFIT_CAP

    return baseline_pnl, RESOLUTION_GUARD


def apply_daily_caps(df, use_profit_cap, profit_cap_value, use_loss_cap, loss_cap_value):
    """Walk trades in chronological order and clip each day's running total
    at the profit cap and/or loss cap. Runs on the already-resolved (guard +
    per-trade cap) result for each trade, so the real sequence is
    guard -> per-trade cap -> daily cap."""
    df = df.sort_values("Date and Time").reset_index(drop=True)

    capped = []
    hidden = []
    running = 0.0
    stopped = False
    current_day = object()

    for _, row in df.iterrows():
        raw_dt = row["Date and Time"]
        day = raw_dt.date() if pd.notna(raw_dt) else None

        if day != current_day:
            current_day = day
            running = 0.0
            stopped = False

        pnl = row["Simulated PnL USD"]

        if stopped:
            capped.append(0.0)
            hidden.append(True)
            continue

        proposed = running + pnl
        hit_profit = use_profit_cap and proposed >= profit_cap_value
        hit_loss = use_loss_cap and proposed <= -loss_cap_value

        if hit_profit and hit_loss:
            if abs(profit_cap_value - running) <= abs(-loss_cap_value - running):
                hit_loss = False
            else:
                hit_profit = False

        if hit_profit:
            capped.append(profit_cap_value - running)
            hidden.append(False)
            running = profit_cap_value
            stopped = True
        elif hit_loss:
            capped.append(-loss_cap_value - running)
            hidden.append(False)
            running = -loss_cap_value
            stopped = True
        else:
            capped.append(pnl)
            hidden.append(False)
            running = proposed

    df["Capped PnL USD"] = capped
    df["Hidden By Cap"] = hidden
    return df


def max_drawdown(cumulative_series):
    # Peak starts at 0 (starting equity), not at the first cumulative value -
    # otherwise a curve that opens with a loss and never recovers above it
    # (exactly what a tight cap produces) reports 0 drawdown instead of the
    # real decline from starting capital.
    running_peak = cumulative_series.cummax().clip(lower=0.0)
    drawdown = running_peak - cumulative_series
    return drawdown.max() if len(drawdown) > 0 else 0.0


def worst_daily_drawdown(df, pnl_col):
    worst = 0.0
    for _, day_trades in df.groupby(df["Date and Time"].dt.date):
        cum = 0.0
        peak = 0.0
        for pnl in day_trades[pnl_col]:
            cum += pnl
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
    return worst


def run_simulation(
    df,
    threshold_pct,
    partial_pct,
    guard_mode,
    use_trade_profit_cap,
    trade_profit_cap_value,
    use_trade_loss_cap,
    trade_loss_cap_value,
    use_profit_cap,
    profit_cap_value,
    use_loss_cap,
    loss_cap_value,
    threshold_pct_2=0.0,
    guard_mode_2=None,
):
    df = df.copy()
    has_adverse = "Adverse Excursion USD" in df.columns

    def resolve(row):
        adv_exc = row.get("Adverse Excursion USD") if has_adverse else None
        return resolve_trade(
            row["Baseline PnL USD"],
            row["Favorable Excursion USD"],
            adv_exc,
            has_adverse,
            threshold_pct,
            partial_pct,
            guard_mode,
            use_trade_profit_cap,
            trade_profit_cap_value,
            use_trade_loss_cap,
            trade_loss_cap_value,
            threshold_pct_2,
            guard_mode_2,
        )

    resolved = df.apply(resolve, axis=1)
    df["Simulated PnL USD"] = resolved.apply(lambda r: r[0])
    df["Resolved By"] = resolved.apply(lambda r: r[1])

    df = apply_daily_caps(df, use_profit_cap, profit_cap_value, use_loss_cap, loss_cap_value)
    df = df.sort_values("Date and Time").reset_index(drop=True)
    df["Baseline Cumulative PnL"] = df["Baseline PnL USD"].cumsum()
    df["Simulated Cumulative PnL"] = df["Simulated PnL USD"].cumsum()
    df["Capped Cumulative PnL"] = df["Capped PnL USD"].cumsum()
    return df


uploaded_file = st.sidebar.file_uploader("Upload TradingView backtest CSV", type=["csv"])
st.sidebar.caption(
    "Upload a TradingView 'List of Trades' CSV export. Required columns: "
    "Trade number, Type, Net PnL USD, Favorable excursion USD. A date/time "
    "column is used when present."
)
if st.sidebar.button("Clear saved session", help="Forgets the saved upload and settings; starts fresh."):
    for p in (SETTINGS_PATH, LAST_CSV_PATH, LAST_CSV_NAME_PATH):
        if os.path.exists(p):
            os.remove(p)
    st.session_state.clear()
    st.rerun()
st.sidebar.markdown("---")

st.sidebar.header("Risk Management Controls")

threshold_pct = st.sidebar.slider(
    "Gain Guard Threshold (%)",
    min_value=0,
    max_value=100,
    step=5,
    key="threshold_pct_raw",
    help="Percentage of the excursion/target distance required to trigger a partial take-profit.",
) / 100.0

partial_pct = st.sidebar.slider(
    "Partial Close Size (%)",
    min_value=0,
    max_value=100,
    step=5,
    key="partial_pct_raw",
    help="Percentage of the position size scaled out when the threshold is met.",
) / 100.0

guard_mode = st.sidebar.selectbox(
    "Stop-Loss Guard Mode",
    options=[GUARD_BE, GUARD_QUARTER, GUARD_HALF, GUARD_THREE_QUARTER, GUARD_FULL],
    key="guard_mode",
    help=(
        "Break-Even: remaining runner risk moves to 0. "
        "Quarter-Stop: remaining runner absorbs 1/4 of the initial risk. "
        "Half-Stop: remaining runner absorbs 1/2 of the initial risk. "
        "Three-Quarter-Stop: remaining runner absorbs 3/4 of the initial risk. "
        "Full Runner: no guard, runs to original outcome."
    ),
)

use_second_guard = st.sidebar.checkbox(
    "Enable Second Excursion Guard",
    key="use_second_guard",
    help="Re-tightens the remaining runner's stop again once a further (higher) favorable excursion is reached.",
)
threshold_pct_2 = st.sidebar.slider(
    "Second Gain Guard Threshold (%)",
    min_value=0,
    max_value=100,
    step=5,
    key="threshold_pct_2_raw",
    disabled=not use_second_guard,
    help="Percentage of the excursion/target distance required to trigger the second stop move.",
) / 100.0
guard_mode_2 = st.sidebar.selectbox(
    "Second Stop-Loss Guard Mode",
    options=[GUARD_BE, GUARD_QUARTER, GUARD_HALF, GUARD_THREE_QUARTER, GUARD_FULL],
    key="guard_mode_2",
    disabled=not use_second_guard,
    help="Risk fraction the remaining runner is re-exposed to once the second threshold is reached (same options as the first guard).",
)

st.sidebar.markdown("---")
st.sidebar.header("Per-Trade PnL Cap (optional)")
st.sidebar.caption(
    "Simulates a real hard ceiling: even when the guard already triggered, "
    "if this cap is TIGHTER than the guard's own result, the cap wins. On "
    "trades the guard never triggers on, the cap is checked against the "
    "raw excursion instead, representing the original stop/target."
)

use_trade_profit_cap = st.sidebar.checkbox("Enable Per-Trade Profit Cap", key="use_trade_profit_cap")
trade_profit_cap_value = st.sidebar.number_input(
    "Per-Trade Profit Cap (USD)",
    min_value=0.0,
    step=25.0,
    key="trade_profit_cap_value",
    disabled=not use_trade_profit_cap,
    help=(
        "If the trade's favorable excursion reached this amount, it's treated as "
        "hitting a target there - even a trade that ultimately closed as a loss. "
        "Only checked on trades the guard didn't already trigger on. Rescales "
        "automatically when Target Lot Size changes; edit it directly to set a new baseline."
    ),
)

use_trade_loss_cap = st.sidebar.checkbox("Enable Per-Trade Loss Cap", key="use_trade_loss_cap")
trade_loss_cap_value = st.sidebar.number_input(
    "Per-Trade Loss Cap (USD)",
    min_value=0.0,
    step=25.0,
    key="trade_loss_cap_value",
    disabled=not use_trade_loss_cap,
    help=(
        "If the trade's adverse excursion reached this amount, it's treated as "
        "getting stopped out there - even a trade that ultimately closed profitable. "
        "Only checked on trades the guard didn't already trigger on. Requires an "
        "Adverse Excursion column in the file; without it, this cap can't act on "
        "any trade and the guard's own result is used instead. Rescales automatically "
        "when Target Lot Size changes; edit it directly to set a new baseline."
    ),
)

st.sidebar.markdown("---")
st.sidebar.header("Daily PnL Caps (optional)")
st.sidebar.caption("Applied on top of the guard + per-trade cap result above. Turn either or both on.")

use_profit_cap = st.sidebar.checkbox("Enable Daily Profit Cap", key="use_profit_cap")
profit_cap_value = st.sidebar.number_input(
    "Daily Profit Cap (USD)",
    min_value=0.0,
    step=50.0,
    key="profit_cap_value",
    disabled=not use_profit_cap,
    help=(
        "Once a day's running simulated PnL reaches this amount, later trades that day "
        "contribute $0. Rescales automatically when Target Lot Size changes; edit it "
        "directly to set a new baseline."
    ),
)

use_loss_cap = st.sidebar.checkbox("Enable Daily Loss Cap", key="use_loss_cap")
loss_cap_value = st.sidebar.number_input(
    "Daily Loss Cap (USD)",
    min_value=0.0,
    step=50.0,
    key="loss_cap_value",
    disabled=not use_loss_cap,
    help=(
        "Once a day's running simulated PnL drops to minus this amount, later trades "
        "that day contribute $0. Rescales automatically when Target Lot Size changes; "
        "edit it directly to set a new baseline."
    ),
)

st.sidebar.markdown("---")
st.sidebar.header("Trade Filters")
exclude_weekend_held = st.sidebar.checkbox(
    "Exclude Weekend-Held Trades",
    key="exclude_weekend_held",
    help=(
        "Drops trades whose entry-to-exit span (or exit day, if entry timing "
        "isn't in the file) includes a Saturday or Sunday."
    ),
)
use_time_filter = st.sidebar.checkbox(
    "Filter by Entry Time",
    key="use_time_filter",
    help=(
        "Only counts trades opened inside this time-of-day window (the From time "
        "is included, the To time is not). Times are in the CSV's own timezone. "
        "If From is later than To, the window wraps past midnight."
    ),
)
time_from_col, time_to_col = st.sidebar.columns(2)
time_filter_start = time_from_col.time_input(
    "From", key="time_filter_start", step=timedelta(minutes=15), disabled=not use_time_filter,
)
time_filter_end = time_to_col.time_input(
    "To", key="time_filter_end", step=timedelta(minutes=15), disabled=not use_time_filter,
)

st.sidebar.markdown("---")

if uploaded_file is not None:
    csv_bytes = uploaded_file.getvalue()
    active_file_name = uploaded_file.name
    with open(LAST_CSV_PATH, "wb") as f:
        f.write(csv_bytes)
    with open(LAST_CSV_NAME_PATH, "w") as f:
        f.write(active_file_name)
elif os.path.exists(LAST_CSV_PATH):
    with open(LAST_CSV_PATH, "rb") as f:
        csv_bytes = f.read()
    active_file_name = (
        open(LAST_CSV_NAME_PATH).read().strip() if os.path.exists(LAST_CSV_NAME_PATH) else "restored.csv"
    )
else:
    csv_bytes = None
    active_file_name = None

if csv_bytes is not None:
    try:
        trades = load_trades(io.BytesIO(csv_bytes))
    except ValueError as e:
        st.error(str(e))
        st.stop()

    if trades.empty:
        st.warning("No trade rows were found in this file.")
        st.stop()

    st.sidebar.markdown("---")
    st.sidebar.header("Position Sizing")
    st.sidebar.caption(
        "Lot size is read straight from the CSV's own position-size column. Enter the "
        "size you actually want to simulate - every PnL and excursion dollar value, plus "
        "every per-trade and daily cap below, is scaled by target ÷ CSV size."
    )

    detected_sizes = trades["Position Size"].dropna() if "Position Size" in trades.columns else pd.Series(dtype=float)
    if not detected_sizes.empty:
        csv_lot_size = float(detected_sizes.mode().iloc[0])
        st.sidebar.caption(f"Detected CSV lot size: {csv_lot_size:g}")
        if detected_sizes.nunique() > 1:
            st.sidebar.caption(
                f"This file's position size varies trade to trade ({detected_sizes.min():g}-"
                f"{detected_sizes.max():g}); using the most common value ({csv_lot_size:g}) as the baseline."
            )
    else:
        st.sidebar.warning(
            "No position/size column found in this CSV - enter its lot size manually."
        )
        csv_lot_size = st.sidebar.number_input(
            "CSV Lot Size", min_value=0.0001, value=1.0, step=1.0,
            help="The lot/contract size this CSV's Net PnL and excursion columns were recorded at.",
        )

    # The four cap widgets above are seeded relative to whatever Target Lot Size
    # was in effect when their value was last set (by default or by hand). When
    # this file is first loaded (or a different file replaces it), re-anchor
    # Target Lot Size - and the caps, which start untouched - to the file's own
    # detected size, so nothing is scaled relative to a stale, unrelated file.
    # The testing period resets too, so a new file isn't cut down to a date
    # range picked for the previous one.
    if st.session_state.get("_sizing_file_id") != active_file_name:
        st.session_state["_sizing_file_id"] = active_file_name
        st.session_state["target_lot_size"] = csv_lot_size
        st.session_state["_lot_size_anchor"] = csv_lot_size
        st.session_state["testing_period"] = "Entire history"
        st.session_state.pop("custom_start_date", None)
        st.session_state.pop("custom_end_date", None)

    def _rescale_caps_for_lot_size():
        anchor = st.session_state.get("_lot_size_anchor") or st.session_state["target_lot_size"]
        new_size = st.session_state["target_lot_size"]
        if anchor and anchor > 0:
            ratio = new_size / anchor
            for cap_key in (
                "trade_profit_cap_value",
                "trade_loss_cap_value",
                "profit_cap_value",
                "loss_cap_value",
            ):
                if cap_key in st.session_state:
                    st.session_state[cap_key] = round(st.session_state[cap_key] * ratio, 2)
        st.session_state["_lot_size_anchor"] = new_size

    target_lot_size = st.sidebar.number_input(
        "Target Lot Size",
        min_value=0.0001,
        step=1.0,
        key="target_lot_size",
        on_change=_rescale_caps_for_lot_size,
        help=(
            "The lot/contract size you want to simulate. 20 with a CSV size of 10 doubles "
            "every value and every cap above; 5 halves them. Change a cap by hand afterward "
            "and it becomes the new baseline the next time you change this."
        ),
    )
    size_multiplier = target_lot_size / csv_lot_size
    if size_multiplier != 1.0:
        dollar_cols = [
            c for c in ["Baseline PnL USD", "Favorable Excursion USD", "Adverse Excursion USD"]
            if c in trades.columns
        ]
        trades[dollar_cols] = trades[dollar_cols] * size_multiplier

    st.sidebar.markdown("---")
    header_col, reset_col = st.sidebar.columns([3, 1])
    header_col.markdown("**Testing period**")

    PERIOD_OPTIONS = [
        "Last 7 days",
        "Last 30 days",
        "Last 90 days",
        "Last 365 days",
        "Entire history",
        "Custom date range",
    ]
    if "testing_period" not in st.session_state:
        st.session_state["testing_period"] = "Entire history"

    def _reset_testing_period():
        st.session_state["testing_period"] = "Entire history"

    reset_col.button("Reset", key="reset_testing_period_btn", on_click=_reset_testing_period)

    testing_period = st.sidebar.radio(
        "Testing period",
        PERIOD_OPTIONS,
        key="testing_period",
        label_visibility="collapsed",
    )

    min_dt = trades["Date and Time"].min().date()
    max_dt = trades["Date and Time"].max().date()

    if testing_period == "Custom date range":
        st.session_state.setdefault("custom_start_date", min_dt)
        st.session_state.setdefault("custom_end_date", max_dt)
        st.session_state["custom_start_date"] = min(max(st.session_state["custom_start_date"], min_dt), max_dt)
        st.session_state["custom_end_date"] = min(max(st.session_state["custom_end_date"], min_dt), max_dt)
        start_date = st.sidebar.date_input(
            "Start Date", min_value=min_dt, max_value=max_dt, key="custom_start_date"
        )
        end_date = st.sidebar.date_input(
            "End Date", min_value=min_dt, max_value=max_dt, key="custom_end_date"
        )
    else:
        end_date = max_dt
        if testing_period == "Last 7 days":
            start_date = max_dt - timedelta(days=7)
        elif testing_period == "Last 30 days":
            start_date = max_dt - timedelta(days=30)
        elif testing_period == "Last 90 days":
            start_date = max_dt - timedelta(days=90)
        elif testing_period == "Last 365 days":
            start_date = max_dt - timedelta(days=365)
        else:
            start_date = min_dt
        start_date = max(start_date, min_dt)

    mask = (trades["Date and Time"].dt.date >= start_date) & (trades["Date and Time"].dt.date <= end_date)
    filtered_trades = trades.loc[mask].copy()

    save_settings()

    if exclude_weekend_held:
        filtered_trades = filtered_trades[~filtered_trades["Weekend Held"]].copy()

    if use_time_filter:
        entry_dt = filtered_trades["Entry Time"]
        entry_min = entry_dt.dt.hour * 60 + entry_dt.dt.minute + entry_dt.dt.second / 60
        start_min = time_filter_start.hour * 60 + time_filter_start.minute
        end_min = time_filter_end.hour * 60 + time_filter_end.minute
        if start_min <= end_min:
            in_window = (entry_min >= start_min) & (entry_min < end_min)
        else:
            in_window = (entry_min >= start_min) | (entry_min < end_min)
        filtered_trades = filtered_trades[in_window].copy()

    if filtered_trades.empty:
        st.warning("No trades found within the selected date range and filters.")
        st.stop()

    if use_trade_loss_cap and "Adverse Excursion USD" not in filtered_trades.columns:
        st.warning(
            "The Per-Trade Loss Cap is on, but this file has no Adverse Excursion column, "
            "so it can't tell whether price actually dipped that far during a trade. "
            "That cap will have no effect - the guard's own result is used for every trade instead."
        )

    if size_multiplier != 1.0:
        st.caption(
            f"Position sizing: CSV recorded at {csv_lot_size:g}, scaled to {target_lot_size:g} "
            f"(×{size_multiplier:g}) - every PnL and excursion dollar value below reflects this size. "
            f"The per-trade and daily caps in the sidebar were rescaled the same way when you "
            f"changed Target Lot Size."
        )

    result = run_simulation(
        filtered_trades,
        threshold_pct,
        partial_pct,
        guard_mode,
        use_trade_profit_cap,
        trade_profit_cap_value,
        use_trade_loss_cap,
        trade_loss_cap_value,
        use_profit_cap,
        profit_cap_value,
        use_loss_cap,
        loss_cap_value,
        threshold_pct_2 if use_second_guard else 0.0,
        guard_mode_2 if use_second_guard else None,
    )

    trade_caps_active = use_trade_profit_cap or use_trade_loss_cap
    caps_active = use_profit_cap or use_loss_cap
    effective_col = "Capped PnL USD" if caps_active else "Simulated PnL USD"
    effective_cum_col = "Capped Cumulative PnL" if caps_active else "Simulated Cumulative PnL"
    label_parts = ["guards"]
    if trade_caps_active:
        label_parts.append("trade caps")
    if caps_active:
        label_parts.append("daily caps")
    effective_label = "Simulated (" + " + ".join(label_parts) + ")"

    visible = result[~result["Hidden By Cap"]]
    hidden_count = len(result) - len(visible)

    baseline_total = result["Baseline PnL USD"].sum()
    sim_total = result[effective_col].sum()
    baseline_winrate = (result["Baseline PnL USD"] > 0).mean() * 100 if len(result) > 0 else 0
    sim_winrate = (visible[effective_col] > 0).mean() * 100 if len(visible) > 0 else 0
    total_trades = len(visible)
    trading_days = visible["Date and Time"].dt.date.nunique()
    avg_trades_per_day = total_trades / trading_days if trading_days > 0 else 0.0
    avg_pnl_per_day = sim_total / trading_days if trading_days > 0 else 0.0

    baseline_dd = max_drawdown(result["Baseline Cumulative PnL"])
    sim_dd = max_drawdown(result[effective_cum_col])
    baseline_daily_dd = worst_daily_drawdown(result, "Baseline PnL USD")
    sim_daily_dd = worst_daily_drawdown(result, effective_col)

    wins = visible[visible[effective_col] > 0]
    losses = visible[visible[effective_col] < 0]
    win_count = len(wins)
    loss_count = len(losses)
    gross_profit = wins[effective_col].sum()
    gross_loss = -losses[effective_col].sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    avg_win = wins[effective_col].mean() if win_count else 0.0
    avg_loss = losses[effective_col].mean() if loss_count else 0.0
    avg_win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")

    daily_pnl = visible.groupby(visible["Date and Time"].dt.date)[effective_col].sum().sort_index()
    day_win_count = int((daily_pnl > 0).sum())
    day_total = len(daily_pnl)
    day_win_pct = (day_win_count / day_total * 100) if day_total else 0.0

    period_text = testing_period if testing_period != "Custom date range" else "Custom range"
    if use_time_filter:
        period_text += f' · entries {time_filter_start.strftime("%H:%M")}–{time_filter_end.strftime("%H:%M")}'
    st.markdown(
        f'<div class="tj-page-head"><div class="tj-page-title">Dashboard</div>'
        f'<div class="tj-page-sub">{active_file_name} · {period_text} · '
        f'{start_date.strftime("%b %d, %Y")} – {end_date.strftime("%b %d, %Y")}</div></div>',
        unsafe_allow_html=True,
    )

    loss_cap_count = int((result["Resolved By"] == RESOLUTION_LOSS_CAP).sum())
    profit_cap_count = int((result["Resolved By"] == RESOLUTION_PROFIT_CAP).sum())
    guard_count = int((result["Resolved By"] == RESOLUTION_GUARD).sum())

    if trade_caps_active or caps_active:
        cap_notes = []
        if use_trade_profit_cap:
            cap_notes.append(
                f"per-trade profit cap ${trade_profit_cap_value:,.2f} "
                f"(affected {profit_cap_count} trade{'s' if profit_cap_count != 1 else ''})"
            )
        if use_trade_loss_cap:
            cap_notes.append(
                f"per-trade loss cap ${trade_loss_cap_value:,.2f} "
                f"(affected {loss_cap_count} trade{'s' if loss_cap_count != 1 else ''})"
            )
        if use_profit_cap:
            cap_notes.append(f"daily profit cap ${profit_cap_value:,.2f}")
        if use_loss_cap:
            cap_notes.append(f"daily loss cap ${loss_cap_value:,.2f}")
        cap_caption = "Caps active: " + " · ".join(cap_notes)
        if hidden_count > 0:
            cap_caption += f" · {hidden_count} trade(s) hidden by a daily cap"
        render_banner(cap_caption)

    if trade_caps_active:
        render_banner(
            f"The guard's own result stood on {guard_count} of {len(result)} trade(s) "
            f"(including any where the guard triggered but the cap was looser than its "
            f"result); the caps tightened the outcome further on the remaining "
            f"{loss_cap_count + profit_cap_count} trade(s)."
        )

    ratio_text = f"{avg_win_loss_ratio:.2f}" if avg_win_loss_ratio != float("inf") else "∞"
    win_share = avg_win / (avg_win + abs(avg_loss)) * 100 if (avg_win + abs(avg_loss)) > 0 else 50.0
    be_count = total_trades - win_count - loss_count
    wl_side = f"{win_count:,} W<br>" + (f"{be_count:,} BE<br>" if be_count else "") + f"{loss_count:,} L"
    delta = sim_total - baseline_total

    render_card_grid([
        kpi_card(
            "Net P&L", "dollar",
            f'<div class="tj-value {pnl_class(sim_total)}">{money(sim_total, signed=True)}</div>'
            f'<span class="tj-pill {pnl_class(delta)}">{"▲" if delta >= 0 else "▼"} {money(abs(delta))} vs baseline</span>'
            f'<div class="tj-sub">{total_trades:,} closed trades</div>',
        ),
        kpi_card("Trade Win %", "target", gauge(sim_winrate, wl_side)),
        kpi_card(
            "Profit Factor", "scale",
            f'<div class="tj-value">{f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"}</div>'
            '<div class="tj-sub">gross profit ÷ gross loss</div>',
        ),
        kpi_card("Day Win %", "calendar", gauge(day_win_pct, f"{day_win_count} / {day_total}<br>days")),
        kpi_card(
            "Avg Win / Loss", "arrows",
            f'<div class="tj-value">{ratio_text}</div>'
            f'<div class="tj-ratio-bar"><div style="width:{win_share:.1f}%;background:#22c55e"></div>'
            f'<div style="width:{100 - win_share:.1f}%;background:#ef4444"></div></div>'
            f'<div class="tj-sub"><span class="pos">{money(avg_win, signed=True)}</span> avg win · '
            f'<span class="neg">{money(avg_loss)}</span> avg loss</div>',
        ),
    ])
    render_card_grid([
        kpi_card(
            "Avg Trades / Day", "list",
            f'<div class="tj-value">{avg_trades_per_day:.1f}</div>'
            f'<div class="tj-sub">{total_trades:,} trades over {trading_days} trading days</div>',
        ),
        kpi_card(
            "Avg P&L / Day", "trend",
            f'<div class="tj-value {pnl_class(avg_pnl_per_day)}">{money(avg_pnl_per_day, signed=True)}</div>'
            '<div class="tj-sub">net P&L ÷ trading days</div>',
        ),
        kpi_card(
            "Max Drawdown", "down",
            f'<div class="tj-value neg">{money(-sim_dd)}</div>'
            f'<div class="tj-sub">baseline {money(-baseline_dd)} · {money(sim_dd - baseline_dd, signed=True)} vs baseline</div>',
        ),
        kpi_card(
            "Worst Day Drawdown", "down",
            f'<div class="tj-value neg">{money(-sim_daily_dd)}</div>'
            f'<div class="tj-sub">baseline {money(-baseline_daily_dd)} · {money(sim_daily_dd - baseline_daily_dd, signed=True)} vs baseline</div>',
        ),
        kpi_card(
            "Baseline", "pulse",
            f'<div class="tj-value {pnl_class(baseline_total)}">{money(baseline_total, signed=True)}</div>'
            f'<div class="tj-sub">{len(result):,} trades · {baseline_winrate:.1f}% win rate<br>'
            f'simulated = {effective_label}</div>',
        ),
    ])

    def style_chart(fig, height):
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#71717a", size=11), hovermode="x unified",
            hoverlabel=dict(bgcolor="#18181b", bordercolor="#27272a", font=dict(color="#fafafa")),
            margin=dict(t=8, b=8, l=8, r=8), height=height,
        )
        fig.update_xaxes(showgrid=False, nticks=6, linecolor="#1f1f23")
        fig.update_yaxes(gridcolor="#1f1f23", zerolinecolor="#3f3f46", tickprefix="$", tickformat="~s", nticks=6)

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        with st.container(border=True, key="tjc-cumulative"):
            st.markdown('<div class="tj-label">Daily Net Cumulative P&L</div>', unsafe_allow_html=True)
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=result["Date and Time"], y=result["Baseline Cumulative PnL"],
                    mode="lines", name="Baseline", line=dict(color="#52525b", width=1.5),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=result["Date and Time"], y=result[effective_cum_col],
                    mode="lines", name=effective_label, line=dict(color="#1d9bf0", width=2),
                    fill="tozeroy", fillcolor="rgba(29,155,240,0.10)",
                )
            )
            style_chart(fig, 290)
            fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1.0))
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

            eq = result[effective_cum_col]
            running_peak = eq.cummax().clip(lower=0.0)
            dd_series = eq - running_peak
            st.markdown(
                '<div class="tj-card-title"><span class="tj-label">Drawdown (USD)</span>'
                f'<span class="tj-label neg">Max {money(dd_series.min())}</span></div>',
                unsafe_allow_html=True,
            )
            fig = go.Figure(
                go.Scatter(
                    x=result["Date and Time"], y=dd_series, mode="lines", name="Drawdown",
                    line=dict(color="#ef4444", width=1.2), fill="tozeroy", fillcolor="rgba(239,68,68,0.18)",
                )
            )
            style_chart(fig, 150)
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    with chart_col2:
        with st.container(border=True, key="tjc-daily"):
            st.markdown('<div class="tj-label">Net Daily P&L</div>', unsafe_allow_html=True)
            bar_colors = ["#22c55e" if v >= 0 else "#ef4444" for v in daily_pnl.values]
            fig = go.Figure(
                go.Bar(x=list(daily_pnl.index), y=daily_pnl.values, marker_color=bar_colors, name="Net P&L")
            )
            style_chart(fig, 482)
            fig.update_layout(showlegend=False, bargap=0.25)
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    if day_total > 0:
        st.session_state.setdefault("selected_cal_day", None)

        last_month_period = pd.Series(list(daily_pnl.index)).map(lambda d: (d.year, d.month)).max()
        cal_year, cal_month = last_month_period
        month_days = {d: v for d, v in daily_pnl.items() if (d.year, d.month) == (cal_year, cal_month)}
        month_trade_counts = (
            visible[
                (visible["Date and Time"].dt.year == cal_year) & (visible["Date and Time"].dt.month == cal_month)
            ]
            .groupby(visible["Date and Time"].dt.date)
            .size()
        )
        month_total = sum(month_days.values())
        month_green = sum(1 for v in month_days.values() if v > 0)
        selected_day = st.session_state.get("selected_cal_day")

        # Tint each traded day's button green/red, and outline the selected one.
        day_css = []
        for d, v in month_days.items():
            bg, border = ("rgba(34,197,94,0.14)", "rgba(34,197,94,0.35)") if v >= 0 else ("rgba(239,68,68,0.14)", "rgba(239,68,68,0.35)")
            day_css.append(f".st-key-calday_{d} button {{ background: {bg}; border-color: {border}; }}")
        if selected_day:
            day_css.append(f".st-key-calday_{selected_day} button {{ border: 1px solid #1d9bf0 !important; }}")
        st.html(f"<style>{''.join(day_css)}</style>")

        with st.container(border=True, key="tjc-calendar"):
            st.markdown(
                f'<div class="tj-card-title"><span class="tj-label">{cal_module.month_name[cal_month]} {cal_year}</span>'
                '<span class="tj-note">click a day to see its trades</span></div>',
                unsafe_allow_html=True,
            )
            weeks = cal_module.Calendar(firstweekday=6).monthdayscalendar(cal_year, cal_month)
            col_widths = [1] * 7 + [1.1]
            head_cols = st.columns(col_widths, gap="small")
            for hc, wd in zip(head_cols, ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Week"]):
                hc.markdown(
                    f'<div class="tj-cal-head{" week" if wd == "Week" else ""}">{wd}</div>',
                    unsafe_allow_html=True,
                )

            for week in weeks:
                row_cols = st.columns(col_widths, gap="small")
                week_pnl = 0.0
                week_trades = 0
                for col, day in zip(row_cols, week):
                    if day == 0:
                        continue
                    d = pd.Timestamp(year=cal_year, month=cal_month, day=day).date()
                    with col:
                        if d in month_days:
                            pnl = month_days[d]
                            trades_n = int(month_trade_counts.get(d, 0))
                            week_pnl += pnl
                            week_trades += trades_n
                            label = (
                                f"{day}  \n**{'+' if pnl > 0 else '-' if pnl < 0 else ''}${abs(pnl):,.0f}**  \n"
                                f"{trades_n} trade{'s' if trades_n != 1 else ''}"
                            )
                        else:
                            label = f"{day}"
                        if st.button(label, key=f"calday_{d}", width='stretch'):
                            st.session_state["selected_cal_day"] = None if selected_day == str(d) else str(d)
                            st.rerun()
                week_html = (
                    f'<span class="{pnl_class(week_pnl)}">{money(week_pnl, signed=True)}</span>'
                    f'<span class="tj-sub">{week_trades} trades</span>'
                    if week_trades else '<span class="tj-sub">–</span>'
                )
                row_cols[7].markdown(f'<div class="tj-cal-week">{week_html}</div>', unsafe_allow_html=True)

            st.markdown(
                f'<div class="tj-cal-foot"><span>{len(month_days)} trading days · {month_green} green</span>'
                f'<span>Month: <b class="{pnl_class(month_total)}">{money(month_total, signed=True)}</b></span></div>',
                unsafe_allow_html=True,
            )

        if selected_day:
            with st.container(border=True, key="tjc-activity"):
                sel_date = pd.Timestamp(selected_day).date()
                act_trades = visible[visible["Date and Time"].dt.date == sel_date].sort_values("Date and Time")
                title = f'Trades on {sel_date.strftime("%b %d, %Y")} · {len(act_trades)}'
                head_col, clear_col = st.columns([3, 1])
                head_col.markdown(f'<div class="tj-label">{title}</div>', unsafe_allow_html=True)
                if clear_col.button("Clear", key="clear_cal_day"):
                    st.session_state["selected_cal_day"] = None
                    st.rerun()

                rows = []
                for _, t in act_trades.iterrows():
                    v = t[effective_col]
                    cls = pnl_class(v) or "flat"
                    tag = {"pos": "WIN", "neg": "LOSS", "flat": "BE"}[cls]
                    when = t["Date and Time"].strftime("%H:%M")
                    rows.append(
                        f'<div class="tj-act-row"><span class="tj-act-tag {cls}">{tag}</span>'
                        f'<span class="tj-act-main">#{t["Trade"]:.0f} <span class="tj-act-time">{when}</span></span>'
                        f'<span class="tj-act-pnl {pnl_class(v)}">{money(v, signed=True)}</span></div>'
                    )
                st.markdown(
                    f'<div class="tj-activity">{"".join(rows) or "<div class=tj-sub>No trades.</div>"}</div>',
                    unsafe_allow_html=True,
                )

    st.markdown('<div class="tj-page-head" style="margin-top:18px"><div class="tj-page-title">Granular Time Analysis</div></div>', unsafe_allow_html=True)
    tab_month, tab_week, tab_day = st.tabs(["Month-by-Month", "Week-by-Week", "Day-by-Day"])

    def build_period_summary(full_df, visible_df, period_freq):
        base = full_df.copy()
        base["Period"] = base["Date and Time"].dt.to_period(period_freq).astype(str)
        base_g = base.groupby("Period").agg(
            Baseline_Trades=("Trade", "count"),
            Baseline_PnL=("Baseline PnL USD", "sum"),
            Baseline_WinRate=("Baseline PnL USD", lambda x: (x > 0).mean() * 100),
        )

        sim = visible_df.copy()
        sim["Period"] = sim["Date and Time"].dt.to_period(period_freq).astype(str)
        sim_g = sim.groupby("Period").agg(
            Trades=("Trade", "count"),
            Simulated_PnL=(effective_col, "sum"),
            Simulated_WinRate=(effective_col, lambda x: (x > 0).mean() * 100),
        )

        summary = base_g.join(sim_g, how="outer")
        fill_cols = ["Baseline_Trades", "Baseline_PnL", "Baseline_WinRate", "Trades", "Simulated_PnL", "Simulated_WinRate"]
        summary[fill_cols] = summary[fill_cols].fillna(0)
        summary = summary.reset_index()
        summary["PnL Difference"] = summary["Simulated_PnL"] - summary["Baseline_PnL"]
        summary = summary[
            [
                "Period",
                "Baseline_Trades",
                "Trades",
                "Baseline_PnL",
                "Simulated_PnL",
                "PnL Difference",
                "Baseline_WinRate",
                "Simulated_WinRate",
            ]
        ]
        return summary

    def render_period_table(summary):
        st.dataframe(
            summary.style.format(
                {
                    "Baseline_PnL": "${:,.2f}",
                    "Simulated_PnL": "${:,.2f}",
                    "PnL Difference": "${:,.2f}",
                    "Baseline_WinRate": "{:.1f}%",
                    "Simulated_WinRate": "{:.1f}%",
                }
            ),
            width='stretch',
        )
        st.download_button(
            "Download this breakdown as CSV",
            data=summary.to_csv(index=False).encode("utf-8"),
            file_name="breakdown.csv",
            mime="text/csv",
            key=f"dl_{id(summary)}",
        )

    with tab_month:
        monthly_summary = build_period_summary(result, visible, "M")
        render_period_table(monthly_summary)

    with tab_week:
        weekly_summary = build_period_summary(result, visible, "W")
        render_period_table(weekly_summary)

    with tab_day:
        if hidden_count > 0:
            st.caption(f"{hidden_count} trade(s) hidden because a daily cap was reached that day are excluded from the Simulated side below.")
        daily_summary = build_period_summary(result, visible, "D")
        render_period_table(daily_summary)

    export_source = visible.drop(columns=["Hidden By Cap"]).copy()
    export_source["Baseline PnL USD"] = export_source[effective_col]
    export_source["Baseline Cumulative PnL"] = export_source[effective_cum_col]

    raw_export_df = trades.attrs.get("raw_export_df")
    export_trade_col = trades.attrs.get("export_trade_col")
    export_pnl_col = trades.attrs.get("export_pnl_col")
    export_cum_col = trades.attrs.get("export_cum_col")
    export_type_col = trades.attrs.get("export_type_col")
    export_size_col = trades.attrs.get("export_size_col")
    export_price_col = trades.attrs.get("export_price_col")

    if raw_export_df is not None and export_trade_col is not None:
        pnl_map = export_source.set_index("Trade")["Baseline PnL USD"]
        cum_map = export_source.set_index("Trade")["Baseline Cumulative PnL"]
        full_export = raw_export_df[raw_export_df[export_trade_col].isin(pnl_map.index)].copy()
        full_export[export_pnl_col] = full_export[export_trade_col].map(pnl_map)
        if export_cum_col is not None:
            full_export[export_cum_col] = full_export[export_trade_col].map(cum_map)

        # A guard/cap can flip a trade's simulated PnL sign relative to what
        # its raw entry/exit price actually did - importers (e.g. Trade
        # Journal) sanity-check PnL sign against price direction and flag a
        # mismatch. Re-derive a consistent exit price from the simulated PnL
        # so the two never disagree, leaving the entry price untouched.
        if export_type_col is not None and export_size_col is not None and export_price_col is not None:
            full_export[export_price_col] = pd.to_numeric(full_export[export_price_col], errors="coerce")
            type_lc = full_export[export_type_col].astype(str).str.lower()
            entry_mask = type_lc.str.contains("entry")
            exit_mask = type_lc.str.contains("exit")
            entry_price = full_export[entry_mask].set_index(export_trade_col)[export_price_col]
            is_long = full_export[entry_mask].set_index(export_trade_col)[export_type_col].astype(str).str.lower().str.contains("long")
            qty = pd.to_numeric(full_export.groupby(export_trade_col)[export_size_col].first(), errors="coerce")

            valid = qty.reindex(pnl_map.index).gt(0) & entry_price.reindex(pnl_map.index).notna()
            pnl_per_unit = (pnl_map / qty.reindex(pnl_map.index)).where(valid)
            direction = is_long.reindex(pnl_map.index)
            synth_exit_price = entry_price.reindex(pnl_map.index) + pnl_per_unit.where(direction, -pnl_per_unit)

            exit_trade_ids = full_export.loc[exit_mask, export_trade_col]
            mapped_price = exit_trade_ids.map(synth_exit_price)
            full_export.loc[exit_mask, export_price_col] = mapped_price.where(
                mapped_price.notna(), full_export.loc[exit_mask, export_price_col]
            )
    else:
        full_export = prepare_export(export_source, FULL_EXPORT_ORDER)
    csv_text = full_export.to_csv(index=False)
    csv_bytes = csv_text.encode("utf-8")
    dl_col, push_col = st.columns([2, 1])
    dl_col.download_button(
        "Download full trade-by-trade results as CSV",
        data=csv_bytes,
        file_name="simulated_trades.csv",
        mime="text/csv",
    )
    if push_col.button("Push to Trade Journal", help=f"Updates the '{TRADE_JOURNAL_ACCOUNT_NAME}' account at {TRADE_JOURNAL_URL}"):
        try:
            push_result = push_to_trade_journal(csv_text, file_name=active_file_name or "simulated_trades.csv")
            st.success(
                f"Pushed to Trade Journal -> '{TRADE_JOURNAL_ACCOUNT_NAME}' account: "
                f"{push_result.get('inserted', '?')} execution(s) imported. "
                f"Open {TRADE_JOURNAL_URL} and select that account to view."
            )
            if push_result.get("warnings"):
                st.warning(" ".join(push_result["warnings"]))
        except requests.exceptions.RequestException as e:
            st.error(f"Couldn't reach Trade Journal at {TRADE_JOURNAL_URL} - is it running? ({e})")
        except RuntimeError as e:
            st.error(str(e))

    simulated_export = prepare_export(export_source, SIMULATED_ONLY_EXPORT_ORDER)
    simulated_csv_bytes = simulated_export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download simulated results only as CSV",
        data=simulated_csv_bytes,
        file_name="simulated_only.csv",
        mime="text/csv",
    )
else:
    st.title("Excursion-Based Risk Management Simulator")
    st.write(
        "Upload a TradingView backtest CSV export in the sidebar, configure strategy "
        "parameters and optional daily caps, and analyze performance across custom "
        "dates, weeks, and days."
    )
    st.info("Upload a CSV file in the sidebar to begin.")
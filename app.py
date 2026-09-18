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

import io
from datetime import timedelta
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# --------------------------------------------------------------------------
# Page config
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="Excursion-Based Risk Management Simulator",
    layout="wide",
)

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

    entry_dt_series = out["Trade"].map(entry_dates) if entry_dates else pd.Series([pd.NaT] * len(out))
    entry_dt_series = entry_dt_series.where(entry_dt_series.notna(), out["Date and Time"])
    out["Weekend Held"] = [
        is_weekend_held(e, x) for e, x in zip(entry_dt_series, out["Date and Time"])
    ]
    return out


def is_weekend_held(entry_dt, exit_dt):
    if pd.isna(entry_dt) or pd.isna(exit_dt):
        return False
    start = min(entry_dt, exit_dt).normalize()
    end = max(entry_dt, exit_dt).normalize()
    return any(d.weekday() >= 5 for d in pd.date_range(start, end, freq="D"))


def simulate_trade(baseline_pnl, fav_exc, threshold_pct, partial_pct, guard_mode):
    """Guard-only simulation, run on the RAW trade. Assumes the caller has
    already confirmed the guard actually triggers (see resolve_trade)."""
    fav_exc = 0.0 if pd.isna(fav_exc) else fav_exc
    threshold_value = threshold_pct * abs(baseline_pnl)
    partial_pnl = partial_pct * threshold_value

    if baseline_pnl > 0:
        remainder_pnl = (1 - partial_pct) * baseline_pnl
    else:
        fraction = GUARD_RISK_FRACTIONS.get(guard_mode, 1.0)
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
        guard_pnl = simulate_trade(baseline_pnl, fav_exc, threshold_pct, partial_pct, guard_mode)
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


st.sidebar.header("Risk Management Controls")

threshold_pct = st.sidebar.slider(
    "Gain Guard Threshold (%)",
    min_value=0,
    max_value=100,
    value=50,
    step=5,
    help="Percentage of the excursion/target distance required to trigger a partial take-profit.",
) / 100.0

partial_pct = st.sidebar.slider(
    "Partial Close Size (%)",
    min_value=0,
    max_value=100,
    value=50,
    step=5,
    help="Percentage of the position size scaled out when the threshold is met.",
) / 100.0

guard_mode = st.sidebar.selectbox(
    "Stop-Loss Guard Mode",
    options=[GUARD_BE, GUARD_QUARTER, GUARD_HALF, GUARD_THREE_QUARTER, GUARD_FULL],
    help=(
        "Break-Even: remaining runner risk moves to 0. "
        "Quarter-Stop: remaining runner absorbs 1/4 of the initial risk. "
        "Half-Stop: remaining runner absorbs 1/2 of the initial risk. "
        "Three-Quarter-Stop: remaining runner absorbs 3/4 of the initial risk. "
        "Full Runner: no guard, runs to original outcome."
    ),
)

st.sidebar.markdown("---")
st.sidebar.header("Per-Trade PnL Cap (optional)")
st.sidebar.caption(
    "Simulates a real hard ceiling: even when the guard already triggered, "
    "if this cap is TIGHTER than the guard's own result, the cap wins. On "
    "trades the guard never triggers on, the cap is checked against the "
    "raw excursion instead, representing the original stop/target."
)

use_trade_profit_cap = st.sidebar.checkbox("Enable Per-Trade Profit Cap", value=False)
trade_profit_cap_value = st.sidebar.number_input(
    "Per-Trade Profit Cap (USD)",
    min_value=0.0,
    value=200.0,
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

use_trade_loss_cap = st.sidebar.checkbox("Enable Per-Trade Loss Cap", value=False)
trade_loss_cap_value = st.sidebar.number_input(
    "Per-Trade Loss Cap (USD)",
    min_value=0.0,
    value=200.0,
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

use_profit_cap = st.sidebar.checkbox("Enable Daily Profit Cap", value=False)
profit_cap_value = st.sidebar.number_input(
    "Daily Profit Cap (USD)",
    min_value=0.0,
    value=500.0,
    step=50.0,
    key="profit_cap_value",
    disabled=not use_profit_cap,
    help=(
        "Once a day's running simulated PnL reaches this amount, later trades that day "
        "contribute $0. Rescales automatically when Target Lot Size changes; edit it "
        "directly to set a new baseline."
    ),
)

use_loss_cap = st.sidebar.checkbox("Enable Daily Loss Cap", value=False)
loss_cap_value = st.sidebar.number_input(
    "Daily Loss Cap (USD)",
    min_value=0.0,
    value=500.0,
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
    value=False,
    help=(
        "Drops trades whose entry-to-exit span (or exit day, if entry timing "
        "isn't in the file) includes a Saturday or Sunday."
    ),
)

st.sidebar.markdown("---")
st.sidebar.caption(
    "Upload a TradingView 'List of Trades' CSV export. Required columns: "
    "Trade number, Type, Net PnL USD, Favorable excursion USD. A date/time "
    "column is used when present."
)

st.title("Excursion-Based Risk Management Simulator")
st.write(
    "Upload a TradingView backtest CSV export, configure strategy parameters "
    "and optional daily caps, and analyze performance across custom dates, "
    "weeks, and days."
)

uploaded_file = st.file_uploader("Upload TradingView backtest CSV", type=["csv"])

if uploaded_file is not None:
    try:
        trades = load_trades(io.BytesIO(uploaded_file.getvalue()))
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
    if st.session_state.get("_sizing_file_id") != uploaded_file.name:
        st.session_state["_sizing_file_id"] = uploaded_file.name
        st.session_state["target_lot_size"] = csv_lot_size
        st.session_state["_lot_size_anchor"] = csv_lot_size

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
        start_date = st.sidebar.date_input("Start Date", min_value=min_dt, max_value=max_dt, value=min_dt)
        end_date = st.sidebar.date_input("End Date", min_value=min_dt, max_value=max_dt, value=max_dt)
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

    if exclude_weekend_held:
        filtered_trades = filtered_trades[~filtered_trades["Weekend Held"]].copy()

    if filtered_trades.empty:
        st.warning("No trades found within the selected date range.")
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

    st.subheader("Key Performance Indicators")

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
        st.caption(cap_caption)

    if trade_caps_active:
        st.caption(
            f"The guard's own result stood on {guard_count} of {len(result)} trade(s) "
            f"(including any where the guard triggered but the cap was looser than its "
            f"result); the caps tightened the outcome further on the remaining "
            f"{loss_cap_count + profit_cap_count} trade(s)."
        )

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric(
        "Net Total PnL (USD)",
        f"${sim_total:,.2f}",
        delta=f"${sim_total - baseline_total:,.2f} vs baseline",
    )
    col2.metric(
        "Win Rate (%)",
        f"{sim_winrate:.1f}%",
        delta=f"{sim_winrate - baseline_winrate:+.1f} pts vs baseline",
    )
    col3.metric("Total Trades Analyzed", f"{total_trades}")
    col4.metric("Avg Trades / Day", f"{avg_trades_per_day:.2f}")
    col5.metric("Avg PnL / Day (USD)", f"${avg_pnl_per_day:,.2f}")

    col5, col6 = st.columns(2)
    col5.metric(
        "Max Drawdown - Baseline (USD)",
        f"${baseline_dd:,.2f}",
        help="Largest peak-to-trough decline across the entire selected period.",
    )
    col6.metric(
        "Max Drawdown - Simulated (USD)",
        f"${sim_dd:,.2f}",
        delta=f"${sim_dd - baseline_dd:,.2f} vs baseline",
        delta_color="inverse",
        help="Largest peak-to-trough decline across the entire selected period.",
    )

    col7, col8 = st.columns(2)
    col7.metric(
        "Worst Daily Drawdown - Baseline (USD)",
        f"${baseline_daily_dd:,.2f}",
        help="Largest single-day dip below that day's own starting capital - resets every day, unlike Max Drawdown above.",
    )
    col8.metric(
        "Worst Daily Drawdown - Simulated (USD)",
        f"${sim_daily_dd:,.2f}",
        delta=f"${sim_daily_dd - baseline_daily_dd:,.2f} vs baseline",
        delta_color="inverse",
        help="Largest single-day dip below that day's own starting capital - resets every day, unlike Max Drawdown above.",
    )

    st.caption(
        f"Baseline (all {len(result)} trades) — Net Total PnL: ${baseline_total:,.2f} · "
        f"Win Rate: {baseline_winrate:.1f}% · Simulated line = {effective_label}"
    )

    st.subheader("Equity Curve: Baseline vs. Simulated")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=result["Date and Time"],
            y=result["Baseline Cumulative PnL"],
            mode="lines",
            name="Baseline",
            line=dict(color="#94a3b8", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=result["Date and Time"],
            y=result[effective_cum_col],
            mode="lines",
            name=effective_label,
            line=dict(color="#2563eb", width=2),
        )
    )
    fig.update_layout(
        xaxis_title="Date",
        yaxis_title="Cumulative PnL (USD)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5),
        margin=dict(t=40, b=60),
    )
    fig.update_xaxes(type="date", dtick="M1", tickformat="%b %Y")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Granular Time Analysis")
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
            use_container_width=True,
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

    csv_bytes = visible.drop(columns=["Hidden By Cap"]).to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download full trade-by-trade results as CSV",
        data=csv_bytes,
        file_name="simulated_trades.csv",
        mime="text/csv",
    )
else:
    st.info("Upload a CSV file to begin.")
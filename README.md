# Excursion-Based Risk Management Simulator

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Streamlit](https://img.shields.io/badge/streamlit-app-ff4b4b?logo=streamlit&logoColor=white)
![Status](https://img.shields.io/badge/status-active-brightgreen)

A Streamlit app that re-simulates a TradingView backtest trade-by-trade under
a configurable partial-take-profit / stop-loss-guard strategy, with optional
per-trade and daily PnL caps, CSV-detected position sizing, and drawdown
analysis on top of the simulated equity curve.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

## Input file

A TradingView "List of Trades" CSV export. The app looks for columns matching:

- `Trade number` (or any column containing "trade")
- `Type` (used to keep only Exit rows, if present)
- `Net PnL USD` (or any column containing "net" + "pnl", falling back to "profit")
- `Favorable excursion USD` (any column containing "favorable")
- `Adverse excursion USD` (optional, carried through if present)

Column matching is case-insensitive and tolerant of TradingView's exact export
naming, so slightly different header text should still be picked up.

## Simulation logic

For each trade, with `threshold%` and `partial%` set in the sidebar:

1. **Trigger check:** `Favorable Excursion USD >= threshold% * |Baseline PnL USD|`
2. **Not triggered:** simulated PnL = baseline PnL (standard loser, unchanged).
3. **Triggered, baseline PnL > 0 (winner):**
   `simulated PnL = partial% * (threshold% * baseline PnL) + (1 - partial%) * baseline PnL`
4. **Triggered, baseline PnL < 0 (reversing near-miss):**
   `banked = partial% * (threshold% * |baseline PnL|)`
   remainder depends on the Stop-Loss Guard Mode:
   - **Break-Even:** remainder = 0
   - **Half-Stop:** remainder = `(1 - partial%) * baseline PnL * 0.5`
   - **Full Runner:** remainder = `(1 - partial%) * baseline PnL` (no guard)
   `simulated PnL = banked + remainder`

The app then compares baseline vs. simulated Net Total PnL, Win Rate, and
Total Trades, and plots both cumulative equity curves.

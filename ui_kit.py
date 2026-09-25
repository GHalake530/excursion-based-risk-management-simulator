"""Shared dashboard building blocks: Trade Journal-style KPI cards, gauges
and banners. The matching CSS is injected once by app.py."""

import streamlit as st

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
    "wallet": '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14h2"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2 .7 3 2.8 3 6"/>',
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


def gauge(pct, side_html, center=None):
    pct = max(0.0, min(100.0, pct))
    center = f"{pct:.1f}%" if center is None else center
    arc = "M10 50 A38 38 0 0 1 86 50"
    return (
        '<div class="tj-gauge-row"><div class="tj-gauge"><svg viewBox="0 0 96 56">'
        f'<path d="{arc}" fill="none" stroke="#27272a" stroke-width="9" stroke-linecap="round" pathLength="100"/>'
        f'<path d="{arc}" fill="none" stroke="#1d9bf0" stroke-width="9" stroke-linecap="round" pathLength="100" stroke-dasharray="{pct:.1f} 100"/>'
        f'</svg><span>{center}</span></div><div class="tj-gauge-side">{side_html}</div></div>'
    )


def render_card_grid(cards, cols=None):
    extra = f" cols-{cols}" if cols else ""
    st.markdown(f'<div class="tj-grid{extra}">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_banner(text):
    st.markdown(f'<div class="tj-banner">{text}</div>', unsafe_allow_html=True)

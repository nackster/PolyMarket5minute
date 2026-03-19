"""Combined web dashboard for all BTC trading bots.

Shows real-time trade history and stats for:
  - 5-Min Bot (LIVE) — btc-updown-5m markets
  - Hourly Bot (PAPER) — bitcoin-up-or-down hourly markets
  - Hyperliquid Bot (PAPER) — BTC-PERP perpetual futures
"""

import json
import os
from datetime import datetime

from flask import Flask, jsonify, render_template_string

from db import get_bot_state, get_trades, get_stats

app = Flask(__name__)

TRADES_FILE_5M      = "trades/real_trades.json"
TRADES_FILE_HOURLY  = "trades/real_trades_hourly.json"
TRADES_FILE_HL      = "trades/hl_trades.json"
TRADES_FILE_BACKTEST= "trades/backtest_crypto_trades.json"
TRADES_FILE_HL_BT   = "trades/hl_momentum_backtest.json"
TRADES_FILE_HL_LIVE = "trades/hl_momentum_live.json"
TRADES_FILE_MOMENTUM= "trades/crypto_momentum_live.json"
TRADES_FILE_SCALPER = "trades/scalper_live.json"


# ── Data helpers ─────────────────────────────────────────────────────────────

def load_json_trades(path: str) -> dict:
    """Load trades from a JSON file. Returns empty state if not found."""
    if not os.path.exists(path):
        return {"equity": 0.0, "mode": "paper", "updated_at": None, "trades": []}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"equity": 0.0, "mode": "paper", "updated_at": None, "trades": []}


def compute_stats(trades: list) -> dict:
    """Compute summary stats from a list of trade dicts."""
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0}
    n     = len(trades)
    wins  = sum(1 for t in trades if t.get("won"))
    pnl   = sum(t.get("pnl", 0) for t in trades)
    pnls  = [t.get("pnl", 0) for t in trades]
    return {
        "total":       n,
        "wins":        wins,
        "losses":      n - wins,
        "win_rate":    wins / n,
        "total_pnl":   pnl,
        "avg_pnl":     pnl / n,
        "best_trade":  max(pnls),
        "worst_trade": min(pnls),
    }


def get_5m_data():
    """Get 5-min bot data: prefer DB, fall back to JSON."""
    db_state  = get_bot_state()
    db_trades = get_trades(limit=50)
    db_stats  = get_stats()

    if db_trades:
        return db_state or {}, db_stats or {}, db_trades

    # Fallback to JSON
    state = load_json_trades(TRADES_FILE_5M)
    trades = list(reversed(state["trades"]))[:50]
    stats  = compute_stats(state["trades"])
    return state, stats, trades


def get_hourly_data():
    """Get hourly bot data from JSON file."""
    state  = load_json_trades(TRADES_FILE_HOURLY)
    trades = list(reversed(state["trades"]))[:50]
    stats  = compute_stats(state["trades"])
    return state, stats, trades


def compute_hl_stats(trades: list) -> dict:
    """Compute summary stats from Hyperliquid trade dicts (use pnl_usd, not pnl)."""
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0}
    n     = len(trades)
    wins  = sum(1 for t in trades if t.get("pnl_usd", 0) > 0)
    pnl   = sum(t.get("pnl_usd", 0) for t in trades)
    pnls  = [t.get("pnl_usd", 0) for t in trades]
    return {
        "total":       n,
        "wins":        wins,
        "losses":      n - wins,
        "win_rate":    wins / n,
        "total_pnl":   pnl,
        "avg_pnl":     pnl / n,
        "best_trade":  max(pnls),
        "worst_trade": min(pnls),
    }


def get_hl_data():
    """Get Hyperliquid bot data from JSON file."""
    state  = load_json_trades(TRADES_FILE_HL)
    trades = list(reversed(state["trades"]))[:50]
    stats  = compute_hl_stats(state["trades"])
    return state, stats, trades


def get_backtest_data():
    """Load crypto momentum backtest results from JSON."""
    empty = {
        "settings": {},
        "summary":  {"final": 0, "peak": 0, "total_return": 0, "peak_return": 0,
                     "cagr": 0, "max_dd": 0, "cash_periods": 0, "total_trades": 0},
        "trades":   [],
    }
    if not os.path.exists(TRADES_FILE_BACKTEST):
        return empty
    try:
        with open(TRADES_FILE_BACKTEST) as f:
            return json.load(f)
    except Exception:
        return empty


def get_hl_backtest_data():
    """Load HL perpetuals backtest results from JSON."""
    empty = {
        "settings": {},
        "summary":  {"final": 0, "peak": 0, "total_return": 0, "peak_return": 0,
                     "cagr": 0, "max_dd": 0, "total_funding": 0, "total_fees": 0,
                     "long_periods": 0, "short_periods": 0, "flat_periods": 0},
        "trades":   [],
    }
    if not os.path.exists(TRADES_FILE_HL_BT):
        return empty
    try:
        with open(TRADES_FILE_HL_BT) as f:
            return json.load(f)
    except Exception:
        return empty


def get_hl_live():
    """Load HL perps live paper trading state from JSON."""
    empty = {
        "started_at": None, "initial": 10000, "equity": 10000,
        "peak_equity": 10000, "leverage": 1.0, "status": "flat",
        "position": None, "last_check": None, "next_check": None,
        "signal": None, "trades": [],
        "total_funding_paid": 0.0, "total_fees_paid": 0.0,
    }
    if not os.path.exists(TRADES_FILE_HL_LIVE):
        return empty
    try:
        with open(TRADES_FILE_HL_LIVE) as f:
            return json.load(f)
    except Exception:
        return empty


def get_momentum_live():
    """Load live paper trading state from JSON."""
    empty = {
        "started_at": None, "initial": 10000, "equity": 10000,
        "peak_equity": 10000, "cash": 10000, "status": "cash",
        "position": None, "last_check": None, "next_check": None,
        "signal": None, "trades": [],
    }
    if not os.path.exists(TRADES_FILE_MOMENTUM):
        return empty
    try:
        with open(TRADES_FILE_MOMENTUM) as f:
            return json.load(f)
    except Exception:
        return empty


def get_scalper_live() -> dict:
    """Load scalper bot state — DB first, JSON fallback."""
    empty = {
        "equity": 25000, "capital": 25000, "leverage": 5,
        "position": None, "trades": [], "total_pnl": 0,
        "total_fees": 0, "peak_equity": 25000, "max_dd_pct": 0,
        "status": "flat", "last_check": None, "unrealized_pnl": 0,
        "current_price": None,
    }
    try:
        import db as _db
        db_state = _db.get_scalper_state()
        if db_state is not None:
            return db_state
    except Exception:
        pass
    if not os.path.exists(TRADES_FILE_SCALPER):
        return empty
    try:
        with open(TRADES_FILE_SCALPER) as f:
            return json.load(f)
    except Exception:
        return empty


def fmt_time(ts):
    if not ts:
        return "—"
    try:
        return datetime.utcfromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "—"


def fmt_move(t: dict) -> str:
    try:
        o, c = float(t["btc_at_open"]), float(t["btc_at_close"])
        if o > 0:
            return f"{(c - o) / o * 100:+.3f}%"
    except Exception:
        pass
    return "—"


# ── HTML template ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BTC Trader Dashboard</title>
  <meta http-equiv="refresh" content="30">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Courier New', monospace;
      background: #0d1117; color: #c9d1d9; min-height: 100vh; padding: 24px 16px;
    }
    .page-title {
      font-size: 1.4em; font-weight: 700; color: #58a6ff;
      display: flex; align-items: center; gap: 10px; margin-bottom: 4px;
    }
    .page-subtitle { color: #484f58; font-size: 0.85em; margin-bottom: 28px; }

    /* Tabs */
    .tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #21262d; }
    .tab {
      padding: 10px 20px; cursor: pointer; border-radius: 6px 6px 0 0;
      font-size: 0.9em; font-weight: 600; color: #8b949e;
      border: 1px solid transparent; border-bottom: none;
      transition: all 0.15s;
    }
    .tab.active { background: #161b22; color: #c9d1d9; border-color: #30363d; }
    .tab:hover:not(.active) { color: #c9d1d9; background: #161b22; }
    .tab-badge {
      display: inline-block; padding: 2px 7px; border-radius: 10px;
      font-size: 0.75em; font-weight: 700; margin-left: 6px;
    }
    .badge-live { background: #3d1f1f; color: #f85149; }
    .badge-paper { background: #1a2d1a; color: #3fb950; }

    .panel { display: none; }
    .panel.active { display: block; }

    /* Cards */
    .cards {
      display: grid; gap: 12px; margin-bottom: 24px;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    }
    .card {
      background: #161b22; border: 1px solid #30363d; border-radius: 8px;
      padding: 16px 14px; text-align: center;
    }
    .card .lbl {
      color: #8b949e; font-size: 0.72em; text-transform: uppercase;
      letter-spacing: 1px; margin-bottom: 6px;
    }
    .card .val { font-size: 1.55em; font-weight: 700; line-height: 1; }

    .pos  { color: #3fb950; }
    .neg  { color: #f85149; }
    .blue { color: #58a6ff; }

    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
    .grey { color: #8b949e; }

    /* Table */
    .section-hdr {
      font-size: 1em; font-weight: 600; color: #58a6ff;
      margin: 20px 0 10px; display: flex; align-items: center; gap: 8px;
    }
    .tbl-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #21262d; }
    table { width: 100%; border-collapse: collapse; font-size: 0.86em; }
    thead th {
      background: #161b22; color: #8b949e; text-align: left;
      padding: 10px 12px; font-size: 0.78em; text-transform: uppercase;
      letter-spacing: 0.8px; border-bottom: 1px solid #30363d;
      white-space: nowrap;
    }
    tbody td { padding: 9px 12px; border-bottom: 1px solid #161b22; white-space: nowrap; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover { background: #161b22; }

    .pill {
      display: inline-block; padding: 2px 8px; border-radius: 10px;
      font-size: 0.8em; font-weight: 700; line-height: 1.4;
    }
    .pill-win  { background: #1a2d1a; color: #3fb950; }
    .pill-loss { background: #2d1a1a; color: #f85149; }
    .pill-up   { background: #1b2a3b; color: #58a6ff; }
    .pill-down { background: #2b1f2e; color: #bc8cff; }

    .empty { color: #484f58; text-align: center; padding: 48px; font-size: 0.9em; }
    .updated { color: #484f58; font-size: 0.78em; margin-top: 20px; text-align: right; }

    @media (max-width: 640px) {
      .cards { grid-template-columns: repeat(2, 1fr); }
      .card .val { font-size: 1.3em; }
    }
  </style>
</head>
<body>

<div class="page-title">&#9889; BTC Trader Dashboard</div>
<p class="page-subtitle">Auto-refreshes every 30s &mdash; UTC times</p>

<div class="tabs">
  <div class="tab active" onclick="switchTab('fivemin', this)">
    5-Min Bot <span class="tab-badge badge-live">LIVE</span>
  </div>
  <div class="tab" onclick="switchTab('hourly', this)">
    Hourly Bot <span class="tab-badge badge-paper">PAPER</span>
  </div>
  <div class="tab" onclick="switchTab('hl', this)">
    Hyperliquid <span class="tab-badge badge-paper">PAPER</span>
  </div>
  <div class="tab" onclick="switchTab('backtest', this)">
    Crypto Backtest <span class="tab-badge" style="background:#1f2a1f;color:#d4b44a;">HIST</span>
  </div>
  <div class="tab" onclick="switchTab('momentum', this)">
    Momentum Bot <span class="tab-badge badge-paper">PAPER</span>
  </div>
  <div class="tab" onclick="switchTab('hlbacktest', this)">
    HL Perps <span class="tab-badge" style="background:#1f1a2e;color:#bc8cff;">HIST</span>
  </div>
  <div class="tab" onclick="switchTab('scalper', this)">
    ETH Scalper <span class="tab-badge badge-paper">PAPER</span>
  </div>
</div>

<!-- ═══ 5-MIN PANEL ═══════════════════════════════════════════════════════ -->
<div class="panel active" id="panel-fivemin">

  <div class="cards">
    <div class="card">
      <div class="lbl">Mode</div>
      <div class="val {{ 'neg' if m5_state.get('mode','paper') == 'live' else 'pos' }}">
        {{ (m5_state.get('mode','paper') or 'paper')|upper }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">Equity</div>
      <div class="val {{ 'pos' if m5_stats.get('total_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(m5_stats.get('total_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">Win Rate</div>
      <div class="val {{ 'pos' if m5_stats.get('win_rate',0) >= 0.55 else 'neg' }}">
        {{ "%.1f"|format(m5_stats.get('win_rate', 0) * 100) }}%
      </div>
    </div>
    <div class="card">
      <div class="lbl">Trades</div>
      <div class="val blue">{{ m5_stats.get('total', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Avg PnL</div>
      <div class="val {{ 'pos' if m5_stats.get('avg_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(m5_stats.get('avg_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">W / L</div>
      <div class="val grey">
        <span class="pos">{{ m5_stats.get('wins', 0) }}</span>
        /
        <span class="neg">{{ m5_stats.get('losses', 0) }}</span>
      </div>
    </div>
  </div>

  <div class="section-hdr">&#128202; Recent Trades (5-Min)</div>
  {% if m5_trades %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>Time (UTC)</th>
        <th>Market</th>
        <th>Dir</th>
        <th>Entry</th>
        <th>Edge</th>
        <th>BTC Move</th>
        <th>Result</th>
        <th>PnL</th>
      </tr>
    </thead>
    <tbody>
    {% for t in m5_trades %}
    <tr>
      <td>{{ fmt_time(t.get('opened_at')) }}</td>
      <td style="color:#484f58; max-width:200px; overflow:hidden; text-overflow:ellipsis">
        {{ t.get('market_slug','')[-20:] }}
      </td>
      <td>
        <span class="pill {{ 'pill-up' if t.get('direction')=='Up' else 'pill-down' }}">
          {{ t.get('direction','?') }}
        </span>
      </td>
      <td>{{ "%.3f"|format(t.get('entry_price', 0)) }}</td>
      <td class="{{ 'pos' if t.get('edge',0) > 0 else 'neg' }}">
        {{ "%+.3f"|format(t.get('edge', 0)) }}
      </td>
      <td class="{{ 'pos' if t.get('btc_at_close',0) >= t.get('btc_at_open',1) else 'neg' }}">
        {{ fmt_move(t) }}
      </td>
      <td>
        <span class="pill {{ 'pill-win' if t.get('won') else 'pill-loss' }}">
          {{ 'WIN' if t.get('won') else 'LOSS' }}
        </span>
      </td>
      <td class="{{ 'pos' if t.get('pnl',0) >= 0 else 'neg' }}">
        {{ "%+.2f"|format(t.get('pnl', 0)) }}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">No 5-min trades yet. Bot is watching for opportunities...</div>
  {% endif %}

  {% if m5_state.get('updated_at') %}
  <div class="updated">Last updated: {{ fmt_time(m5_state.get('updated_at')) }} UTC</div>
  {% endif %}
</div>

<!-- ═══ HOURLY PANEL ══════════════════════════════════════════════════════ -->
<div class="panel" id="panel-hourly">

  <div class="cards">
    <div class="card">
      <div class="lbl">Mode</div>
      <div class="val pos">PAPER</div>
    </div>
    <div class="card">
      <div class="lbl">Paper PnL</div>
      <div class="val {{ 'pos' if hr_stats.get('total_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(hr_stats.get('total_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">Win Rate</div>
      <div class="val {{ 'pos' if hr_stats.get('win_rate',0) >= 0.65 else ('grey' if hr_stats.get('win_rate',0) >= 0.55 else 'neg') }}">
        {{ "%.1f"|format(hr_stats.get('win_rate', 0) * 100) }}%
      </div>
    </div>
    <div class="card">
      <div class="lbl">Trades</div>
      <div class="val blue">{{ hr_stats.get('total', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Avg PnL</div>
      <div class="val {{ 'pos' if hr_stats.get('avg_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(hr_stats.get('avg_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">W / L</div>
      <div class="val grey">
        <span class="pos">{{ hr_stats.get('wins', 0) }}</span>
        /
        <span class="neg">{{ hr_stats.get('losses', 0) }}</span>
      </div>
    </div>
  </div>

  <!-- Backtest benchmark -->
  <div style="background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:20px; font-size:0.85em; color:#8b949e;">
    &#128200; <strong style="color:#c9d1d9;">Backtest target:</strong>
    5-min entry, 0.2% BTC move &rarr; <span style="color:#3fb950;">80% WR, +$12/trade</span>
    &nbsp;|&nbsp;
    0.3% move &rarr; <span style="color:#3fb950;">89% WR, +$14/trade</span>
  </div>

  <div class="section-hdr">&#128202; Recent Trades (Hourly)</div>
  {% if hr_trades %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>Time (UTC)</th>
        <th>Market</th>
        <th>Dir</th>
        <th>Entry</th>
        <th>Edge</th>
        <th>BTC Open</th>
        <th>BTC Close</th>
        <th>Move</th>
        <th>Result</th>
        <th>PnL</th>
      </tr>
    </thead>
    <tbody>
    {% for t in hr_trades %}
    <tr>
      <td>{{ fmt_time(t.get('opened_at')) }}</td>
      <td style="color:#484f58; font-size:0.82em; max-width:200px; overflow:hidden; text-overflow:ellipsis">
        {{ t.get('market_slug','') }}
      </td>
      <td>
        <span class="pill {{ 'pill-up' if t.get('direction')=='Up' else 'pill-down' }}">
          {{ t.get('direction','?') }}
        </span>
      </td>
      <td>{{ "%.3f"|format(t.get('entry_price', 0)) }}</td>
      <td class="{{ 'pos' if t.get('edge',0) > 0 else 'neg' }}">
        {{ "%+.3f"|format(t.get('edge', 0)) }}
      </td>
      <td>${{ "{:,.0f}".format(t.get('btc_at_open', 0)) }}</td>
      <td>${{ "{:,.0f}".format(t.get('btc_at_close', 0)) }}</td>
      <td class="{{ 'pos' if t.get('btc_at_close',0) >= t.get('btc_at_open',1) else 'neg' }}">
        {{ fmt_move(t) }}
      </td>
      <td>
        <span class="pill {{ 'pill-win' if t.get('won') else 'pill-loss' }}">
          {{ 'WIN' if t.get('won') else 'LOSS' }}
        </span>
      </td>
      <td class="{{ 'pos' if t.get('pnl',0) >= 0 else 'neg' }}">
        {{ "%+.2f"|format(t.get('pnl', 0)) }}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    No hourly trades yet.<br>
    Bot enters 5&ndash;12 min into the hour when BTC moves &ge;0.2%.
    Check back after the next full hour.
  </div>
  {% endif %}

  {% if hr_state.get('updated_at') %}
  <div class="updated">Last updated: {{ fmt_time(hr_state.get('updated_at')) }} UTC</div>
  {% endif %}
</div>

<!-- ═══ HYPERLIQUID PANEL ══════════════════════════════════════════════════ -->
<div class="panel" id="panel-hl">

  <div class="cards">
    <div class="card">
      <div class="lbl">Mode</div>
      <div class="val pos">PAPER</div>
    </div>
    <div class="card">
      <div class="lbl">Paper PnL</div>
      <div class="val {{ 'pos' if hl_stats.get('total_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(hl_stats.get('total_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">Win Rate</div>
      <div class="val {{ 'pos' if hl_stats.get('win_rate',0) >= 0.65 else ('grey' if hl_stats.get('win_rate',0) >= 0.55 else 'neg') }}">
        {{ "%.1f"|format(hl_stats.get('win_rate', 0) * 100) }}%
      </div>
    </div>
    <div class="card">
      <div class="lbl">Trades</div>
      <div class="val blue">{{ hl_stats.get('total', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Avg PnL</div>
      <div class="val {{ 'pos' if hl_stats.get('avg_pnl',0) >= 0 else 'neg' }}">
        ${{ "%.2f"|format(hl_stats.get('avg_pnl', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">W / L</div>
      <div class="val grey">
        <span class="pos">{{ hl_stats.get('wins', 0) }}</span>
        /
        <span class="neg">{{ hl_stats.get('losses', 0) }}</span>
      </div>
    </div>
  </div>

  <div style="background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:20px; font-size:0.85em; color:#8b949e;">
    &#9889; <strong style="color:#c9d1d9;">BTC-PERP Futures</strong>
    &nbsp;|&nbsp; 3x leverage &nbsp;|&nbsp; Stop 0.5% &nbsp;|&nbsp; Trail 0.3%
    &nbsp;|&nbsp; Same signal as hourly bot &mdash; enter 1&ndash;5 min into hour on 0.2%+ move
  </div>

  <div class="section-hdr">&#128202; Recent Trades (Hyperliquid)</div>
  {% if hl_trades %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>Time (UTC)</th>
        <th>Dir</th>
        <th>Entry</th>
        <th>Exit</th>
        <th>BTC Move</th>
        <th>Exit Reason</th>
        <th>Margin</th>
        <th>PnL</th>
      </tr>
    </thead>
    <tbody>
    {% for t in hl_trades %}
    {% set pnl = t.get('pnl_usd', 0) %}
    {% set ep = t.get('entry_price', 0) %}
    {% set xp = t.get('exit_price', 0) %}
    {% set move = ((xp - ep) / ep * 100) if ep > 0 else 0 %}
    {% set is_long = t.get('direction') == 'Long' %}
    <tr>
      <td>{{ fmt_time(t.get('opened_at')) }}</td>
      <td>
        <span class="pill {{ 'pill-up' if is_long else 'pill-down' }}">
          {{ t.get('direction','?') }}
        </span>
      </td>
      <td>${{ "{:,.0f}".format(ep) }}</td>
      <td>${{ "{:,.0f}".format(xp) }}</td>
      <td class="{{ 'pos' if (is_long and move >= 0) or (not is_long and move <= 0) else 'neg' }}">
        {{ "%+.3f"|format(move) }}%
      </td>
      <td style="color:#8b949e; font-size:0.85em;">{{ t.get('exit_reason','') }}</td>
      <td>${{ "%.0f"|format(t.get('size_usd', 0)) }}</td>
      <td class="{{ 'pos' if pnl >= 0 else 'neg' }}">
        {{ "%+.2f"|format(pnl) }}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    No Hyperliquid trades yet.<br>
    Bot enters 1&ndash;5 min into the hour when BTC moves &ge;0.2%.
  </div>
  {% endif %}

  {% if hl_state.get('updated_at') %}
  <div class="updated">Last updated: {{ fmt_time(hl_state.get('updated_at')) }} UTC</div>
  {% endif %}
</div>

<!-- ═══ CRYPTO BACKTEST PANEL ════════════════════════════════════════════ -->
<div class="panel" id="panel-backtest">

  {% set bt = bt_data %}
  {% set bs = bt.get('summary', {}) %}
  {% set bset = bt.get('settings', {}) %}

  <!-- Strategy info bar -->
  <div style="background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:20px; font-size:0.85em; color:#8b949e; line-height:1.7;">
    &#128200; <strong style="color:#c9d1d9;">Crypto Momentum Strategy</strong>
    &nbsp;&mdash;&nbsp;
    Universe: <span style="color:#58a6ff;">{{ ', '.join(bset.get('universe', [])) }}</span>
    &nbsp;|&nbsp;
    Period: <span style="color:#58a6ff;">{{ bset.get('period','monthly') }}</span>
    &nbsp;|&nbsp;
    Lookback: <span style="color:#58a6ff;">{{ bset.get('lookback', 3) }}mo</span>
    &nbsp;|&nbsp;
    Trend filter: <span style="color:#58a6ff;">{{ bset.get('trend_ma', 200) }}d MA</span>
    &nbsp;|&nbsp;
    Date range: <span style="color:#58a6ff;">{{ bset.get('start','') }} &rarr; {{ bset.get('end','') }}</span>
  </div>

  <!-- Summary cards -->
  <div class="cards" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
    <div class="card">
      <div class="lbl">Initial</div>
      <div class="val blue">${{ "{:,.0f}".format(bset.get('initial', 10000)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Final Equity</div>
      <div class="val pos">${{ "{:,.0f}".format(bs.get('final', 0)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Total Return</div>
      <div class="val pos">+{{ "{:,.0f}".format(bs.get('total_return', 0)) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Peak Equity</div>
      <div class="val" style="color:#d4b44a;">${{ "{:,.0f}".format(bs.get('peak', 0)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Peak Return</div>
      <div class="val" style="color:#d4b44a;">+{{ "{:,.0f}".format(bs.get('peak_return', 0)) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">CAGR</div>
      <div class="val pos">{{ "+{:.1f}".format(bs.get('cagr', 0)) }}%/yr</div>
    </div>
    <div class="card">
      <div class="lbl">Max Drawdown</div>
      <div class="val neg">-{{ "{:.1f}".format(bs.get('max_dd', 0)) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Rotations</div>
      <div class="val blue">{{ bs.get('total_trades', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Cash Periods</div>
      <div class="val grey">{{ bs.get('cash_periods', 0) }}</div>
    </div>
  </div>

  <div class="section-hdr">&#128202; Rotation History — All Trades</div>
  {% if bt.get('trades') %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Date</th>
        <th>Asset</th>
        <th>Buy Price</th>
        <th>Momentum</th>
        <th>Equity After</th>
        <th>Return vs Initial</th>
      </tr>
    </thead>
    <tbody>
    {% for t in bt.get('trades', []) %}
    {% set ret_pct = ((t.get('equity_after', 0) - bset.get('initial', 10000)) / bset.get('initial', 10000) * 100) if bset.get('initial', 10000) > 0 else 0 %}
    <tr>
      <td style="color:#484f58;">{{ loop.index }}</td>
      <td>{{ t.get('date','') }}</td>
      <td>
        {% set ticker = t.get('ticker','') %}
        {% if 'BTC' in ticker %}
          <span class="pill pill-up">&#8383; {{ t.get('name','') }}</span>
        {% elif 'ETH' in ticker %}
          <span class="pill" style="background:#1b1f3b;color:#9198ff;">&#9830; {{ t.get('name','') }}</span>
        {% elif 'SOL' in ticker %}
          <span class="pill" style="background:#1a2b2b;color:#14f195;">&#9670; {{ t.get('name','') }}</span>
        {% else %}
          <span class="pill pill-down">{{ t.get('name','') }}</span>
        {% endif %}
      </td>
      <td>${{ "{:,.2f}".format(t.get('price', 0)) }}</td>
      <td class="{{ 'pos' if t.get('momentum_pct', 0) >= 0 else 'neg' }}">
        {{ "%+.1f"|format(t.get('momentum_pct', 0)) }}%
      </td>
      <td class="pos" style="font-weight:600;">
        ${{ "{:,.0f}".format(t.get('equity_after', 0)) }}
      </td>
      <td class="{{ 'pos' if ret_pct >= 0 else 'neg' }}">
        {{ "{:+,.0f}".format(ret_pct) }}%
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    No backtest data found.<br>
    Run: <code style="color:#58a6ff;">python backtest_crypto.py --tickers BTC-USD,ETH-USD,SOL-USD --lookback 2 --top-k 1 --trend-ma 200 --period weekly --export</code>
  </div>
  {% endif %}

  <div class="updated">
    Backtest: {{ bset.get('start','') }} &rarr; {{ bset.get('end','') }}
    &nbsp;&mdash;&nbsp; Weekly rebalancing, {{ bset.get('trend_ma', 200) }}d MA bear filter
  </div>
</div>

<!-- ═══ MOMENTUM BOT PANEL ════════════════════════════════════════════════ -->
<div class="panel" id="panel-momentum">

  {% set ml = ml_data %}
  {% set ml_pos = ml.get('position') %}
  {% set ml_sig = ml.get('signal') or {} %}
  {% set ml_initial = ml.get('initial', 10000) %}
  {% set ml_equity  = ml.get('equity',  ml_initial) %}
  {% set ml_peak    = ml.get('peak_equity', ml_initial) %}
  {% set ml_ret     = ((ml_equity - ml_initial) / ml_initial * 100) if ml_initial > 0 else 0 %}
  {% set ml_peak_ret= ((ml_peak - ml_initial) / ml_initial * 100) if ml_initial > 0 else 0 %}

  <!-- Info bar -->
  <div style="background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:20px; font-size:0.85em; color:#8b949e; line-height:1.7;">
    &#9889; <strong style="color:#c9d1d9;">Live Paper Trading</strong>
    &nbsp;&mdash;&nbsp; BTC + ETH + SOL &nbsp;|&nbsp; Weekly rebalancing &nbsp;|&nbsp; 2mo lookback &nbsp;|&nbsp; 200d MA filter
    &nbsp;|&nbsp; Same strategy: +709,301% backtest (2016&ndash;2026)
    <br>
    Last check: <span style="color:#58a6ff;">{{ ml.get('last_check') or 'Never' }}</span>
    &nbsp;|&nbsp;
    Next check: <span style="color:#58a6ff;">{{ ml.get('next_check') or '—' }}</span>
  </div>

  <!-- Summary cards -->
  <div class="cards" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
    <div class="card">
      <div class="lbl">Status</div>
      {% if ml.get('status') == 'holding' and ml_pos %}
        <div class="val pos">HOLDING</div>
      {% else %}
        <div class="val grey">CASH</div>
      {% endif %}
    </div>
    <div class="card">
      <div class="lbl">Started</div>
      <div class="val blue" style="font-size:1.1em;">{{ ml.get('started_at') or '—' }}</div>
    </div>
    <div class="card">
      <div class="lbl">Initial</div>
      <div class="val blue">${{ "{:,.0f}".format(ml_initial) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Current Equity</div>
      <div class="val {{ 'pos' if ml_ret >= 0 else 'neg' }}">${{ "{:,.0f}".format(ml_equity) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Total Return</div>
      <div class="val {{ 'pos' if ml_ret >= 0 else 'neg' }}">{{ "{:+.1f}".format(ml_ret) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Peak Equity</div>
      <div class="val" style="color:#d4b44a;">${{ "{:,.0f}".format(ml_peak) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Rotations</div>
      <div class="val blue">{{ ml.get('trades', []) | selectattr('action', 'equalto', 'BUY') | list | length }}</div>
    </div>
  </div>

  <!-- Current position -->
  {% if ml_pos %}
  <div style="background:#0d2218; border:1px solid #1a4a2e; border-radius:8px; padding:16px; margin-bottom:20px;">
    <div class="section-hdr" style="margin:0 0 12px;">&#128200; Current Position</div>
    <div style="display:flex; gap:32px; flex-wrap:wrap; font-size:0.9em;">
      <div><span style="color:#8b949e;">Asset</span><br><strong style="color:#3fb950; font-size:1.2em;">{{ ml_pos.get('name','') }}</strong></div>
      <div><span style="color:#8b949e;">Entry Price</span><br><strong>${{ "{:,.2f}".format(ml_pos.get('entry_price', 0)) }}</strong></div>
      <div><span style="color:#8b949e;">Entry Date</span><br><strong>{{ ml_pos.get('entry_date','') }}</strong></div>
      <div><span style="color:#8b949e;">Units</span><br><strong>{{ "{:.6f}".format(ml_pos.get('units', 0)) }}</strong></div>
      <div><span style="color:#8b949e;">Entry Equity</span><br><strong>${{ "{:,.0f}".format(ml_pos.get('entry_equity', 0)) }}</strong></div>
    </div>
  </div>
  {% else %}
  <div style="background:#1a1a0d; border:1px solid #3a3a1a; border-radius:8px; padding:16px; margin-bottom:20px; font-size:0.9em; color:#8b949e;">
    &#9203; <strong style="color:#d4b44a;">Waiting for signal</strong>
    &nbsp;&mdash;&nbsp; All assets below 200d MA. Bot is in cash preserving capital until the bull market resumes.
  </div>
  {% endif %}

  <!-- Market status (latest signal) -->
  {% if ml_sig.get('details') %}
  <div class="section-hdr">&#127763; Current Market Status</div>
  <div class="cards" style="grid-template-columns: repeat(3, 1fr); margin-bottom:24px;">
    {% for ticker in ['BTC-USD', 'ETH-USD', 'SOL-USD'] %}
    {% set d = ml_sig.get('details', {}).get(ticker, {}) %}
    {% set is_up = d.get('uptrend', False) %}
    <div class="card" style="border-color: {{ '#1a4a2e' if is_up else '#4a1a1a' }};">
      <div class="lbl">{{ 'Bitcoin' if 'BTC' in ticker else ('Ethereum' if 'ETH' in ticker else 'Solana') }}</div>
      <div class="val {{ 'pos' if is_up else 'neg' }}" style="font-size:1.1em;">
        {% if d.get('price') %}${{ "{:,.0f}".format(d['price']) }}{% else %}—{% endif %}
      </div>
      <div style="font-size:0.75em; margin-top:6px; color:#8b949e;">
        MA200: {% if d.get('ma200') %}${{ "{:,.0f}".format(d['ma200']) }}{% else %}—{% endif %}
      </div>
      <div style="font-size:0.8em; margin-top:4px;" class="{{ 'pos' if d.get('momentum', 0) >= 0 else 'neg' }}">
        {{ "{:+.1f}".format(d.get('momentum', 0)) }}% momentum
      </div>
      <div style="font-size:0.72em; margin-top:6px; font-weight:700; letter-spacing:0.5px;"
           class="{{ 'pos' if is_up else 'neg' }}">
        {{ 'ABOVE MA &#10003;' if is_up else 'BELOW MA &#10007;' }}
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- Trade history -->
  {% set ml_buys = ml.get('trades', []) | selectattr('action', 'equalto', 'BUY') | list %}
  <div class="section-hdr">&#128202; Rotation History</div>
  {% if ml_buys %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Date</th>
        <th>Asset</th>
        <th>Buy Price</th>
        <th>Units</th>
        <th>Momentum</th>
        <th>Equity After</th>
        <th>Return vs Initial</th>
      </tr>
    </thead>
    <tbody>
    {% for t in ml_buys %}
    {% set ret_pct = ((t.get('equity_after', 0) - ml_initial) / ml_initial * 100) if ml_initial > 0 else 0 %}
    <tr>
      <td style="color:#484f58;">{{ loop.index }}</td>
      <td>{{ t.get('date','') }}</td>
      <td>
        {% set tn = t.get('ticker','') %}
        {% if 'BTC' in tn %}
          <span class="pill pill-up">&#8383; {{ t.get('name','') }}</span>
        {% elif 'ETH' in tn %}
          <span class="pill" style="background:#1b1f3b;color:#9198ff;">&#9830; {{ t.get('name','') }}</span>
        {% elif 'SOL' in tn %}
          <span class="pill" style="background:#1a2b2b;color:#14f195;">&#9670; {{ t.get('name','') }}</span>
        {% else %}
          <span class="pill pill-down">{{ t.get('name','') }}</span>
        {% endif %}
      </td>
      <td>${{ "{:,.2f}".format(t.get('price', 0)) }}</td>
      <td style="color:#8b949e; font-size:0.85em;">{{ "{:.6f}".format(t.get('units', 0)) }}</td>
      <td class="{{ 'pos' if t.get('momentum_pct', 0) >= 0 else 'neg' }}">
        {{ "%+.1f"|format(t.get('momentum_pct', 0)) }}%
      </td>
      <td class="pos" style="font-weight:600;">${{ "{:,.0f}".format(t.get('equity_after', 0)) }}</td>
      <td class="{{ 'pos' if ret_pct >= 0 else 'neg' }}">
        {{ "{:+,.0f}".format(ret_pct) }}%
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    No rotations yet &mdash; bot is in cash waiting for the 200d MA signal.<br>
    <span style="font-size:0.85em; color:#484f58; margin-top:8px; display:block;">
      Run <code style="color:#58a6ff;">python crypto_momentum_bot.py</code> weekly to check for signals.
    </span>
  </div>
  {% endif %}

  <div class="updated">
    Paper trading started {{ ml.get('started_at') or '—' }} &nbsp;&mdash;&nbsp;
    Signal updates every 7 days
  </div>
</div>

<!-- ═══ HL PERPS BACKTEST PANEL ══════════════════════════════════════════ -->
<div class="panel" id="panel-hlbacktest">

  <!-- ── Live paper trading status ── -->
  {% set live = hl_live_data %}
  {% set live_pos = live.get('position') %}
  {% set live_sig = live.get('signal') or {} %}
  {% set live_initial = live.get('initial', 10000) %}
  {% set live_equity  = live.get('equity', live_initial) %}
  {% set live_peak    = live.get('peak_equity', live_initial) %}
  {% set live_lev     = live.get('leverage', 1.0) %}
  {% set live_ret     = ((live_equity - live_initial) / live_initial * 100) if live_initial > 0 else 0 %}

  <div style="background:#1a1a2e; border:1px solid #4a3a6e; border-radius:8px; padding:16px; margin-bottom:20px;">
    <div style="font-size:0.95em; font-weight:700; color:#bc8cff; margin-bottom:12px;">
      &#9889; Live Paper Trading &nbsp;&mdash;&nbsp;
      <span style="font-weight:400; color:#8b949e; font-size:0.85em;">
        BTC + ETH + SOL &nbsp;|&nbsp; Weekly &nbsp;|&nbsp; 2mo momentum &nbsp;|&nbsp; 200d MA &nbsp;|&nbsp; {{ live_lev }}x leverage
      </span>
    </div>
    <div style="display:flex; gap:20px; flex-wrap:wrap; align-items:flex-start;">
      <!-- Status cards -->
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        <div class="card" style="min-width:110px; padding:10px 12px;">
          <div class="lbl">Status</div>
          {% if live.get('status') == 'long' %}
            <div class="val pos" style="font-size:1.2em;">LONG</div>
          {% elif live.get('status') == 'short' %}
            <div class="val" style="font-size:1.2em;color:#bc8cff;">SHORT</div>
          {% else %}
            <div class="val grey" style="font-size:1.2em;">FLAT</div>
          {% endif %}
        </div>
        <div class="card" style="min-width:110px; padding:10px 12px;">
          <div class="lbl">Equity</div>
          <div class="val {{ 'pos' if live_ret >= 0 else 'neg' }}" style="font-size:1.2em;">
            ${{ "{:,.0f}".format(live_equity) }}
          </div>
        </div>
        <div class="card" style="min-width:110px; padding:10px 12px;">
          <div class="lbl">Return</div>
          <div class="val {{ 'pos' if live_ret >= 0 else 'neg' }}" style="font-size:1.2em;">
            {{ "{:+.1f}".format(live_ret) }}%
          </div>
        </div>
        <div class="card" style="min-width:110px; padding:10px 12px;">
          <div class="lbl">Peak</div>
          <div class="val" style="color:#d4b44a; font-size:1.2em;">
            ${{ "{:,.0f}".format(live_peak) }}
          </div>
        </div>
        <div class="card" style="min-width:110px; padding:10px 12px;">
          <div class="lbl">Next Check</div>
          <div class="val blue" style="font-size:0.95em;">{{ live.get('next_check') or '—' }}</div>
        </div>
      </div>
      <!-- Current position or flat notice -->
      {% if live_pos %}
      <div style="background:#0d2218; border:1px solid #1a4a2e; border-radius:6px; padding:10px 14px; font-size:0.85em; flex:1; min-width:200px;">
        <div style="color:#8b949e; margin-bottom:6px; font-size:0.78em; text-transform:uppercase; letter-spacing:0.8px;">Current Position</div>
        <strong style="color:#3fb950; font-size:1.1em;">{{ live_pos.get('direction','') }} {{ live_pos.get('name','') }}</strong><br>
        <span style="color:#8b949e;">Entry: </span><strong>${{ "{:,.4f}".format(live_pos.get('entry_price', 0)) }}</strong>
        &nbsp;&nbsp;<span style="color:#8b949e;">on </span><strong>{{ live_pos.get('entry_date','') }}</strong>
      </div>
      {% else %}
      <div style="background:#1a1a0d; border:1px solid #3a3a1a; border-radius:6px; padding:10px 14px; font-size:0.85em; flex:1; min-width:200px; color:#8b949e;">
        &#9203; <strong style="color:#d4b44a;">FLAT</strong> — All assets below 200d MA.<br>
        Capital preserved until bull market resumes.
      </div>
      {% endif %}
    </div>
    <!-- Market signals -->
    {% if live_sig.get('details') %}
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:12px;">
      {% for ticker in ['BTC-USD', 'ETH-USD', 'SOL-USD'] %}
      {% set d = live_sig.get('details', {}).get(ticker, {}) %}
      {% set is_up = d.get('uptrend', False) %}
      <div style="background:#0d1117; border:1px solid {{ '#1a4a2e' if is_up else '#4a1a1a' }}; border-radius:6px; padding:6px 12px; font-size:0.8em; display:flex; align-items:center; gap:10px;">
        <span class="{{ 'pos' if is_up else 'neg' }}" style="font-weight:700;">
          {{ 'BTC' if 'BTC' in ticker else ('ETH' if 'ETH' in ticker else 'SOL') }}
        </span>
        {% if d.get('price') %}
        <span>${{ "{:,.0f}".format(d['price']) }}</span>
        {% endif %}
        <span class="{{ 'pos' if d.get('momentum', 0) >= 0 else 'neg' }}">
          {{ "{:+.1f}".format(d.get('momentum', 0)) }}%
        </span>
        <span class="{{ 'pos' if is_up else 'neg' }}" style="font-size:0.85em;">
          {{ '▲ ABOVE MA' if is_up else '▼ BELOW MA' }}
        </span>
      </div>
      {% endfor %}
    </div>
    {% endif %}
    <div style="color:#484f58; font-size:0.75em; margin-top:8px;">
      Run <code style="color:#bc8cff;">python hl_momentum_bot.py</code> to check signal
      &nbsp;|&nbsp; Started {{ live.get('started_at') or 'Not yet started' }}
      &nbsp;|&nbsp; Last check: {{ live.get('last_check') or '—' }}
    </div>
  </div>

  {% set hl = hl_bt_data %}
  {% set hls = hl.get('summary', {}) %}
  {% set hlset = hl.get('settings', {}) %}

  <!-- Strategy info bar -->
  <div style="background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; margin-bottom:20px; font-size:0.85em; color:#8b949e; line-height:1.7;">
    &#128200; <strong style="color:#c9d1d9;">Hyperliquid Perpetuals Strategy</strong>
    &nbsp;&mdash;&nbsp;
    Universe: <span style="color:#bc8cff;">{{ ', '.join(hlset.get('universe', [])) }}</span>
    &nbsp;|&nbsp;
    Leverage: <span style="color:#bc8cff;">{{ hlset.get('leverage', 1.0) }}x</span>
    &nbsp;|&nbsp;
    Lookback: <span style="color:#bc8cff;">{{ hlset.get('lookback', 2) }}mo</span>
    &nbsp;|&nbsp;
    Bear market: <span style="color:#bc8cff;">{{ 'SHORT weakest' if hlset.get('short_bear', True) else 'Go flat' }}</span>
    &nbsp;|&nbsp;
    Funding: <span style="color:#bc8cff;">{{ "{:.4f}".format((hlset.get('funding_rate_8h', 0.00005)) * 100) }}%/8hr</span>
    &nbsp;|&nbsp;
    Date range: <span style="color:#bc8cff;">{{ hlset.get('start','') }} &rarr; {{ hlset.get('end','') }}</span>
  </div>

  <!-- Summary cards -->
  <div class="cards" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
    <div class="card">
      <div class="lbl">Initial</div>
      <div class="val blue">${{ "{:,.0f}".format(hlset.get('initial', 10000)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Final Equity</div>
      <div class="val pos">${{ "{:,.0f}".format(hls.get('final', 0)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Total Return</div>
      <div class="val pos">+{{ "{:,.0f}".format(hls.get('total_return', 0)) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Peak Equity</div>
      <div class="val" style="color:#d4b44a;">${{ "{:,.0f}".format(hls.get('peak', 0)) }}</div>
    </div>
    <div class="card">
      <div class="lbl">CAGR</div>
      <div class="val pos">{{ "+{:.1f}".format(hls.get('cagr', 0)) }}%/yr</div>
    </div>
    <div class="card">
      <div class="lbl">Max Drawdown</div>
      <div class="val neg">-{{ "{:.1f}".format(hls.get('max_dd', 0)) }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Long Periods</div>
      <div class="val pos">{{ hls.get('long_periods', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Short Periods</div>
      <div class="val" style="color:#bc8cff;">{{ hls.get('short_periods', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Flat Periods</div>
      <div class="val grey">{{ hls.get('flat_periods', 0) }}</div>
    </div>
    <div class="card">
      <div class="lbl">Funding Net</div>
      <div class="val {{ 'pos' if hls.get('total_funding', 0) >= 0 else 'neg' }}">
        ${{ "{:,.0f}".format(hls.get('total_funding', 0)) }}
      </div>
    </div>
    <div class="card">
      <div class="lbl">Total Fees</div>
      <div class="val neg">-${{ "{:,.0f}".format(hls.get('total_fees', 0)) }}</div>
    </div>
  </div>

  <div class="section-hdr">&#128202; Trade Log — All Positions</div>
  {% set hl_trades = hl.get('trades', []) | selectattr('action', 'in', ['LONG', 'SHORT']) | list %}
  {% if hl_trades %}
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Status</th>
        <th>Date</th>
        <th>Dir</th>
        <th>Asset</th>
        <th>Entry</th>
        <th>Exit</th>
        <th>Days</th>
        <th>Momentum</th>
        <th>PnL</th>
        <th>Equity After</th>
        <th>Return vs Initial</th>
      </tr>
    </thead>
    <tbody>
    {% for t in hl_trades %}
    {% set ret_pct = ((t.get('equity_after', 0) - hlset.get('initial', 10000)) / hlset.get('initial', 10000) * 100) if hlset.get('initial', 10000) > 0 else 0 %}
    {% set is_open = t.get('exit_price') is none %}
    <tr>
      <td style="color:#484f58;">{{ loop.index }}</td>
      <td>
        {% if is_open %}
          <span class="pill" style="background:#1a2d1a;color:#3fb950;animation:pulse 2s infinite;">● OPEN</span>
        {% else %}
          <span class="pill" style="background:#1e1e1e;color:#484f58;">✓ CLOSED</span>
        {% endif %}
      </td>
      <td>{{ t.get('date','') }}</td>
      <td>
        {% if t.get('direction') == 'LONG' %}
          <span class="pill pill-up">&#8593; LONG</span>
        {% elif t.get('direction') == 'SHORT' %}
          <span class="pill" style="background:#2b1a2e;color:#bc8cff;">&#8595; SHORT</span>
        {% else %}
          <span class="pill pill-loss">FLAT</span>
        {% endif %}
      </td>
      <td>
        {% set ticker = t.get('ticker','') %}
        {% if 'BTC' in ticker %}
          <span class="pill pill-up">&#8383; {{ t.get('name','') }}</span>
        {% elif 'ETH' in ticker %}
          <span class="pill" style="background:#1b1f3b;color:#9198ff;">&#9830; {{ t.get('name','') }}</span>
        {% elif 'SOL' in ticker %}
          <span class="pill" style="background:#1a2b2b;color:#14f195;">&#9670; {{ t.get('name','') }}</span>
        {% else %}
          <span class="pill pill-down">{{ t.get('name','') }}</span>
        {% endif %}
      </td>
      <td>{% if t.get('entry_price') %}${{ "{:,.2f}".format(t['entry_price']) }}{% else %}—{% endif %}</td>
      <td>{% if t.get('exit_price') %}${{ "{:,.2f}".format(t['exit_price']) }}{% else %}<span style="color:#484f58;">open</span>{% endif %}</td>
      <td style="color:#8b949e;">{{ t.get('days_held', 0) }}</td>
      <td class="{{ 'pos' if t.get('momentum_pct', 0) >= 0 else 'neg' }}">
        {{ "%+.1f"|format(t.get('momentum_pct', 0)) }}%
      </td>
      <td class="{{ 'pos' if t.get('pnl', 0) >= 0 else 'neg' }}">
        {% if t.get('pnl') %}{{ "{:+,.0f}".format(t['pnl']) }}{% else %}—{% endif %}
      </td>
      <td class="{{ 'pos' if t.get('equity_after', 0) > hlset.get('initial', 10000) else 'grey' }}" style="font-weight:600;">
        ${{ "{:,.0f}".format(t.get('equity_after', 0)) }}
      </td>
      <td class="{{ 'pos' if ret_pct >= 0 else 'neg' }}">
        {{ "{:+,.0f}".format(ret_pct) }}%
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    No HL backtest data found.<br>
    Run: <code style="color:#bc8cff;">python backtest_hl_momentum.py --export</code>
  </div>
  {% endif %}

  <div class="updated">
    Backtest: {{ hlset.get('start','') }} &rarr; {{ hlset.get('end','') }}
    &nbsp;&mdash;&nbsp; Weekly rebalancing, {{ hlset.get('trend_ma', 200) }}d MA, LONG bull / SHORT bear
  </div>
</div>

<!-- ═══ ETH SCALPER PANEL ══════════════════════════════════════════════════ -->
<div class="panel" id="panel-scalper">
{% set sc = sc_data %}
{% set sc_trades = sc.get('trades', []) %}
{% set sc_equity = sc.get('equity', sc.get('capital', 25000)) %}
{% set sc_capital = sc.get('capital', 25000) %}
{% set sc_ret = (sc_equity - sc_capital) / sc_capital * 100 if sc_capital else 0 %}
{% set sc_n = sc_trades | length %}
{% set sc_wins = sc_trades | selectattr('pnl_usd', '>', 0) | list | length %}
{% set sc_wr = (sc_wins / sc_n * 100) if sc_n > 0 else 0 %}
{% set sc_dd = sc.get('max_dd_pct', 0) * 100 %}
{% set sc_pnl = sc.get('total_pnl', 0) %}
{% set sc_pos = sc.get('position') %}

  <!-- Live status bar -->
  <div style="background:#0f1a0f;border:1px solid #1d4a1d;border-radius:8px;padding:12px 18px;margin-bottom:18px;display:flex;gap:24px;flex-wrap:wrap;align-items:center;">
    <span style="color:#aaa;font-size:12px;">ETH SCALPER</span>
    <span style="color:#d4b44a;font-weight:600;">Equity: ${{ "{:,.2f}".format(sc_equity) }}</span>
    <span style="color:{% if sc_ret >= 0 %}#4fc97e{% else %}#e05252{% endif %};">{{ "{:+.2f}".format(sc_ret) }}%</span>
    <span style="color:#aaa;">Trades: {{ sc_n }}{% if sc_n %} (WR {{ "{:.0f}".format(sc_wr) }}%){% endif %}</span>
    <span style="color:#e05252;">MaxDD: -{{ "{:.1f}".format(sc_dd) }}%</span>
    <span style="color:#aaa;">${{ "{:,.0f}".format(sc_capital) }} &times; {{ sc.get('leverage', 5) }}x leverage</span>
    {% if sc_pos %}
      {% set d = sc_pos.get('direction', 0) %}
      {% set dir_label = 'LONG' if d == 1 else 'SHORT' %}
      {% set unr = sc.get('unrealized_pnl', 0) %}
      <span style="color:{% if d == 1 %}#4fc97e{% else %}#e05252{% endif %};font-weight:600;">
        &#9679; {{ dir_label }} @ {{ "{:,.1f}".format(sc_pos.get('entry_price', 0)) }}
        &nbsp;SL={{ "{:,.1f}".format(sc_pos.get('stop', 0)) }}
        &nbsp;TP={{ "{:,.1f}".format(sc_pos.get('target', 0)) }}
        &nbsp;UnrPnL=${{ "{:+.0f}".format(unr) }}
      </span>
    {% else %}
      <span style="color:#777;">&#9675; FLAT — waiting for EMA pullback signal</span>
    {% endif %}
    <span style="color:#555;font-size:11px;margin-left:auto;">{{ sc.get('last_check', '')[:19] }} UTC</span>
  </div>

  <!-- Strategy info cards -->
  <div class="cards">
    <div class="card">
      <div class="lbl">Strategy</div>
      <div class="val" style="font-size:13px;">Pullback to EMA</div>
      <div style="color:#777;font-size:11px;margin-top:4px;">EMA21/100 + RSI14</div>
    </div>
    <div class="card">
      <div class="lbl">Signal Logic</div>
      <div class="val" style="font-size:11px;line-height:1.6;">
        Long: price &le; EMA21, RSI cross &uarr;45<br>
        Short: price &ge; EMA21, RSI cross &darr;55
      </div>
    </div>
    <div class="card">
      <div class="lbl">Risk / Reward</div>
      <div class="val">1 : 3.0</div>
      <div style="color:#777;font-size:11px;margin-top:4px;">SL = swing &#177;0.1&times;ATR</div>
    </div>
    <div class="card">
      <div class="lbl">Backtest Edge</div>
      <div class="val" style="color:#4fc97e;">$647/day avg</div>
      <div style="color:#777;font-size:11px;margin-top:4px;">37.1% WR, -21% MaxDD</div>
    </div>
    <div class="card">
      <div class="lbl">Total P&amp;L</div>
      <div class="val" style="color:{% if sc_pnl >= 0 %}#4fc97e{% else %}#e05252{% endif %};">${{ "{:+,.2f}".format(sc_pnl) }}</div>
    </div>
  </div>

  <!-- Trade log -->
  {% if sc_trades %}
  <table>
    <thead>
      <tr>
        <th>Status</th>
        <th>Dir</th>
        <th>Entry Time</th>
        <th>Exit Time</th>
        <th>Entry</th>
        <th>Exit</th>
        <th>Reason</th>
        <th>Bars</th>
        <th>P&amp;L $</th>
        <th>Equity</th>
      </tr>
    </thead>
    <tbody>
      {% for t in sc_trades | reverse %}
      <tr class="{% if t.pnl_usd > 0 %}win{% else %}lose{% endif %}">
        <td><span style="color:#4fc97e;font-size:11px;">&#10003; CLOSED</span></td>
        <td style="color:{% if t.direction == 'long' %}#4fc97e{% else %}#e05252{% endif %};">{{ t.direction | upper }}</td>
        <td>{{ t.entry_time[:16] }}</td>
        <td>{{ t.exit_time[:16] }}</td>
        <td>${{ "{:,.1f}".format(t.entry_price) }}</td>
        <td>${{ "{:,.1f}".format(t.exit_price) }}</td>
        <td style="color:{% if t.exit_reason == 'TP' %}#4fc97e{% elif t.exit_reason == 'SL' %}#e05252{% else %}#d4b44a{% endif %};">{{ t.exit_reason }}</td>
        <td>{{ t.get('bars_held', '—') }}</td>
        <td style="color:{% if t.pnl_usd > 0 %}#4fc97e{% else %}#e05252{% endif %};">${{ "{:+,.2f}".format(t.pnl_usd) }}</td>
        <td>${{ "{:,.2f}".format(t.get('equity_after', 0)) }}</td>
      </tr>
      {% endfor %}
      {% if sc_pos %}
      <tr style="opacity:0.7;">
        <td><span style="color:#d4b44a;font-size:11px;animation:pulse 1.5s infinite;">&#9679; OPEN</span></td>
        <td style="color:{% if sc_pos.direction == 1 %}#4fc97e{% else %}#e05252{% endif %};">{{ 'LONG' if sc_pos.direction == 1 else 'SHORT' }}</td>
        <td>{{ sc_pos.entry_time[:16] }}</td>
        <td>—</td>
        <td>${{ "{:,.1f}".format(sc_pos.entry_price) }}</td>
        <td>{{ "{:,.1f}".format(sc.get('current_price', 0)) }}</td>
        <td>—</td>
        <td>—</td>
        <td style="color:{% if sc.get('unrealized_pnl', 0) >= 0 %}#4fc97e{% else %}#e05252{% endif %};">${{ "{:+,.2f}".format(sc.get('unrealized_pnl', 0)) }}</td>
        <td>${{ "{:,.2f}".format(sc_equity) }}</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    No scalper trades yet.<br>
    Run: <code style="color:#4fc97e;">python scalper_bot.py --daemon --capital 25000 --leverage 5</code>
  </div>
  {% endif %}

  <div class="updated">
    Strategy: Pullback to EMA (EMA21/100, RSI14, ATR14) &mdash;
    TP 3R, SL swing&#177;0.1&times;ATR, Timeout 30 bars (2.5h) &mdash;
    Fee: +0.02% maker entry, -0.05% taker exit
  </div>
</div>

<script>
function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
}
</script>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    m5_state, m5_stats, m5_trades = get_5m_data()
    hr_state, hr_stats, hr_trades = get_hourly_data()
    hl_state, hl_stats, hl_trades = get_hl_data()
    bt_data      = get_backtest_data()
    hl_bt_data   = get_hl_backtest_data()
    hl_live_data = get_hl_live()
    ml_data      = get_momentum_live()
    sc_data      = get_scalper_live()
    return render_template_string(
        DASHBOARD_HTML,
        m5_state=m5_state, m5_stats=m5_stats, m5_trades=m5_trades,
        hr_state=hr_state, hr_stats=hr_stats, hr_trades=hr_trades,
        hl_state=hl_state, hl_stats=hl_stats, hl_trades=hl_trades,
        bt_data=bt_data, hl_bt_data=hl_bt_data, hl_live_data=hl_live_data,
        ml_data=ml_data, sc_data=sc_data,
        fmt_time=fmt_time, fmt_move=fmt_move,
    )


@app.route("/api/5m")
def api_5m():
    state, stats, trades = get_5m_data()
    return jsonify({"state": state, "stats": stats, "trades": trades})


@app.route("/api/hourly")
def api_hourly():
    state, stats, trades = get_hourly_data()
    return jsonify({"state": state, "stats": stats, "trades": trades})


@app.route("/api/hl")
def api_hl():
    state, stats, trades = get_hl_data()
    return jsonify({"state": state, "stats": stats, "trades": trades})


@app.route("/api/backtest")
def api_backtest():
    return jsonify(get_backtest_data())


@app.route("/api/momentum")
def api_momentum():
    return jsonify(get_momentum_live())


@app.route("/api/hl-backtest")
def api_hl_backtest():
    return jsonify(get_hl_backtest_data())


@app.route("/api/scalper")
def api_scalper():
    return jsonify(get_scalper_live())


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

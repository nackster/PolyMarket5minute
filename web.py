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

TRADES_FILE_5M     = "trades/real_trades.json"
TRADES_FILE_HOURLY = "trades/real_trades_hourly.json"
TRADES_FILE_HL     = "trades/hl_trades.json"


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
    .tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #21262d; }
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
    return render_template_string(
        DASHBOARD_HTML,
        m5_state=m5_state, m5_stats=m5_stats, m5_trades=m5_trades,
        hr_state=hr_state, hr_stats=hr_stats, hr_trades=hr_trades,
        hl_state=hl_state, hl_stats=hl_stats, hl_trades=hl_trades,
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


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

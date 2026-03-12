"""Flask web dashboard for monitoring trades.

Provides a simple web UI to check bot status, trade history, and stats.
Runs as the 'web' dyno on Heroku.
"""

import os
from datetime import datetime

from flask import Flask, jsonify, render_template_string

from db import get_bot_state, get_trades, get_stats

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BTC 5-Min Trader</title>
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
            background: #0d1117; color: #c9d1d9; padding: 20px;
        }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { color: #58a6ff; margin-bottom: 5px; font-size: 1.5em; }
        .subtitle { color: #8b949e; margin-bottom: 20px; font-size: 0.9em; }
        .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
        .card {
            background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            padding: 16px; text-align: center;
        }
        .card .label { color: #8b949e; font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px; }
        .card .value { font-size: 1.6em; font-weight: bold; margin-top: 4px; }
        .positive { color: #3fb950; }
        .negative { color: #f85149; }
        .neutral { color: #58a6ff; }
        .mode-live { color: #f85149; font-weight: bold; }
        .mode-paper { color: #3fb950; }
        table { width: 100%; border-collapse: collapse; margin-top: 16px; }
        th { background: #161b22; color: #8b949e; text-align: left; padding: 10px 12px;
             font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px;
             border-bottom: 1px solid #30363d; }
        td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.9em; }
        tr:hover { background: #161b22; }
        .won { color: #3fb950; font-weight: bold; }
        .lost { color: #f85149; font-weight: bold; }
        .section-title { color: #58a6ff; margin: 24px 0 8px; font-size: 1.1em; }
        .updated { color: #484f58; font-size: 0.8em; margin-top: 16px; text-align: center; }
        .no-data { color: #8b949e; text-align: center; padding: 40px; }
        @media (max-width: 600px) {
            .cards { grid-template-columns: repeat(2, 1fr); }
            td, th { padding: 6px 8px; font-size: 0.8em; }
        }
    </style>
</head>
<body>
<div class="container">
    <h1>&#9889; BTC 5-Min Trader</h1>
    <p class="subtitle">Polymarket btc-updown-5m markets &mdash; auto-refreshes every 30s</p>

    {% if state %}
    <div class="cards">
        <div class="card">
            <div class="label">Mode</div>
            <div class="value {{ 'mode-live' if state.mode == 'live' else 'mode-paper' }}">
                {{ state.mode|upper }}
            </div>
        </div>
        <div class="card">
            <div class="label">Equity</div>
            <div class="value {{ 'positive' if state.equity >= 0 else 'negative' }}">
                ${{ "%.2f"|format(state.equity) }}
            </div>
        </div>
        <div class="card">
            <div class="label">Peak Equity</div>
            <div class="value positive">${{ "%.2f"|format(state.peak_equity) }}</div>
        </div>
        <div class="card">
            <div class="label">Max Drawdown</div>
            <div class="value negative">${{ "%.2f"|format(state.max_drawdown) }}</div>
        </div>
    </div>
    {% endif %}

    {% if stats %}
    <div class="cards">
        <div class="card">
            <div class="label">Total Trades</div>
            <div class="value neutral">{{ stats.total }}</div>
        </div>
        <div class="card">
            <div class="label">Win Rate</div>
            <div class="value {{ 'positive' if stats.win_rate >= 0.5 else 'negative' }}">
                {{ "%.1f"|format(stats.win_rate * 100) }}%
            </div>
        </div>
        <div class="card">
            <div class="label">Total PnL</div>
            <div class="value {{ 'positive' if stats.total_pnl >= 0 else 'negative' }}">
                ${{ "%.2f"|format(stats.total_pnl) }}
            </div>
        </div>
        <div class="card">
            <div class="label">Avg PnL/Trade</div>
            <div class="value {{ 'positive' if stats.avg_pnl >= 0 else 'negative' }}">
                ${{ "%.2f"|format(stats.avg_pnl) }}
            </div>
        </div>
    </div>
    {% endif %}

    <h2 class="section-title">Recent Trades</h2>
    {% if trades %}
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Dir</th>
                <th>Entry</th>
                <th>Edge</th>
                <th>Result</th>
                <th>PnL</th>
                <th>BTC Move</th>
            </tr>
        </thead>
        <tbody>
        {% for t in trades %}
            <tr>
                <td>{{ format_time(t.opened_at) }}</td>
                <td>{{ t.direction }}</td>
                <td>{{ "%.3f"|format(t.entry_price) }}</td>
                <td>{{ "%.3f"|format(t.edge) }}</td>
                <td class="{{ 'won' if t.won else 'lost' }}">{{ 'WIN' if t.won else 'LOSS' }}</td>
                <td class="{{ 'positive' if t.pnl >= 0 else 'negative' }}">
                    ${{ "%.2f"|format(t.pnl) }}
                </td>
                <td>
                    {% set move = ((t.btc_at_close - t.btc_at_open) / t.btc_at_open * 100) if t.btc_at_open > 0 else 0 %}
                    {{ "%+.3f"|format(move) }}%
                </td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="no-data">No trades yet. Bot is running and watching for opportunities...</div>
    {% endif %}

    {% if state and state.updated_at %}
    <div class="updated">Last updated: {{ format_time(state.updated_at) }} UTC</div>
    {% endif %}
</div>
</body>
</html>
"""


def format_time(ts):
    """Format unix timestamp to readable string."""
    try:
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "N/A"


@app.route("/")
def dashboard():
    state = get_bot_state()
    stats = get_stats()
    trades_raw = get_trades(limit=50)

    # Convert dicts to objects for easier template access
    class Obj:
        def __init__(self, d):
            self.__dict__.update(d)

    state_obj = Obj(state) if state else None
    stats_obj = Obj(stats) if stats else None
    trade_objs = [Obj(t) for t in trades_raw]

    return render_template_string(
        DASHBOARD_HTML,
        state=state_obj,
        stats=stats_obj,
        trades=trade_objs,
        format_time=format_time,
    )


@app.route("/api/state")
def api_state():
    state = get_bot_state()
    return jsonify(state or {"error": "no data"})


@app.route("/api/trades")
def api_trades():
    trades = get_trades(limit=100)
    return jsonify(trades)


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    return jsonify(stats or {"error": "no data"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

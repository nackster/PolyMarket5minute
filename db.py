"""PostgreSQL trade storage for Heroku deployment.

Uses DATABASE_URL env var (auto-set by Heroku Postgres addon).
Falls back to local JSON file if no database is configured.
"""

import json
import os
import time
from dataclasses import asdict

import structlog

log = structlog.get_logger()

# Heroku sets DATABASE_URL automatically when you add Postgres addon
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Heroku uses postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_connection():
    """Get a psycopg2 connection."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Create trades table if it doesn't exist."""
    if not DATABASE_URL:
        log.info("no_database_url", msg="Skipping DB init, using JSON fallback")
        return False

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                opened_at DOUBLE PRECISION NOT NULL,
                resolved_at DOUBLE PRECISION NOT NULL,
                direction VARCHAR(10) NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                btc_at_entry DOUBLE PRECISION NOT NULL,
                btc_at_open DOUBLE PRECISION NOT NULL,
                btc_at_close DOUBLE PRECISION NOT NULL,
                size DOUBLE PRECISION NOT NULL,
                edge DOUBLE PRECISION NOT NULL,
                won BOOLEAN NOT NULL,
                pnl DOUBLE PRECISION NOT NULL,
                mode VARCHAR(10) NOT NULL,
                market_slug VARCHAR(100) NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                equity DOUBLE PRECISION DEFAULT 0,
                peak_equity DOUBLE PRECISION DEFAULT 0,
                max_drawdown DOUBLE PRECISION DEFAULT 0,
                mode VARCHAR(10) DEFAULT 'paper',
                updated_at DOUBLE PRECISION,
                CHECK (id = 1)
            );

            INSERT INTO bot_state (id, equity, peak_equity, max_drawdown, mode, updated_at)
            VALUES (1, 0, 0, 0, 'paper', %s)
            ON CONFLICT (id) DO NOTHING;
        """, (time.time(),))
        conn.commit()
        cur.close()
        conn.close()
        log.info("db_initialized")
        return True
    except Exception as e:
        log.error("db_init_failed", error=str(e))
        return False


def save_trade(trade):
    """Save a completed trade to the database."""
    if not DATABASE_URL:
        return False

    try:
        d = asdict(trade) if hasattr(trade, '__dataclass_fields__') else trade
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades (opened_at, resolved_at, direction, entry_price,
                btc_at_entry, btc_at_open, btc_at_close, size, edge, won, pnl,
                mode, market_slug, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            d['opened_at'], d['resolved_at'], d['direction'], d['entry_price'],
            d['btc_at_entry'], d['btc_at_open'], d['btc_at_close'], d['size'],
            d['edge'], d['won'], d['pnl'], d['mode'], d['market_slug'],
            d['reason'],
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("save_trade_failed", error=str(e))
        return False


def update_bot_state(equity, peak_equity, max_drawdown, mode):
    """Update the bot state row."""
    if not DATABASE_URL:
        return False

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE bot_state
            SET equity = %s, peak_equity = %s, max_drawdown = %s,
                mode = %s, updated_at = %s
            WHERE id = 1
        """, (equity, peak_equity, max_drawdown, mode, time.time()))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("update_state_failed", error=str(e))
        return False


def get_bot_state():
    """Get current bot state."""
    if not DATABASE_URL:
        return None

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT equity, peak_equity, max_drawdown, mode, updated_at FROM bot_state WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {
                'equity': row[0],
                'peak_equity': row[1],
                'max_drawdown': row[2],
                'mode': row[3],
                'updated_at': row[4],
            }
        return None
    except Exception as e:
        log.error("get_state_failed", error=str(e))
        return None


def get_trades(limit=100):
    """Get recent trades, newest first."""
    if not DATABASE_URL:
        return []

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT opened_at, resolved_at, direction, entry_price, btc_at_entry,
                   btc_at_open, btc_at_close, size, edge, won, pnl, mode,
                   market_slug, reason
            FROM trades
            ORDER BY opened_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        trades = []
        for r in rows:
            trades.append({
                'opened_at': r[0], 'resolved_at': r[1], 'direction': r[2],
                'entry_price': r[3], 'btc_at_entry': r[4], 'btc_at_open': r[5],
                'btc_at_close': r[6], 'size': r[7], 'edge': r[8], 'won': r[9],
                'pnl': r[10], 'mode': r[11], 'market_slug': r[12], 'reason': r[13],
            })
        return trades
    except Exception as e:
        log.error("get_trades_failed", error=str(e))
        return []


def init_backtest_table():
    """Create backtest_trades table if it doesn't exist."""
    if not DATABASE_URL:
        return False
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id          SERIAL PRIMARY KEY,
                strategy    VARCHAR(50)  NOT NULL,
                date        VARCHAR(20)  NOT NULL,
                action      VARCHAR(10)  NOT NULL,
                ticker      VARCHAR(20)  NOT NULL,
                name        VARCHAR(50),
                direction   VARCHAR(10),
                entry_price DOUBLE PRECISION,
                momentum_pct DOUBLE PRECISION,
                equity_after DOUBLE PRECISION,
                pnl         DOUBLE PRECISION,
                funding_cost DOUBLE PRECISION,
                fee_cost    DOUBLE PRECISION,
                days_held   INTEGER,
                created_at  TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("init_backtest_table_failed", error=str(e))
        return False


def save_backtest_trades(strategy, trades):
    """Upsert backtest trades for a given strategy (clears old then inserts)."""
    if not DATABASE_URL:
        return False
    try:
        init_backtest_table()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM backtest_trades WHERE strategy = %s", (strategy,))
        for t in trades:
            cur.execute("""
                INSERT INTO backtest_trades
                    (strategy, date, action, ticker, name, direction,
                     entry_price, momentum_pct, equity_after, pnl,
                     funding_cost, fee_cost, days_held)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                strategy,
                t.get("date"), t.get("action", "BUY"), t.get("ticker"),
                t.get("name"), t.get("direction", "LONG"),
                t.get("entry_price") or t.get("price"),
                t.get("momentum_pct"), t.get("equity_after"),
                t.get("pnl", 0), t.get("funding_cost", 0),
                t.get("fee_cost", 0), t.get("days_held", 7),
            ))
        conn.commit()
        cur.close()
        conn.close()
        log.info("backtest_trades_saved", strategy=strategy, count=len(trades))
        return True
    except Exception as e:
        log.error("save_backtest_trades_failed", error=str(e))
        return False


def get_backtest_trades(strategy=None, limit=500):
    """Fetch backtest trades, optionally filtered by strategy."""
    if not DATABASE_URL:
        return []
    try:
        init_backtest_table()
        conn = get_connection()
        cur = conn.cursor()
        if strategy:
            cur.execute("""
                SELECT strategy, date, action, ticker, name, direction,
                       entry_price, momentum_pct, equity_after, pnl,
                       funding_cost, fee_cost, days_held
                FROM backtest_trades WHERE strategy = %s
                ORDER BY date ASC LIMIT %s
            """, (strategy, limit))
        else:
            cur.execute("""
                SELECT strategy, date, action, ticker, name, direction,
                       entry_price, momentum_pct, equity_after, pnl,
                       funding_cost, fee_cost, days_held
                FROM backtest_trades
                ORDER BY strategy, date ASC LIMIT %s
            """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {"strategy": r[0], "date": r[1], "action": r[2], "ticker": r[3],
             "name": r[4], "direction": r[5], "entry_price": r[6],
             "momentum_pct": r[7], "equity_after": r[8], "pnl": r[9],
             "funding_cost": r[10], "fee_cost": r[11], "days_held": r[12]}
            for r in rows
        ]
    except Exception as e:
        log.error("get_backtest_trades_failed", error=str(e))
        return []


# ── Scalper bot persistence ───────────────────────────────────────────────────

def init_scalper_tables():
    """Create scalper_state and scalper_trades tables if they don't exist."""
    if not DATABASE_URL:
        return False
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scalper_state (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                equity          DOUBLE PRECISION NOT NULL DEFAULT 25000,
                capital         DOUBLE PRECISION NOT NULL DEFAULT 25000,
                leverage        DOUBLE PRECISION NOT NULL DEFAULT 5,
                total_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
                total_fees      DOUBLE PRECISION NOT NULL DEFAULT 0,
                peak_equity     DOUBLE PRECISION NOT NULL DEFAULT 25000,
                max_dd_pct      DOUBLE PRECISION NOT NULL DEFAULT 0,
                status          VARCHAR(10) NOT NULL DEFAULT 'flat',
                last_check      TIMESTAMP WITH TIME ZONE,
                unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0,
                current_price   DOUBLE PRECISION,
                started_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                position_json   TEXT,
                CHECK (id = 1)
            );

            INSERT INTO scalper_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

            CREATE TABLE IF NOT EXISTS scalper_trades (
                id              SERIAL PRIMARY KEY,
                entry_time      TIMESTAMP WITH TIME ZONE NOT NULL,
                exit_time       TIMESTAMP WITH TIME ZONE NOT NULL,
                direction       VARCHAR(10) NOT NULL,
                entry_price     DOUBLE PRECISION NOT NULL,
                exit_price      DOUBLE PRECISION NOT NULL,
                exit_reason     VARCHAR(10) NOT NULL,
                pnl_pct         DOUBLE PRECISION NOT NULL,
                pnl_usd         DOUBLE PRECISION NOT NULL,
                fees_usd        DOUBLE PRECISION NOT NULL,
                pos_size        DOUBLE PRECISION NOT NULL,
                bars_held       INTEGER,
                equity_after    DOUBLE PRECISION NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("init_scalper_tables_failed", error=str(e))
        return False


def save_scalper_state(state: dict) -> bool:
    """Persist the full scalper state to DB."""
    if not DATABASE_URL:
        return False
    import json as _json
    try:
        init_scalper_tables()
        conn = get_connection()
        cur = conn.cursor()
        pos_json = _json.dumps(state.get("position")) if state.get("position") else None
        cur.execute("""
            UPDATE scalper_state SET
                equity         = %s,
                capital        = %s,
                leverage       = %s,
                total_pnl      = %s,
                total_fees     = %s,
                peak_equity    = %s,
                max_dd_pct     = %s,
                status         = %s,
                last_check     = NOW(),
                unrealized_pnl = %s,
                current_price  = %s,
                position_json  = %s
            WHERE id = 1
        """, (
            state.get("equity", 25000),
            state.get("capital", 25000),
            state.get("leverage", 5),
            state.get("total_pnl", 0),
            state.get("total_fees", 0),
            state.get("peak_equity", 25000),
            state.get("max_dd_pct", 0),
            state.get("status", "flat"),
            state.get("unrealized_pnl", 0),
            state.get("current_price"),
            pos_json,
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("save_scalper_state_failed", error=str(e))
        return False


def append_scalper_trade(trade: dict) -> bool:
    """Insert one completed scalper trade into DB."""
    if not DATABASE_URL:
        return False
    try:
        init_scalper_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scalper_trades
                (entry_time, exit_time, direction, entry_price, exit_price,
                 exit_reason, pnl_pct, pnl_usd, fees_usd, pos_size,
                 bars_held, equity_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            trade.get("entry_time"),
            trade.get("exit_time"),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("exit_price"),
            trade.get("exit_reason"),
            trade.get("pnl_pct", 0),
            trade.get("pnl_usd", 0),
            trade.get("fees_usd", 0),
            trade.get("pos_size", 0),
            trade.get("bars_held"),
            trade.get("equity_after", 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("append_scalper_trade_failed", error=str(e))
        return False


def get_scalper_state() -> dict | None:
    """Load full scalper state from DB (state + all trades)."""
    if not DATABASE_URL:
        return None
    import json as _json
    try:
        init_scalper_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT equity, capital, leverage, total_pnl, total_fees,
                   peak_equity, max_dd_pct, status, last_check,
                   unrealized_pnl, current_price, started_at, position_json
            FROM scalper_state WHERE id = 1
        """)
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return None

        cur.execute("""
            SELECT entry_time, exit_time, direction, entry_price, exit_price,
                   exit_reason, pnl_pct, pnl_usd, fees_usd, pos_size,
                   bars_held, equity_after
            FROM scalper_trades ORDER BY entry_time ASC
        """)
        trade_rows = cur.fetchall()
        cur.close()
        conn.close()

        trades = []
        for r in trade_rows:
            trades.append({
                "entry_time":  r[0].isoformat() if r[0] else None,
                "exit_time":   r[1].isoformat() if r[1] else None,
                "direction":   r[2],
                "entry_price": r[3],
                "exit_price":  r[4],
                "exit_reason": r[5],
                "pnl_pct":     r[6],
                "pnl_usd":     r[7],
                "fees_usd":    r[8],
                "pos_size":    r[9],
                "bars_held":   r[10],
                "equity_after": r[11],
            })

        return {
            "equity":        row[0],
            "capital":       row[1],
            "leverage":      row[2],
            "total_pnl":     row[3],
            "total_fees":    row[4],
            "peak_equity":   row[5],
            "max_dd_pct":    row[6],
            "status":        row[7],
            "last_check":    row[8].isoformat() if row[8] else None,
            "unrealized_pnl": row[9],
            "current_price": row[10],
            "started_at":    row[11].isoformat() if row[11] else None,
            "position":      _json.loads(row[12]) if row[12] else None,
            "trades":        trades,
        }
    except Exception as e:
        log.error("get_scalper_state_failed", error=str(e))
        return None


def reset_scalper_state(capital: float = 25000, leverage: float = 5) -> bool:
    """Wipe trades and reset state to starting values."""
    if not DATABASE_URL:
        return False
    try:
        init_scalper_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM scalper_trades")
        cur.execute("""
            UPDATE scalper_state SET
                equity=%(c)s, capital=%(c)s, leverage=%(l)s,
                total_pnl=0, total_fees=0, peak_equity=%(c)s, max_dd_pct=0,
                status='flat', last_check=NULL, unrealized_pnl=0,
                current_price=NULL, started_at=NOW(), position_json=NULL
            WHERE id=1
        """, {"c": capital, "l": leverage})
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("reset_scalper_state_failed", error=str(e))
        return False


# ── BB Reversal bot persistence ───────────────────────────────────────────────

def init_bb_tables():
    """Create scalper_bb_state and scalper_bb_trades tables if they don't exist."""
    if not DATABASE_URL:
        return False
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scalper_bb_state (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                equity          DOUBLE PRECISION NOT NULL DEFAULT 25000,
                capital         DOUBLE PRECISION NOT NULL DEFAULT 25000,
                leverage        DOUBLE PRECISION NOT NULL DEFAULT 5,
                total_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
                total_fees      DOUBLE PRECISION NOT NULL DEFAULT 0,
                peak_equity     DOUBLE PRECISION NOT NULL DEFAULT 25000,
                max_dd_pct      DOUBLE PRECISION NOT NULL DEFAULT 0,
                status          VARCHAR(10) NOT NULL DEFAULT 'flat',
                last_check      TIMESTAMP WITH TIME ZONE,
                unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0,
                current_price   DOUBLE PRECISION,
                started_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                position_json   TEXT,
                CHECK (id = 1)
            );

            INSERT INTO scalper_bb_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

            CREATE TABLE IF NOT EXISTS scalper_bb_trades (
                id              SERIAL PRIMARY KEY,
                entry_time      TIMESTAMP WITH TIME ZONE NOT NULL,
                exit_time       TIMESTAMP WITH TIME ZONE NOT NULL,
                direction       VARCHAR(10) NOT NULL,
                entry_price     DOUBLE PRECISION NOT NULL,
                exit_price      DOUBLE PRECISION NOT NULL,
                exit_reason     VARCHAR(10) NOT NULL,
                pnl_pct         DOUBLE PRECISION NOT NULL,
                pnl_usd         DOUBLE PRECISION NOT NULL,
                fees_usd        DOUBLE PRECISION NOT NULL,
                pos_size        DOUBLE PRECISION NOT NULL,
                bars_held       INTEGER,
                equity_after    DOUBLE PRECISION NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("init_bb_tables_failed", error=str(e))
        return False


def save_bb_state(state: dict) -> bool:
    """Persist the full BB bot state to DB."""
    if not DATABASE_URL:
        return False
    import json as _json
    try:
        init_bb_tables()
        conn = get_connection()
        cur = conn.cursor()
        pos_json = _json.dumps(state.get("position")) if state.get("position") else None
        cur.execute("""
            UPDATE scalper_bb_state SET
                equity         = %s,
                capital        = %s,
                leverage       = %s,
                total_pnl      = %s,
                total_fees     = %s,
                peak_equity    = %s,
                max_dd_pct     = %s,
                status         = %s,
                last_check     = NOW(),
                unrealized_pnl = %s,
                current_price  = %s,
                position_json  = %s
            WHERE id = 1
        """, (
            state.get("equity", 25000),
            state.get("capital", 25000),
            state.get("leverage", 5),
            state.get("total_pnl", 0),
            state.get("total_fees", 0),
            state.get("peak_equity", 25000),
            state.get("max_dd_pct", 0),
            state.get("status", "flat"),
            state.get("unrealized_pnl", 0),
            state.get("current_price"),
            pos_json,
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("save_bb_state_failed", error=str(e))
        return False


def append_bb_trade(trade: dict) -> bool:
    """Insert one completed BB trade into DB."""
    if not DATABASE_URL:
        return False
    try:
        init_bb_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scalper_bb_trades
                (entry_time, exit_time, direction, entry_price, exit_price,
                 exit_reason, pnl_pct, pnl_usd, fees_usd, pos_size,
                 bars_held, equity_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            trade.get("entry_time"),
            trade.get("exit_time"),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("exit_price"),
            trade.get("exit_reason"),
            trade.get("pnl_pct", 0),
            trade.get("pnl_usd", 0),
            trade.get("fees_usd", 0),
            trade.get("pos_size", 0),
            trade.get("bars_held"),
            trade.get("equity_after", 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("append_bb_trade_failed", error=str(e))
        return False


def get_bb_state() -> dict | None:
    """Load full BB bot state from DB (state + all trades)."""
    if not DATABASE_URL:
        return None
    import json as _json
    try:
        init_bb_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT equity, capital, leverage, total_pnl, total_fees,
                   peak_equity, max_dd_pct, status, last_check,
                   unrealized_pnl, current_price, started_at, position_json
            FROM scalper_bb_state WHERE id = 1
        """)
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return None

        cur.execute("""
            SELECT entry_time, exit_time, direction, entry_price, exit_price,
                   exit_reason, pnl_pct, pnl_usd, fees_usd, pos_size,
                   bars_held, equity_after
            FROM scalper_bb_trades ORDER BY entry_time ASC
        """)
        trade_rows = cur.fetchall()
        cur.close()
        conn.close()

        trades = []
        for r in trade_rows:
            trades.append({
                "entry_time":  r[0].isoformat() if r[0] else None,
                "exit_time":   r[1].isoformat() if r[1] else None,
                "direction":   r[2],
                "entry_price": r[3],
                "exit_price":  r[4],
                "exit_reason": r[5],
                "pnl_pct":     r[6],
                "pnl_usd":     r[7],
                "fees_usd":    r[8],
                "pos_size":    r[9],
                "bars_held":   r[10],
                "equity_after": r[11],
            })

        return {
            "equity":        row[0],
            "capital":       row[1],
            "leverage":      row[2],
            "total_pnl":     row[3],
            "total_fees":    row[4],
            "peak_equity":   row[5],
            "max_dd_pct":    row[6],
            "status":        row[7],
            "last_check":    row[8].isoformat() if row[8] else None,
            "unrealized_pnl": row[9],
            "current_price": row[10],
            "started_at":    row[11].isoformat() if row[11] else None,
            "position":      _json.loads(row[12]) if row[12] else None,
            "trades":        trades,
        }
    except Exception as e:
        log.error("get_bb_state_failed", error=str(e))
        return None


def reset_bb_state(capital: float = 25000, leverage: float = 5) -> bool:
    """Wipe BB trades and reset state to starting values."""
    if not DATABASE_URL:
        return False
    try:
        init_bb_tables()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM scalper_bb_trades")
        cur.execute("""
            UPDATE scalper_bb_state SET
                equity=%(c)s, capital=%(c)s, leverage=%(l)s,
                total_pnl=0, total_fees=0, peak_equity=%(c)s, max_dd_pct=0,
                status='flat', last_check=NULL, unrealized_pnl=0,
                current_price=NULL, started_at=NOW(), position_json=NULL
            WHERE id=1
        """, {"c": capital, "l": leverage})
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.error("reset_bb_state_failed", error=str(e))
        return False


def get_stats():
    """Get aggregate trading statistics."""
    if not DATABASE_URL:
        return None

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN won THEN 1 ELSE 0 END) as wins,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl,
                MAX(pnl) as best_trade,
                MIN(pnl) as worst_trade,
                AVG(entry_price) as avg_entry,
                AVG(edge) as avg_edge
            FROM trades
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row and row[0] > 0:
            return {
                'total': row[0],
                'wins': row[1],
                'losses': row[0] - row[1],
                'win_rate': row[1] / row[0] if row[0] > 0 else 0,
                'total_pnl': row[2],
                'avg_pnl': row[3],
                'best_trade': row[4],
                'worst_trade': row[5],
                'avg_entry': row[6],
                'avg_edge': row[7],
            }
        return None
    except Exception as e:
        log.error("get_stats_failed", error=str(e))
        return None

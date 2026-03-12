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

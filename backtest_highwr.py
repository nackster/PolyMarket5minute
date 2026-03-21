"""
High Win Rate ETH 5m Scalping Backtest
Target: 60%+ WR, $500+/day on $25k × 5x leverage
Strategies: BB Reversal, VWAP Deviation, RSI Extreme, Keltner Channel, StochRSI
"""

import requests, time, math, json, os, itertools
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CAPITAL       = 25_000.0
LEVERAGE      = 5.0
MAKER_REBATE  = -0.0002   # -0.02% on notional
TAKER_FEE     =  0.0005   #  0.05% on notional
NET_FEE_RT    = MAKER_REBATE + TAKER_FEE  # 0.03% round-trip
TRAIN_DAYS    = 12
TEST_DAYS     = 6
MAX_HOLD      = 20
COOLDOWN      = 2
OUTPUT_FILE   = "trades/scalper_highwr_sweep.json"

# ─── DATA FETCH ───────────────────────────────────────────────────────────────
def fetch_candles(days=18):
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    print(f"Fetching ETH 5m candles for last {days} days …")
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        headers={"Content-Type": "application/json"},
        json={"type": "candleSnapshot", "req": {
            "coin": "ETH", "interval": "5m",
            "startTime": start_ms, "endTime": now_ms
        }}, timeout=30)
    resp.raise_for_status()
    candles = [{"t": int(k["t"])//1000, "o": float(k["o"]), "h": float(k["h"]),
                "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v", 0))}
               for k in resp.json()]
    candles = sorted(candles, key=lambda x: x["t"])
    print(f"  Got {len(candles)} candles  ({candles[0]['t']} to {candles[-1]['t']})")
    return candles

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def _ema(values, period):
    k   = 2.0 / (period + 1)
    out = [float("nan")] * len(values)
    seed = None
    for i, v in enumerate(values):
        if math.isnan(v): continue
        if seed is None: seed = v; out[i] = v
        else: seed = v * k + seed * (1 - k); out[i] = seed
    return out

def _sma(values, period):
    out = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(math.isnan(x) for x in window): continue
        out[i] = sum(window) / period
    return out

def _rsi(closes, period=14):
    out = [float("nan")] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
        if i >= period:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def _atr(highs, lows, closes, period=14):
    out = [float("nan")] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i]  - closes[i-1]))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[-period:]) / period
    return out

def _stdev(values, period):
    out = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(math.isnan(x) for x in window): continue
        mean = sum(window) / period
        out[i] = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
    return out

def _swing_low(lows, idx, lookback=3):
    start = max(0, idx - lookback)
    return min(lows[start:idx+1])

def _swing_high(highs, idx, lookback=3):
    start = max(0, idx - lookback)
    return max(highs[start:idx+1])

# ─── VWAP HELPERS ─────────────────────────────────────────────────────────────
def _compute_vwap(candles):
    """Rolling session VWAP, reset at midnight UTC."""
    vwap_arr  = [float("nan")] * len(candles)
    stdev_arr = [float("nan")] * len(candles)
    sess_tp   = []
    sess_tv   = 0.0
    sess_tpv  = 0.0
    last_day  = None

    for i, c in enumerate(candles):
        day = datetime.fromtimestamp(c["t"], tz=timezone.utc).date()
        if day != last_day:
            sess_tp   = []
            sess_tv   = 0.0
            sess_tpv  = 0.0
            last_day  = day
        tp = (c["h"] + c["l"] + c["c"]) / 3.0
        sess_tp.append(tp)
        sess_tv   += c["v"]
        sess_tpv  += tp * c["v"]
        if sess_tv > 0:
            vwap_arr[i] = sess_tpv / sess_tv
        # rolling stdev of typical price (last 20 bars within session)
        window = sess_tp[-20:]
        if len(window) >= 5:
            mn = sum(window) / len(window)
            stdev_arr[i] = math.sqrt(sum((x - mn)**2 for x in window) / len(window))
    return vwap_arr, stdev_arr

# ─── BACKTEST ENGINE ──────────────────────────────────────────────────────────
def run_backtest(candles, strategy_fn, params):
    """
    Returns dict: daily_pnl, wr_pct, max_dd, num_trades, avg_bars
    strategy_fn(candles, i, indicators, params) -> signal dict or None
    signal = {"side": "long"/"short", "entry": price, "sl": price, "tp": price}
    """
    equity    = CAPITAL
    peak      = CAPITAL
    max_dd    = 0.0
    trades    = []
    position  = None   # {"side","entry","sl","tp","bar_in"}
    cooldown  = 0
    daily_pnl = {}     # date_str -> pnl

    # Pre-compute all indicators once
    indicators = strategy_fn["precompute"](candles)

    # We iterate over every bar; signal is based on candles[i-1] (last complete bar)
    for i in range(1, len(candles)):
        bar = candles[i]
        dt  = datetime.fromtimestamp(bar["t"], tz=timezone.utc)
        day = dt.date().isoformat()

        # ── Manage open position ──
        if position is not None:
            bars_held = i - position["bar_in"]
            hi, lo    = bar["h"], bar["l"]
            side      = position["side"]
            sl, tp    = position["sl"], position["tp"]

            closed    = False
            exit_px   = None
            outcome   = None

            if side == "long":
                # Conservative: SL first if both hit same bar
                if lo <= sl:
                    exit_px = sl; outcome = "sl"; closed = True
                elif hi >= tp:
                    exit_px = tp; outcome = "tp"; closed = True
                elif bars_held >= MAX_HOLD:
                    exit_px = bar["c"]; outcome = "timeout"; closed = True
            else:  # short
                if hi >= sl:
                    exit_px = sl; outcome = "sl"; closed = True
                elif lo <= tp:
                    exit_px = tp; outcome = "tp"; closed = True
                elif bars_held >= MAX_HOLD:
                    exit_px = bar["c"]; outcome = "timeout"; closed = True

            if closed:
                entry = position["entry"]
                notional = equity * LEVERAGE
                if side == "long":
                    pnl_pct = (exit_px - entry) / entry
                else:
                    pnl_pct = (entry - exit_px) / entry
                pnl = pnl_pct * notional - abs(notional) * NET_FEE_RT
                equity += pnl
                if equity > peak: peak = equity
                dd = (peak - equity) / peak
                if dd > max_dd: max_dd = dd
                daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
                trades.append({
                    "outcome": outcome, "pnl": pnl, "bars": bars_held,
                    "win": pnl > 0
                })
                position  = None
                cooldown  = COOLDOWN
                continue

        # ── Cooldown ──
        if cooldown > 0:
            cooldown -= 1
            continue

        # ── Generate signal from last completed bar (i-1) ──
        if position is None and i >= 2:
            sig = strategy_fn["signal"](candles, i - 1, indicators, params)
            if sig is not None:
                position = {
                    "side"   : sig["side"],
                    "entry"  : candles[i]["o"],   # enter at open of current bar
                    "sl"     : sig["sl"],
                    "tp"     : sig["tp"],
                    "bar_in" : i,
                }

    # ── Stats ──
    num_trades = len(trades)
    if num_trades == 0:
        return {"daily_pnl": 0, "wr_pct": 0, "max_dd": 0,
                "num_trades": 0, "avg_bars": 0}
    wins       = sum(1 for t in trades if t["win"])
    wr_pct     = 100.0 * wins / num_trades
    avg_bars   = sum(t["bars"] for t in trades) / num_trades
    days_count = max(len(daily_pnl), 1)
    d_pnl      = sum(daily_pnl.values()) / days_count
    return {
        "daily_pnl" : d_pnl,
        "wr_pct"    : wr_pct,
        "max_dd"    : max_dd * 100,
        "num_trades": num_trades,
        "avg_bars"  : avg_bars,
        "total_pnl" : sum(t["pnl"] for t in trades),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Strategy 1: Bollinger Band Reversal ───────────────────────────────────────
def s1_precompute(candles):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    opens  = [c["o"] for c in candles]
    return {
        "closes": closes, "highs": highs, "lows": lows, "opens": opens,
        "rsi14" : _rsi(closes, 14),
        "rsi7"  : _rsi(closes, 7),
        "atr14" : _atr(highs, lows, closes, 14),
    }

def s1_signal(candles, i, ind, p):
    period  = p["period"]
    dev     = p["dev"]
    rsi_os  = p["rsi_os"]
    sl_mult = p["sl_mult"]
    tp_rr   = p["tp_rr"]
    rsi_ob  = 100 - rsi_os

    closes = ind["closes"]; opens = ind["opens"]
    rsi    = ind["rsi14"]
    atr    = ind["atr14"]

    if i < period: return None
    window = closes[i - period + 1 : i + 1]
    if any(math.isnan(x) for x in window): return None
    mid    = sum(window) / period
    sd     = math.sqrt(sum((x - mid)**2 for x in window) / period)
    upper  = mid + dev * sd
    lower  = mid - dev * sd

    rsi_i  = rsi[i]
    atr_i  = atr[i]
    if math.isnan(rsi_i) or math.isnan(atr_i) or atr_i == 0: return None

    c = closes[i]; o = opens[i]

    if c < lower and rsi_i < rsi_os and c > o:   # Long
        sl   = c - atr_i * sl_mult
        risk = c - sl
        if risk <= 0: return None
        tp   = c + min(mid - c, tp_rr * risk)
        if tp <= c: return None
        return {"side": "long", "sl": sl, "tp": tp}

    if c > upper and rsi_i > rsi_ob and c < o:   # Short
        sl   = c + atr_i * sl_mult
        risk = sl - c
        if risk <= 0: return None
        tp   = c - min(c - mid, tp_rr * risk)
        if tp >= c: return None
        return {"side": "short", "sl": sl, "tp": tp}

    return None

S1 = {"precompute": s1_precompute, "signal": s1_signal}

S1_PARAMS = [
    {"period": period, "dev": dev, "rsi_os": rsi_os,
     "sl_mult": sl_mult, "tp_rr": tp_rr}
    for period  in [15, 20]
    for dev     in [1.8, 2.0, 2.2]
    for rsi_os  in [35, 40]
    for sl_mult in [0.8, 1.2]
    for tp_rr   in [1.0, 1.5]
]

# ── Strategy 2: VWAP Deviation Reversion ─────────────────────────────────────
def s2_precompute(candles):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    vwap, vstdev = _compute_vwap(candles)
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "vwap"  : vwap, "vstdev": vstdev,
        "rsi14" : _rsi(closes, 14),
        "atr14" : _atr(highs, lows, closes, 14),
    }

def s2_signal(candles, i, ind, p):
    dev     = p["dev"]
    sl_mult = p["sl_mult"]

    closes  = ind["closes"]
    vwap    = ind["vwap"]
    vstdev  = ind["vstdev"]
    rsi     = ind["rsi14"]
    atr     = ind["atr14"]

    v  = vwap[i];  sd = vstdev[i]
    r  = rsi[i];   a  = atr[i];  c = closes[i]
    if any(math.isnan(x) for x in [v, sd, r, a]): return None
    if sd == 0 or a == 0: return None

    upper = v + dev * sd
    lower = v - dev * sd

    if c < lower and r < 40:   # Long
        sl   = c - a * sl_mult
        risk = c - sl
        if risk <= 0: return None
        tp   = v   # target VWAP
        if tp - c < 0.5 * risk: return None  # min 0.5 R:R
        return {"side": "long", "sl": sl, "tp": tp}

    if c > upper and r > 60:   # Short
        sl   = c + a * sl_mult
        risk = sl - c
        if risk <= 0: return None
        tp   = v
        if c - tp < 0.5 * risk: return None
        return {"side": "short", "sl": sl, "tp": tp}

    return None

S2 = {"precompute": s2_precompute, "signal": s2_signal}

S2_PARAMS = [
    {"dev": dev, "sl_mult": sl_mult}
    for dev     in [1.5, 2.0, 2.5]
    for sl_mult in [0.8, 1.2]
]

# ── Strategy 3: RSI Extreme Reversal ─────────────────────────────────────────
def s3_precompute(candles):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "rsi14" : _rsi(closes, 14),
        "rsi7"  : _rsi(closes, 7),
        "atr14" : _atr(highs, lows, closes, 14),
    }

def s3_signal(candles, i, ind, p):
    rsi_period = p["rsi_period"]
    rsi_os     = p["rsi_os"]
    rsi_ob     = 100 - rsi_os
    tp_rr      = p["tp_rr"]

    closes = ind["closes"]
    highs  = ind["highs"]
    lows   = ind["lows"]
    rsi    = ind["rsi14"] if rsi_period == 14 else ind["rsi7"]
    atr    = ind["atr14"]

    if i < 1: return None
    r_cur  = rsi[i];   r_prev = rsi[i-1]
    a      = atr[i]
    if math.isnan(r_cur) or math.isnan(r_prev) or math.isnan(a): return None
    if a == 0: return None

    if r_prev < rsi_os and r_cur >= rsi_os:   # RSI crosses UP through oversold → Long
        sl   = _swing_low(lows, i, 3) - 0.1 * a
        risk = closes[i] - sl
        if risk <= 0: return None
        tp   = closes[i] + tp_rr * risk
        return {"side": "long", "sl": sl, "tp": tp}

    if r_prev > rsi_ob and r_cur <= rsi_ob:   # RSI crosses DOWN through overbought → Short
        sl   = _swing_high(highs, i, 3) + 0.1 * a
        risk = sl - closes[i]
        if risk <= 0: return None
        tp   = closes[i] - tp_rr * risk
        return {"side": "short", "sl": sl, "tp": tp}

    return None

S3 = {"precompute": s3_precompute, "signal": s3_signal}

S3_PARAMS = [
    {"rsi_period": rsi_period, "rsi_os": rsi_os, "tp_rr": tp_rr}
    for rsi_period in [7, 14]
    for rsi_os     in [25, 30, 35]
    for tp_rr      in [1.0, 1.5, 2.0]
]

# ── Strategy 4: Keltner Channel Mean Reversion ────────────────────────────────
def s4_precompute(candles):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "ema20" : _ema(closes, 20),
        "atr10" : _atr(highs, lows, closes, 10),
        "atr14" : _atr(highs, lows, closes, 14),
        "rsi14" : _rsi(closes, 14),
    }

def s4_signal(candles, i, ind, p):
    mult     = p["mult"]
    sl_mult  = p["sl_mult"]
    min_tp_rr = 0.8

    closes   = ind["closes"]
    highs    = ind["highs"]
    lows     = ind["lows"]
    ema20    = ind["ema20"]
    atr10    = ind["atr10"]
    atr14    = ind["atr14"]
    rsi      = ind["rsi14"]

    if i < 1: return None
    mid     = ema20[i];  mid_p  = ema20[i-1]
    a10     = atr10[i];  a14    = atr14[i]
    r       = rsi[i]
    c       = closes[i]; c_prev = closes[i-1]

    if any(math.isnan(x) for x in [mid, mid_p, a10, a14, r]): return None
    if a10 == 0 or a14 == 0: return None

    upper      = mid   + mult * a10
    lower      = mid   - mult * a10
    upper_prev = mid_p + mult * a10
    lower_prev = mid_p - mult * a10

    if c < lower and c_prev >= lower_prev and r < 45:   # Long — just crossed below KC
        sl   = c - a14 * sl_mult
        risk = c - sl
        if risk <= 0: return None
        tp_dist = mid - c
        if tp_dist < min_tp_rr * risk: return None
        tp   = c + tp_dist
        return {"side": "long", "sl": sl, "tp": tp}

    if c > upper and c_prev <= upper_prev and r > 55:   # Short
        sl   = c + a14 * sl_mult
        risk = sl - c
        if risk <= 0: return None
        tp_dist = c - mid
        if tp_dist < min_tp_rr * risk: return None
        tp   = c - tp_dist
        return {"side": "short", "sl": sl, "tp": tp}

    return None

S4 = {"precompute": s4_precompute, "signal": s4_signal}

S4_PARAMS = [
    {"mult": mult, "sl_mult": sl_mult}
    for mult    in [1.5, 2.0, 2.5]
    for sl_mult in [0.8, 1.0, 1.2]
]

# ── Strategy 5: StochRSI Reversal ─────────────────────────────────────────────
def s5_precompute(candles):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    rsi14  = _rsi(closes, 14)
    # StochRSI raw
    stoch_k_raw = [float("nan")] * len(closes)
    stoch_period = 14
    for i in range(stoch_period - 1, len(closes)):
        window = rsi14[i - stoch_period + 1 : i + 1]
        if any(math.isnan(x) for x in window): continue
        lo = min(window); hi = max(window)
        if hi == lo:
            stoch_k_raw[i] = 50.0
        else:
            stoch_k_raw[i] = (rsi14[i] - lo) / (hi - lo) * 100.0
    # Smooth with EMA
    k_smooth = 3; d_smooth = 3
    pct_k    = _ema(stoch_k_raw, k_smooth)
    pct_d    = _ema(pct_k, d_smooth)
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "pct_k" : pct_k, "pct_d": pct_d,
        "atr14" : _atr(highs, lows, closes, 14),
    }

def s5_signal(candles, i, ind, p):
    os_level = p["os_level"]
    ob_level = 100 - os_level
    tp_rr    = p["tp_rr"]

    closes = ind["closes"]
    highs  = ind["highs"]
    lows   = ind["lows"]
    pct_k  = ind["pct_k"]
    pct_d  = ind["pct_d"]
    atr    = ind["atr14"]

    if i < 1: return None
    k     = pct_k[i];  k_p = pct_k[i-1]
    d     = pct_d[i];  d_p = pct_d[i-1]
    a     = atr[i];    c   = closes[i]
    if any(math.isnan(x) for x in [k, k_p, d, d_p, a]): return None
    if a == 0: return None

    if k < os_level and k_p <= d_p and k > d:   # Long: %K < OS and crosses above %D
        sl   = _swing_low(lows, i, 3) - 0.1 * a
        risk = c - sl
        if risk <= 0: return None
        tp   = c + tp_rr * risk
        return {"side": "long", "sl": sl, "tp": tp}

    if k > ob_level and k_p >= d_p and k < d:   # Short: %K > OB and crosses below %D
        sl   = _swing_high(highs, i, 3) + 0.1 * a
        risk = sl - c
        if risk <= 0: return None
        tp   = c - tp_rr * risk
        return {"side": "short", "sl": sl, "tp": tp}

    return None

S5 = {"precompute": s5_precompute, "signal": s5_signal}

S5_PARAMS = [
    {"os_level": os_level, "tp_rr": tp_rr}
    for os_level in [20, 25]
    for tp_rr    in [1.0, 1.5, 2.0]
]

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SWEEP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    candles = fetch_candles(days=TRAIN_DAYS + TEST_DAYS)

    # Split train / test
    cutoff_ts = candles[0]["t"] + TRAIN_DAYS * 24 * 3600
    train     = [c for c in candles if c["t"] <  cutoff_ts]
    test      = [c for c in candles if c["t"] >= cutoff_ts]
    print(f"Train: {len(train)} bars  |  Test (OOS): {len(test)} bars")
    print(f"Train starts: {datetime.fromtimestamp(train[0]['t'], tz=timezone.utc)}")
    print(f"Test  starts: {datetime.fromtimestamp(test[0]['t'],  tz=timezone.utc)}")
    print()

    strategies = [
        ("BB_Reversal",    S1, S1_PARAMS),
        ("VWAP_Reversion", S2, S2_PARAMS),
        ("RSI_Extreme",    S3, S3_PARAMS),
        ("Keltner_MR",     S4, S4_PARAMS),
        ("StochRSI_Rev",   S5, S5_PARAMS),
    ]

    all_results = []
    total_combos = sum(len(pp) for _, _, pp in strategies)
    done = 0

    for name, strat, param_list in strategies:
        print(f"[{name}] testing {len(param_list)} parameter combos …")
        for params in param_list:
            train_r = run_backtest(train, strat, params)
            test_r  = run_backtest(test,  strat, params)
            row = {
                "strategy"       : name,
                "params"         : params,
                "train_daily_pnl": round(train_r["daily_pnl"], 2),
                "train_wr_pct"   : round(train_r["wr_pct"],    2),
                "train_num_trades": train_r["num_trades"],
                "oos_daily_pnl"  : round(test_r["daily_pnl"],  2),
                "oos_wr_pct"     : round(test_r["wr_pct"],     2),
                "oos_max_dd"     : round(test_r["max_dd"],      2),
                "oos_num_trades" : test_r["num_trades"],
                "oos_avg_bars"   : round(test_r["avg_bars"],    2),
            }
            all_results.append(row)
            done += 1
        print(f"  done. ({done}/{total_combos})")

    # Save all results
    os.makedirs("trades", exist_ok=True)
    out_path = OUTPUT_FILE
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nFull results saved to {out_path}\n")

    # ── Print results ──────────────────────────────────────────────────────────
    SEP = "-" * 110

    # All combos with OOS WR >= 55%
    highwr = [r for r in all_results if r["oos_wr_pct"] >= 55.0]
    highwr_sorted = sorted(highwr, key=lambda x: x["oos_daily_pnl"], reverse=True)

    print("=" * 110)
    print(f"  ALL COMBOS WITH OOS WIN RATE >= 55%   ({len(highwr_sorted)} combos)")
    print("=" * 110)
    hdr = f"{'Strategy':<20} {'WR%':>6} {'Daily$':>9} {'MaxDD%':>7} {'Trades':>7} {'AvgBars':>8}  Params"
    print(hdr); print(SEP)
    for r in highwr_sorted:
        print(f"{r['strategy']:<20} {r['oos_wr_pct']:>6.1f} {r['oos_daily_pnl']:>9.2f} "
              f"{r['oos_max_dd']:>7.2f} {r['oos_num_trades']:>7} {r['oos_avg_bars']:>8.1f}  "
              f"{r['params']}")
    if not highwr_sorted:
        print("  (none found)")
    print()

    # Top 10 overall by OOS daily pnl
    top10 = sorted(all_results, key=lambda x: x["oos_daily_pnl"], reverse=True)[:10]
    print("=" * 110)
    print("  TOP 10 OVERALL BY OOS DAILY PNL")
    print("=" * 110)
    print(hdr); print(SEP)
    for r in top10:
        print(f"{r['strategy']:<20} {r['oos_wr_pct']:>6.1f} {r['oos_daily_pnl']:>9.2f} "
              f"{r['oos_max_dd']:>7.2f} {r['oos_num_trades']:>7} {r['oos_avg_bars']:>8.1f}  "
              f"{r['params']}")
    print()

    # Best combo: WR >= 55% AND daily_pnl >= $300 AND trades >= 10
    best = [r for r in highwr_sorted
            if r["oos_daily_pnl"] >= 300 and r["oos_num_trades"] >= 10]
    print("=" * 110)
    print("  BEST COMBO: OOS WR >= 55%, OOS Daily >= $300, OOS Trades >= 10")
    print("=" * 110)
    if best:
        b = best[0]
        print(f"  Strategy  : {b['strategy']}")
        print(f"  Params    : {b['params']}")
        print(f"  OOS Daily : ${b['oos_daily_pnl']:.2f}")
        print(f"  OOS WR    : {b['oos_wr_pct']:.1f}%")
        print(f"  OOS MaxDD : {b['oos_max_dd']:.2f}%")
        print(f"  OOS Trades: {b['oos_num_trades']}")
        print(f"  OOS AvgBars:{b['oos_avg_bars']:.1f}")
        print(f"  Train Daily: ${b['train_daily_pnl']:.2f}  Train WR: {b['train_wr_pct']:.1f}%")
    else:
        print("  (no combo meets all three criteria)")
        # Show closest
        if highwr_sorted:
            print("\n  Closest by WR >= 55% (best daily_pnl regardless of other filters):")
            b = highwr_sorted[0]
            print(f"  {b['strategy']}  WR={b['oos_wr_pct']:.1f}%  Daily=${b['oos_daily_pnl']:.2f}  "
                  f"Trades={b['oos_num_trades']}  {b['params']}")
    print()

if __name__ == "__main__":
    main()

"""
backtest_v2.py — ETH 5-minute intraday strategy sweep (5 strategies)
Capital: $25,000, Leverage: 5x, Net fee: 0.03% round-trip on notional
Train: first 60 days, OOS: last 30 days
"""

import requests, time, math, json, os, sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_candles(days=90):
    """Fetch ETH 5m candles in batches (API caps at ~5000 bars = ~17.5 days per request)."""
    print(f"Fetching {days} days of ETH 5m candles from Hyperliquid (batched)...")
    BATCH_DAYS = 17  # ~5000 bars per request is the API max
    now_ms = int(time.time() * 1000)
    start_total_ms = now_ms - days * 24 * 3600 * 1000
    all_raw = {}

    batch_start = start_total_ms
    batch_num = 0
    while batch_start < now_ms:
        batch_end = min(batch_start + BATCH_DAYS * 24 * 3600 * 1000, now_ms)
        batch_num += 1
        print(f"  Batch {batch_num}: {time.strftime('%Y-%m-%d', time.gmtime(batch_start // 1000))} to "
              f"{time.strftime('%Y-%m-%d', time.gmtime(batch_end // 1000))} ...", end=" ", flush=True)
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            headers={"Content-Type": "application/json"},
            json={"type": "candleSnapshot", "req": {
                "coin": "ETH", "interval": "5m",
                "startTime": batch_start, "endTime": batch_end
            }},
            timeout=60
        )
        resp.raise_for_status()
        raw = resp.json()
        print(f"{len(raw)} bars")
        for k in raw:
            t = int(k["t"]) // 1000
            all_raw[t] = {
                "t": t,
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": float(k["c"]),
                "v": float(k["v"]),
            }
        batch_start = batch_end
        if raw:
            time.sleep(0.3)  # gentle rate limiting

    candles = sorted(all_raw.values(), key=lambda x: x["t"])
    print(f"  Total: {len(candles)} candles (first: {time.strftime('%Y-%m-%d', time.gmtime(candles[0]['t']))}, "
          f"last: {time.strftime('%Y-%m-%d', time.gmtime(candles[-1]['t']))})")
    return candles

# ---------------------------------------------------------------------------
# Indicator helpers (pure Python)
# ---------------------------------------------------------------------------

def sma(arr, n):
    """Simple moving average — returns list same length, nan where insufficient data."""
    out = [float("nan")] * len(arr)
    for i in range(n - 1, len(arr)):
        out[i] = sum(arr[i - n + 1 : i + 1]) / n
    return out

def ema(arr, n):
    """EMA — returns list same length, nan where insufficient data."""
    out = [float("nan")] * len(arr)
    # find first valid index
    start = next((i for i, v in enumerate(arr) if not math.isnan(v)), None)
    if start is None:
        return out
    # seed with SMA
    if start + n - 1 >= len(arr):
        return out
    seed_start = start
    seed_end = seed_start + n
    if seed_end > len(arr):
        return out
    seed = sum(arr[seed_start:seed_end]) / n
    out[seed_end - 1] = seed
    k = 2.0 / (n + 1)
    for i in range(seed_end, len(arr)):
        if math.isnan(arr[i]):
            out[i] = float("nan")
        else:
            out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out

def atr(highs, lows, closes, n):
    """ATR — returns list same length."""
    tr = [float("nan")] * len(highs)
    for i in range(len(highs)):
        h, l, c = highs[i], lows[i], closes[i]
        if i == 0:
            tr[i] = h - l
        else:
            prev_c = closes[i - 1]
            tr[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))
    # RMA (Wilder MA)
    out = [float("nan")] * len(tr)
    if len(tr) < n:
        return out
    seed = sum(tr[:n]) / n
    out[n - 1] = seed
    for i in range(n, len(tr)):
        if math.isnan(tr[i]):
            out[i] = float("nan")
        else:
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out

def rsi(closes, n=14):
    """RSI — returns list same length."""
    out = [float("nan")] * len(closes)
    if len(closes) < n + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # seed
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    idx = n  # closes index for first RSI
    if avg_loss == 0:
        out[idx] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[idx] = 100 - 100 / (1 + rs)
    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out

def bollinger(closes, n=20, dev=2.0):
    """Returns (mid, upper, lower) lists."""
    mid = sma(closes, n)
    upper = [float("nan")] * len(closes)
    lower = [float("nan")] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1 : i + 1]
        m = mid[i]
        std = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        upper[i] = m + dev * std
        lower[i] = m - dev * std
    return mid, upper, lower

def macd(closes, fast=12, slow=26, signal_p=9):
    """Returns (macd_line, signal_line, histogram) lists."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else float("nan")
        for f, s in zip(ema_fast, ema_slow)
    ]
    signal_line = ema(macd_line, signal_p)
    histogram = [
        (m - s) if not (math.isnan(m) or math.isnan(s)) else float("nan")
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram

def supertrend(highs, lows, closes, period=10, mult=3.0):
    """
    Supertrend — returns (direction, trend_line) lists.
    direction: +1 = uptrend (bullish), -1 = downtrend (bearish)
    """
    n = len(closes)
    atr_vals = atr(highs, lows, closes, period)
    upper_basic = [float("nan")] * n
    lower_basic = [float("nan")] * n
    for i in range(n):
        if math.isnan(atr_vals[i]):
            continue
        mid = (highs[i] + lows[i]) / 2
        upper_basic[i] = mid + mult * atr_vals[i]
        lower_basic[i] = mid - mult * atr_vals[i]

    upper_final = [float("nan")] * n
    lower_final = [float("nan")] * n
    direction = [0] * n
    trend = [float("nan")] * n

    first = next((i for i in range(n) if not math.isnan(upper_basic[i])), None)
    if first is None:
        return direction, trend

    upper_final[first] = upper_basic[first]
    lower_final[first] = lower_basic[first]
    direction[first] = 1  # start bullish

    for i in range(first + 1, n):
        if math.isnan(upper_basic[i]) or math.isnan(lower_basic[i]):
            direction[i] = direction[i - 1]
            trend[i] = trend[i - 1]
            continue

        # lower_final
        if lower_basic[i] > lower_final[i - 1] or closes[i - 1] < lower_final[i - 1]:
            lower_final[i] = lower_basic[i]
        else:
            lower_final[i] = lower_final[i - 1]

        # upper_final
        if upper_basic[i] < upper_final[i - 1] or closes[i - 1] > upper_final[i - 1]:
            upper_final[i] = upper_basic[i]
        else:
            upper_final[i] = upper_final[i - 1]

        # direction
        prev_dir = direction[i - 1]
        if prev_dir == -1 and closes[i] > upper_final[i]:
            direction[i] = 1
        elif prev_dir == 1 and closes[i] < lower_final[i]:
            direction[i] = -1
        else:
            direction[i] = prev_dir

        trend[i] = lower_final[i] if direction[i] == 1 else upper_final[i]

    return direction, trend, upper_final, lower_final

def stoch_rsi(closes, rsi_period=14, stoch_period=14, smooth_k=3, smooth_d=3):
    """Returns (%K, %D) lists."""
    n = len(closes)
    rsi_vals = rsi(closes, rsi_period)
    raw_k = [float("nan")] * n
    for i in range(stoch_period - 1, n):
        window = rsi_vals[i - stoch_period + 1 : i + 1]
        valid = [v for v in window if not math.isnan(v)]
        if len(valid) < stoch_period:
            continue
        lo = min(valid)
        hi = max(valid)
        if hi == lo:
            raw_k[i] = 50.0
        else:
            raw_k[i] = (rsi_vals[i] - lo) / (hi - lo) * 100
    smooth_k_vals = sma(raw_k, smooth_k)
    smooth_d_vals = sma(smooth_k_vals, smooth_d)
    return smooth_k_vals, smooth_d_vals

def keltner(closes, highs, lows, ema_period=20, atr_period=10, mult=2.0):
    """Returns (mid, upper, lower) lists."""
    mid = ema(closes, ema_period)
    atr_vals = atr(highs, lows, closes, atr_period)
    upper = [
        (m + mult * a) if not (math.isnan(m) or math.isnan(a)) else float("nan")
        for m, a in zip(mid, atr_vals)
    ]
    lower = [
        (m - mult * a) if not (math.isnan(m) or math.isnan(a)) else float("nan")
        for m, a in zip(mid, atr_vals)
    ]
    return mid, upper, lower

def swing_low(lows, idx, lookback=5):
    start = max(0, idx - lookback)
    return min(lows[start : idx + 1])

def swing_high(highs, idx, lookback=5):
    start = max(0, idx - lookback)
    return max(highs[start : idx + 1])

# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

CAPITAL = 25_000.0
LEVERAGE = 5.0
FEE_RT = 0.0003   # 0.03% round-trip on notional
MAX_HOLD = 30     # bars
COOLDOWN = 2      # bars after close before re-entry

def run_backtest(candles, signal_fn, warmup):
    """
    signal_fn(i, candles, indicators) -> ('long'|'short'|None, sl_price, tp_price)
    warmup: number of bars needed before signals are valid
    Returns list of trade dicts.
    """
    n = len(candles)
    trades = []
    pos = None  # dict with entry info
    cooldown = 0

    indicators = signal_fn.prep(candles)

    for i in range(warmup, n - 1):
        c = candles[i]

        # ---- manage open position ----
        if pos is not None:
            hi = candles[i]["h"]
            lo = candles[i]["l"]
            close_price = None
            close_reason = None
            bars_held = i - pos["entry_bar"]

            if pos["side"] == "long":
                if lo <= pos["sl"]:
                    close_price = pos["sl"]
                    close_reason = "sl"
                elif hi >= pos["tp"]:
                    close_price = pos["tp"]
                    close_reason = "tp"
            else:  # short
                if hi >= pos["sl"]:
                    close_price = pos["sl"]
                    close_reason = "sl"
                elif lo <= pos["tp"]:
                    close_price = pos["tp"]
                    close_reason = "tp"

            if close_price is None and bars_held >= MAX_HOLD:
                close_price = candles[i]["c"]
                close_reason = "timeout"

            if close_price is not None:
                notional = CAPITAL * LEVERAGE
                if pos["side"] == "long":
                    pnl = (close_price - pos["entry_price"]) / pos["entry_price"] * notional
                else:
                    pnl = (pos["entry_price"] - close_price) / pos["entry_price"] * notional
                pnl -= notional * FEE_RT
                trades.append({
                    "entry_bar": pos["entry_bar"],
                    "exit_bar": i,
                    "side": pos["side"],
                    "entry_price": pos["entry_price"],
                    "exit_price": close_price,
                    "pnl": pnl,
                    "reason": close_reason,
                    "entry_t": candles[pos["entry_bar"]]["t"],
                    "exit_t": candles[i]["t"],
                })
                pos = None
                cooldown = COOLDOWN
                continue

        if pos is None:
            if cooldown > 0:
                cooldown -= 1
                continue
            # signal on last completed bar (i), enter on next bar open
            sig, sl, tp = signal_fn(i, candles, indicators)
            if sig is not None:
                entry_price = candles[i + 1]["o"]
                # recalc SL/TP relative to actual entry if they were offset from close
                # (pass through as-is; strategy sets them)
                pos = {
                    "side": sig,
                    "entry_price": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "entry_bar": i + 1,
                }

    return trades

def compute_metrics(trades, candles, start_bar, end_bar):
    """Compute daily PnL, WR, MaxDD for trades within [start_bar, end_bar)."""
    subset = [t for t in trades if start_bar <= t["entry_bar"] < end_bar]
    if not subset:
        return {"daily_pnl": 0, "wr": 0, "max_dd": 0, "num_trades": 0, "total_pnl": 0}

    # daily pnl
    day_pnl = defaultdict(float)
    for t in subset:
        day = t["entry_t"] // 86400
        day_pnl[day] += t["pnl"]
    days = sorted(day_pnl.keys())
    n_days = len(days)
    total_pnl = sum(t["pnl"] for t in subset)
    daily_pnl = total_pnl / n_days if n_days > 0 else 0

    # win rate
    wins = sum(1 for t in subset if t["pnl"] > 0)
    wr = wins / len(subset) * 100

    # max drawdown (equity curve)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in subset:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "daily_pnl": daily_pnl,
        "wr": wr,
        "max_dd": max_dd,
        "num_trades": len(subset),
        "total_pnl": total_pnl,
        "n_days": n_days,
    }

# ---------------------------------------------------------------------------
# Strategy 1: Supertrend
# ---------------------------------------------------------------------------

def make_supertrend(period, mult, tp_rr):
    class ST:
        def prep(self, candles):
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            closes = [c["c"] for c in candles]
            direction, trend, upper_final, lower_final = supertrend(highs, lows, closes, period, mult)
            atr_vals = atr(highs, lows, closes, period)
            return {"direction": direction, "trend": trend, "upper": upper_final, "lower": lower_final, "atr": atr_vals}

        def __call__(self, i, candles, ind):
            dir_ = ind["direction"]
            atr_ = ind["atr"]
            upper = ind["upper"]
            lower = ind["lower"]
            if i < 2:
                return None, None, None
            if math.isnan(atr_[i]) or atr_[i] == 0:
                return None, None, None
            prev_dir = dir_[i - 1]
            curr_dir = dir_[i]
            if prev_dir == -1 and curr_dir == 1:
                # flip to long
                sl = lower[i] - 0.1 * atr_[i]
                risk = candles[i]["c"] - sl
                if risk <= 0:
                    return None, None, None
                tp = candles[i]["c"] + tp_rr * risk
                return "long", sl, tp
            elif prev_dir == 1 and curr_dir == -1:
                # flip to short
                sl = upper[i] + 0.1 * atr_[i]
                risk = sl - candles[i]["c"]
                if risk <= 0:
                    return None, None, None
                tp = candles[i]["c"] - tp_rr * risk
                return "short", sl, tp
            return None, None, None

    fn = ST()
    fn.name = f"Supertrend(p={period},m={mult},rr={tp_rr})"
    fn.params = {"period": period, "mult": mult, "tp_rr": tp_rr}
    fn.warmup = period * 3
    return fn

# ---------------------------------------------------------------------------
# Strategy 2: Bollinger Band Mean Reversion
# ---------------------------------------------------------------------------

def make_bb_reversion(dev, rsi_os, tp_rr):
    class BB:
        def prep(self, candles):
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            mid, upper, lower = bollinger(closes, 20, dev)
            rsi_vals = rsi(closes, 14)
            atr_vals = atr(highs, lows, closes, 14)
            return {"mid": mid, "upper": upper, "lower": lower, "rsi": rsi_vals, "atr": atr_vals}

        def __call__(self, i, candles, ind):
            if i < 25:
                return None, None, None
            mid = ind["mid"][i]
            upper = ind["upper"][i]
            lower = ind["lower"][i]
            rsi_v = ind["rsi"][i]
            atr_v = ind["atr"][i]
            c = candles[i]["c"]
            p = candles[i - 1]["c"]
            if any(math.isnan(x) for x in [mid, upper, lower, rsi_v, atr_v]):
                return None, None, None
            rsi_ob = 100 - rsi_os
            if c < lower and rsi_v < rsi_os and c > p:
                sl_swing = min(candles[max(0, i-5):i+1], key=lambda x: x["l"])["l"] - 0.5 * atr_v
                risk = c - sl_swing
                if risk <= 0:
                    return None, None, None
                tp = max(mid, c + tp_rr * risk)
                return "long", sl_swing, tp
            if c > upper and rsi_v > rsi_ob and c < p:
                sl_swing = max(candles[max(0, i-5):i+1], key=lambda x: x["h"])["h"] + 0.5 * atr_v
                risk = sl_swing - c
                if risk <= 0:
                    return None, None, None
                tp = min(mid, c - tp_rr * risk)
                return "short", sl_swing, tp
            return None, None, None

    fn = BB()
    fn.name = f"BB_Reversion(dev={dev},os={rsi_os},rr={tp_rr})"
    fn.params = {"dev": dev, "rsi_os": rsi_os, "tp_rr": tp_rr}
    fn.warmup = 50
    return fn

# ---------------------------------------------------------------------------
# Strategy 3: MACD Momentum
# ---------------------------------------------------------------------------

def make_macd_momentum(fast, slow, signal_p, tp_rr):
    class MACD:
        def prep(self, candles):
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            ml, sl2, hist = macd(closes, fast, slow, signal_p)
            ema50 = ema(closes, 50)
            atr_vals = atr(highs, lows, closes, 14)
            return {"hist": hist, "ema50": ema50, "atr": atr_vals, "highs": highs, "lows": lows}

        def __call__(self, i, candles, ind):
            if i < slow + signal_p + 5:
                return None, None, None
            hist = ind["hist"]
            e50 = ind["ema50"][i]
            atr_v = ind["atr"][i]
            c = candles[i]["c"]
            if any(math.isnan(x) for x in [hist[i], hist[i-1], e50, atr_v]):
                return None, None, None
            crossed_above = hist[i] > 0 and hist[i - 1] <= 0
            crossed_below = hist[i] < 0 and hist[i - 1] >= 0
            if crossed_above and c > e50:
                sw_low = swing_low(ind["lows"], i, 5)
                sl = sw_low - 0.1 * atr_v
                risk = c - sl
                if risk <= 0:
                    return None, None, None
                tp = c + tp_rr * risk
                return "long", sl, tp
            if crossed_below and c < e50:
                sw_high = swing_high(ind["highs"], i, 5)
                sl = sw_high + 0.1 * atr_v
                risk = sl - c
                if risk <= 0:
                    return None, None, None
                tp = c - tp_rr * risk
                return "short", sl, tp
            return None, None, None

    fn = MACD()
    fn.name = f"MACD(f={fast},s={slow},sig={signal_p},rr={tp_rr})"
    fn.params = {"fast": fast, "slow": slow, "signal_p": signal_p, "tp_rr": tp_rr}
    fn.warmup = slow + signal_p + 55
    return fn

# ---------------------------------------------------------------------------
# Strategy 4: StochRSI + EMA Trend
# ---------------------------------------------------------------------------

def make_stoch_rsi_trend(tp_rr, ema_period):
    class SRT:
        def prep(self, candles):
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            pct_k, pct_d = stoch_rsi(closes, 14, 14, 3, 3)
            ema_vals = ema(closes, ema_period)
            atr_vals = atr(highs, lows, closes, 14)
            return {"k": pct_k, "d": pct_d, "ema": ema_vals, "atr": atr_vals, "highs": highs, "lows": lows}

        def __call__(self, i, candles, ind):
            warmup_bars = ema_period + 14 + 14 + 10
            if i < warmup_bars:
                return None, None, None
            k = ind["k"]
            d = ind["d"]
            e = ind["ema"]
            atr_v = ind["atr"][i]
            c = candles[i]["c"]
            if any(math.isnan(x) for x in [k[i], k[i-1], d[i], d[i-1], e[i], e[i-10], atr_v]):
                return None, None, None
            ema_slope_up = e[i] > e[i - 10]
            ema_slope_dn = e[i] < e[i - 10]
            k_cross_up = k[i] > d[i] and k[i - 1] <= d[i - 1] and k[i - 1] < 25
            k_cross_dn = k[i] < d[i] and k[i - 1] >= d[i - 1] and k[i - 1] > 75
            if ema_slope_up and k_cross_up:
                sw_low = swing_low(ind["lows"], i, 5)
                sl = sw_low - 0.1 * atr_v
                risk = c - sl
                if risk <= 0:
                    return None, None, None
                tp = c + tp_rr * risk
                return "long", sl, tp
            if ema_slope_dn and k_cross_dn:
                sw_high = swing_high(ind["highs"], i, 5)
                sl = sw_high + 0.1 * atr_v
                risk = sl - c
                if risk <= 0:
                    return None, None, None
                tp = c - tp_rr * risk
                return "short", sl, tp
            return None, None, None

    fn = SRT()
    fn.name = f"StochRSI_EMA(rr={tp_rr},ema={ema_period})"
    fn.params = {"tp_rr": tp_rr, "ema_period": ema_period}
    fn.warmup = ema_period + 50
    return fn

# ---------------------------------------------------------------------------
# Strategy 5: Keltner Channel Breakout
# ---------------------------------------------------------------------------

def make_keltner_breakout(mult, tp_rr):
    class KC:
        def prep(self, candles):
            closes = [c["c"] for c in candles]
            highs = [c["h"] for c in candles]
            lows = [c["l"] for c in candles]
            mid, upper, lower = keltner(closes, highs, lows, 20, 10, mult)
            rsi_vals = rsi(closes, 14)
            atr_vals = atr(highs, lows, closes, 14)
            return {"mid": mid, "upper": upper, "lower": lower, "rsi": rsi_vals, "atr": atr_vals}

        def __call__(self, i, candles, ind):
            if i < 30:
                return None, None, None
            upper = ind["upper"][i]
            lower = ind["lower"][i]
            mid = ind["mid"][i]
            rsi_v = ind["rsi"][i]
            atr_v = ind["atr"][i]
            c = candles[i]["c"]
            if any(math.isnan(x) for x in [upper, lower, mid, rsi_v, atr_v]):
                return None, None, None
            # RSI crossed above 55 in last 2 bars
            rsi_cross_above = (
                rsi_v > 55 and (
                    ind["rsi"][i-1] < 55 or
                    (i >= 2 and ind["rsi"][i-2] < 55 and not math.isnan(ind["rsi"][i-2]))
                )
            )
            rsi_cross_below = (
                rsi_v < 45 and (
                    ind["rsi"][i-1] > 45 or
                    (i >= 2 and ind["rsi"][i-2] > 45 and not math.isnan(ind["rsi"][i-2]))
                )
            )
            if c > upper and rsi_v > 55 and rsi_cross_above:
                sl = mid - 0.1 * atr_v
                risk = c - sl
                if risk <= 0:
                    return None, None, None
                tp = c + tp_rr * risk
                return "long", sl, tp
            if c < lower and rsi_v < 45 and rsi_cross_below:
                sl = mid + 0.1 * atr_v
                risk = sl - c
                if risk <= 0:
                    return None, None, None
                tp = c - tp_rr * risk
                return "short", sl, tp
            return None, None, None

    fn = KC()
    fn.name = f"Keltner_Breakout(mult={mult},rr={tp_rr})"
    fn.params = {"mult": mult, "tp_rr": tp_rr}
    fn.warmup = 40
    return fn

# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def split_candles(candles, train_days=60):
    """Split into train/test by timestamp."""
    if not candles:
        return [], []
    first_t = candles[0]["t"]
    split_t = first_t + train_days * 86400
    train = [c for c in candles if c["t"] < split_t]
    test = [c for c in candles if c["t"] >= split_t]
    return train, test

def main():
    candles = fetch_candles(90)

    # Hyperliquid 5m API caps at ~5000 bars (~17.5 days).
    # Use a 70/30 ratio on whatever data we actually got.
    n_total = len(candles)
    actual_days = (candles[-1]["t"] - candles[0]["t"]) / 86400 if n_total > 1 else 0
    train_ratio = 0.70
    train_end_bar = int(n_total * train_ratio)
    test_start_bar = train_end_bar

    print(f"\nActual data span: {actual_days:.1f} days ({n_total} bars)")
    print(f"Train bars: 0 to {train_end_bar} (~{train_end_bar*5/60/24:.1f} days)")
    print(f"Test (OOS) bars: {test_start_bar} to {n_total} (~{(n_total-test_start_bar)*5/60/24:.1f} days)")
    if actual_days < 30:
        print(f"  NOTE: Only {actual_days:.1f} days available (API limit). OOS = last {(n_total-test_start_bar)*5/60/24:.1f} days.")

    all_combos = []

    # Build all strategy factories
    strategy_groups = [
        # Strategy 1: Supertrend
        ("Supertrend", [
            make_supertrend(p, m, r)
            for p in [7, 10, 14]
            for m in [2.0, 2.5, 3.0, 3.5]
            for r in [2.0, 2.5, 3.0]
        ]),
        # Strategy 2: BB Mean Reversion
        ("BB_Reversion", [
            make_bb_reversion(d, os_, r)
            for d in [1.8, 2.0, 2.2]
            for os_ in [30, 35]
            for r in [1.5, 2.0]
        ]),
        # Strategy 3: MACD Momentum
        ("MACD_Momentum", [
            make_macd_momentum(f, s, 9, r)
            for f in [8, 12]
            for s in [21, 26]
            for r in [2.0, 2.5, 3.0]
        ]),
        # Strategy 4: StochRSI + EMA
        ("StochRSI_EMA", [
            make_stoch_rsi_trend(r, ep)
            for r in [2.0, 2.5, 3.0]
            for ep in [50, 100]
        ]),
        # Strategy 5: Keltner Breakout
        ("Keltner_Breakout", [
            make_keltner_breakout(m, r)
            for m in [1.5, 2.0, 2.5]
            for r in [2.0, 2.5, 3.0]
        ]),
    ]

    results = []
    for strat_name, combos in strategy_groups:
        print(f"\nTesting Strategy: {strat_name} ({len(combos)} combos)...")
        for idx, fn in enumerate(combos):
            sys.stdout.write(f"\r  Combo {idx+1}/{len(combos)}: {fn.name}      ")
            sys.stdout.flush()
            try:
                trades = run_backtest(candles, fn, fn.warmup)
                oos_metrics = compute_metrics(trades, candles, test_start_bar, n_total)
                train_metrics = compute_metrics(trades, candles, 0, train_end_bar)
                row = {
                    "strategy": strat_name,
                    "name": fn.name,
                    "params": fn.params,
                    "oos_daily_pnl": round(oos_metrics["daily_pnl"], 2),
                    "oos_wr": round(oos_metrics["wr"], 1),
                    "oos_max_dd": round(oos_metrics["max_dd"], 2),
                    "oos_trades": oos_metrics["num_trades"],
                    "oos_total_pnl": round(oos_metrics.get("total_pnl", 0), 2),
                    "oos_n_days": oos_metrics.get("n_days", 0),
                    "train_daily_pnl": round(train_metrics["daily_pnl"], 2),
                    "train_wr": round(train_metrics["wr"], 1),
                    "train_trades": train_metrics["num_trades"],
                }
                results.append(row)
            except Exception as e:
                pass
        print()  # newline after progress

    # Sort by OOS daily PnL
    results.sort(key=lambda x: x["oos_daily_pnl"], reverse=True)

    # Save
    os.makedirs("trades", exist_ok=True)
    out_path = "trades/scalper_v2_sweep.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} results to {out_path}")

    # Print top 10
    print("\n" + "="*100)
    print("TOP 10 RESULTS (OOS = Out-of-Sample, last 30 days)")
    print("="*100)
    header = f"{'#':<3} {'Strategy':<20} {'Params':<45} {'OOS $/day':>10} {'WR%':>6} {'MaxDD':>10} {'Trades':>7} {'Train $/day':>12}"
    print(header)
    print("-" * 115)
    for rank, r in enumerate(results[:10], 1):
        params_str = str(r["params"])[:44]
        print(
            f"{rank:<3} {r['strategy']:<20} {params_str:<45} "
            f"{r['oos_daily_pnl']:>10.2f} {r['oos_wr']:>6.1f} "
            f"{r['oos_max_dd']:>10.2f} {r['oos_trades']:>7} "
            f"{r['train_daily_pnl']:>12.2f}"
        )

    # Best combo detail
    if results:
        best = results[0]
        print("\n" + "="*80)
        print("BEST STRATEGY DETAILS")
        print("="*80)
        print(f"Name:           {best['name']}")
        print(f"Strategy:       {best['strategy']}")
        print(f"Params:         {best['params']}")
        print(f"OOS Daily PnL:  ${best['oos_daily_pnl']:.2f}/day")
        print(f"OOS Win Rate:   {best['oos_wr']:.1f}%")
        print(f"OOS Max DD:     ${best['oos_max_dd']:.2f}")
        print(f"OOS Trades:     {best['oos_trades']} over {best['oos_n_days']} days")
        print(f"OOS Total PnL:  ${best['oos_total_pnl']:.2f}")
        print(f"Train Daily PnL: ${best['train_daily_pnl']:.2f}/day")
        print(f"Train Win Rate:  {best['train_wr']:.1f}%")
        print(f"Target ($500/day): {'YES' if best['oos_daily_pnl'] >= 500 else 'NO (best is below target)'}")
        print()

    # Also show any that beat $500/day target
    winners = [r for r in results if r["oos_daily_pnl"] >= 500]
    if winners:
        print(f"\n{len(winners)} combo(s) beat $500/day target on OOS data:")
        for r in winners[:5]:
            print(f"  {r['name']}: ${r['oos_daily_pnl']:.2f}/day, WR={r['oos_wr']:.1f}%, MaxDD=${r['oos_max_dd']:.2f}, Trades={r['oos_trades']}")
    else:
        print("\nNo combos hit $500/day on OOS data. Top 3 closest:")
        for r in results[:3]:
            print(f"  {r['name']}: ${r['oos_daily_pnl']:.2f}/day, WR={r['oos_wr']:.1f}%, MaxDD=${r['oos_max_dd']:.2f}, Trades={r['oos_trades']}")

if __name__ == "__main__":
    main()

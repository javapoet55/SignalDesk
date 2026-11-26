"""
Smart Money Structure | GainzAlgo  —  "Strength" scanner (Python conversion for the FinViz agent)

What it scans for
  The indicator's table shows "Strength": how many of 7 timeframes are trending up, as a percent.
      Strength = (trend1M + trend5M + trend15M + trend30M + trend1H + trend4H + trendD) / 7 * 100
  For each timeframe:  +1 if close > EMA20 and close > VWAP of that timeframe
                       -1 if close < EMA20 and close < VWAP
                        0 otherwise (mixed, or not enough bars)
  So Strength can only be 100, 86, 71, 57, ... and  Strength >= 85  means 6 or 7 points:
  all 7 timeframes up, or 6 up and 1 mixed (none down).

  This file lists every scan ticker with its Strength; tickers with Strength >= MIN_STRENGTH are
  marked BUY (and so go to your TradingView "FinViz BUY" list).

How each timeframe is built (same as TradingView with a regular-session chart)
  1M         FinViz 1-minute bars
  5M, 15M    built from the 1-minute bars, candles starting 9:30, 9:35, ... (TradingView alignment)
  30M        FinViz 30-minute bars
  1H, 4H     built from the 30-minute bars, starting 9:30 (1H: 9:30, 10:30, ... 15:30; 4H: 9:30, 13:30)
  D          FinViz daily bars
  EMA20      Pine's ta.ema: the first value is the 20-bar average, then the usual EMA
  VWAP       Pine's ta.vwap(hlc3) on that timeframe's bars, restarting each session. On the daily
             timeframe a session is one bar, so VWAP = (high + low + close) / 3 of the day
  close      the day's close (what the indicator compares on a daily chart); while the market is
             open with "intraday" price basis, the latest price

  Inputs, drawings, labels and the other tables in the Pine script (CHoCH/BOS lines, buy/sell
  labels, liquidity zones, ...) don't change Strength, so they are not part of this scanner.

On the FinViz agent: upload this file on the Strategies tab. TIMEFRAMES below makes the agent supply
1-minute and 30-minute bars as well as daily bars. Run it with Price basis "Latest close" after the
1:00 pm Pacific close (e.g. every weekday at 13:10) to scan that day's final values.
"""
import math

TITLE = "Smart Money Strength"
DESCRIPTION = "GainzAlgo Smart Money Structure: Strength across 1M, 5M, 15M, 30M, 1H, 4H and D (BUY when >= 85)"
TIMEFRAMES = ["1m", "30m", "1d"]   # bar sizes the agent supplies (see above)
MIN_STRENGTH = 85                  # BUY when Strength >= this
EMA_LEN = 20                       # ta.ema(close, 20) in the indicator

SESSION_OPEN = 9 * 60 + 30         # 9:30 New York
TF_ORDER = ["1M", "5M", "15M", "30M", "1H", "4H", "D"]
FIELDS = ["Ticker", "Status", "Strength", "Up_TFs", "Down_TFs", "Confidence_%", "Close", "Chg_%",
          "1M", "5M", "15M", "30M", "1H", "4H", "D", "As_Of", "Detail"]
ARROW = {1: "▲", -1: "▼", 0: "━"}


# ─────────────────────────────────────────────────────────────────────────────
#  Indicator maths (Pine equivalents)
# ─────────────────────────────────────────────────────────────────────────────
def pine_ema(values, length=EMA_LEN):
    """Last value of Pine's ta.ema: SMA of the first `length` values, then alpha = 2 / (length + 1).
    None if there are fewer than `length` values."""
    if len(values) < length:
        return None
    alpha = 2.0 / (length + 1)
    ema = math.fsum(values[:length]) / length
    for v in values[length:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def session_vwap(bars):
    """ta.vwap(hlc3) at the last bar: volume-weighted hlc3 over the last bar's session (day)."""
    if not bars:
        return None
    day = bars[-1]["day"]
    num = den = 0.0
    for b in bars:
        if b["day"] == day:
            num += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            den += b["v"]
    return num / den if den > 0 else None


def trend(close, ema, vwap):
    """+1 above both, -1 below both, else 0 (na compares as false in Pine)."""
    if close is None or ema is None or vwap is None:
        return 0
    if close > ema and close > vwap:
        return 1
    if close < ema and close < vwap:
        return -1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  Bars
# ─────────────────────────────────────────────────────────────────────────────
def minute_bars(history, sym, asof):
    """[{day, start, o, h, l, c, v}] from a minute history {'YYYY-MM-DD HH:MM': {sym: bar}} up to the
    as-of day, oldest first. `start` = minutes after midnight New York (the candle's start)."""
    out = []
    for key in sorted(history):
        if key[:10] > asof:
            continue
        bar = history[key].get(sym)
        if not bar:
            continue
        hh, mm = int(key[11:13]), int(key[14:16])
        out.append({"day": key[:10], "start": hh * 60 + mm, "o": float(bar["o"]), "h": float(bar["h"]),
                    "l": float(bar["l"]), "c": float(bar["c"]), "v": float(bar.get("v") or 0)})
    return out


def regroup(bars, minutes):
    """Bars combined into `minutes`-long candles starting 9:30 + k * minutes each session."""
    out = []
    for b in bars:
        start = SESSION_OPEN + (b["start"] - SESSION_OPEN) // minutes * minutes
        last = out[-1] if out else None
        if last is not None and last["day"] == b["day"] and last["start"] == start:
            last["h"] = max(last["h"], b["h"]); last["l"] = min(last["l"], b["l"])
            last["c"] = b["c"]; last["v"] += b["v"]
        else:
            out.append(dict(b, start=start))
    return out


def daily_bars(history, sym, asof):
    return [{"day": d, "o": float(history[d][sym]["o"]), "h": float(history[d][sym]["h"]),
             "l": float(history[d][sym]["l"]), "c": float(history[d][sym]["c"]),
             "v": float(history[d][sym].get("v") or 0)}
            for d in sorted(history) if d <= asof and sym in history[d]]


def _cache_symbol(sym, present):
    """FinViz writes class shares as BRK-B; some feeds use BRK.B. Try both."""
    for cand in (sym, sym.replace("-", "."), sym.replace(".", "-")):
        if cand in present:
            return cand
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  One ticker
# ─────────────────────────────────────────────────────────────────────────────
def strength_for(daily, m1, m30):
    """(close, {tf: trend}, notes) for one ticker. daily/m1/m30 = bar lists up to the as-of day."""
    close = daily[-1]["c"] if daily else None
    series = {"1M": m1, "5M": regroup(m1, 5), "15M": regroup(m1, 15),
              "30M": m30, "1H": regroup(m30, 60), "4H": regroup(m30, 240)}
    trends, notes = {}, []
    for tf in TF_ORDER[:-1]:
        bars = series[tf]
        if bars and daily and bars[-1]["day"] != daily[-1]["day"]:
            notes.append(f"{tf}: no bars on {daily[-1]['day']}")
            trends[tf] = 0
            continue
        ema = pine_ema([b["c"] for b in bars])
        if ema is None:
            notes.append(f"{tf}: only {len(bars)} bars")
        trends[tf] = trend(close, ema, session_vwap(bars))
    d_ema = pine_ema([b["c"] for b in daily])
    d_vwap = (daily[-1]["h"] + daily[-1]["l"] + daily[-1]["c"]) / 3.0 if daily else None
    if d_ema is None:
        notes.append(f"D: only {len(daily)} days")
    trends["D"] = trend(close, d_ema, d_vwap)
    return close, trends, notes


def confidence(raw):
    a = abs(raw)
    return 90.0 if a == 7 else 75.0 if a >= 4 else 60.0 if a >= 2 else 50.0


# ─────────────────────────────────────────────────────────────────────────────
#  FinViz agent entry point
# ─────────────────────────────────────────────────────────────────────────────
def finviz_signals(tickers, asof, cache):
    """Called by the FinViz agent. cache = daily history, plus cache['timeframes'] = {'1m': ..., '30m': ...}."""
    daily_hist = cache.get("history") or {}
    tfs = cache.get("timeframes") or {}
    m1_hist, m30_hist = tfs.get("1m") or {}, tfs.get("30m") or {}
    present = set(cache.get("tickers") or {s for bars in daily_hist.values() for s in bars})
    print(f"Smart Money Strength: {len(tickers)} tickers as of {asof}; BUY when Strength >= {MIN_STRENGTH}")
    print(f"  bars: {len({d for d in daily_hist if d <= asof})} days, "
          f"{len({k[:10] for k in m1_hist})} sessions of 1-minute, {len({k[:10] for k in m30_hist})} of 30-minute")
    if not m1_hist or not m30_hist:
        print("  WARNING: minute bars are missing, so the intraday timeframes count as 0 for every ticker. "
              "Run this on the FinViz agent (FinViz prices) so it gets TIMEFRAMES.")
    rows = []
    for ticker in tickers:
        row = {k: "" for k in FIELDS}
        row["Ticker"] = ticker
        sym = _cache_symbol(ticker, present)
        daily = daily_bars(daily_hist, sym, asof) if sym else []
        if not daily:
            row.update(Status="NOT IN PRICE CACHE", Detail="no daily prices for this ticker")
            rows.append(row)
            continue
        try:
            close, trends, notes = strength_for(daily, minute_bars(m1_hist, sym, asof), minute_bars(m30_hist, sym, asof))
        except (KeyError, TypeError, ValueError) as err:
            row.update(Status="ERROR", Detail=f"{type(err).__name__}: {err}")
            rows.append(row)
            continue
        raw = sum(trends.values())
        strength = raw / 7 * 100
        prev = daily[-2]["c"] if len(daily) >= 2 else None
        row.update({tf: ARROW[trends[tf]] for tf in TF_ORDER})
        row.update(Strength=round(strength), Up_TFs=sum(1 for t in trends.values() if t > 0),
                   Down_TFs=sum(1 for t in trends.values() if t < 0), **{"Confidence_%": confidence(raw)},
                   Close=round(close, 4), As_Of=daily[-1]["day"],
                   **{"Chg_%": round((close - prev) / prev * 100, 2) if prev else ""})
        row["Status"] = "BUY" if strength >= MIN_STRENGTH else "NO SIGNAL"
        if daily[-1]["day"] != asof:
            notes.insert(0, f"last daily bar {daily[-1]['day']}, before {asof}")
        row["Detail"] = "; ".join(notes)
        rows.append(row)
    rows.sort(key=lambda r: (r["Strength"] == "", -(r["Strength"] or 0), r["Ticker"]))
    buys = [r["Ticker"] for r in rows if r["Status"] == "BUY"]
    print(f"Strength >= {MIN_STRENGTH}: {len(buys)} of {len(tickers)} tickers"
          + (f": {', '.join(buys[:60])}" + (" …" if len(buys) > 60 else "") if buys else ""))
    return rows

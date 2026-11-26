#!/usr/bin/env python3
"""
MACDgen V5 + Regime Filter  ×  FinViz agent tickers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Runs your V5 strategy (macdgen_v5_regime_backtest.py, used UNCHANGED: its
functions and settings are imported, not copied) on only the tickers from a
FinViz agent run (tickers.txt: line 1 "Run: <date time>", line 2 comma-separated
tickers).

Default mode: SIGNALS AS OF THE RUN'S TRADING DAY
  For every ticker: does V5 fire a buy signal on that day? If not, which exact
  rule stopped it. Also shows the market-regime verdict for that day, and, once
  your price cache has later days, how each signal's trade has done so far
  (same take-profit / trailing-stop / 10-day rules as the backtest).

Optional:  --backtest START END
  Runs the original V5 window backtest restricted to these tickers. WARNING:
  these tickers were chosen because they are winners NOW, so testing them on
  earlier dates overstates results (hindsight / selection bias).

Usage examples
  python3 macdgen_v5_finviz.py
  python3 macdgen_v5_finviz.py --tickers ~/Downloads/tickers.txt
  python3 macdgen_v5_finviz.py --asof 2026-10-23
  python3 macdgen_v5_finviz.py --backtest 2026-09-01 2026-10-23
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_STRATEGY = HERE / "macdgen_v5_regime_backtest.py"
DEFAULT_TICKERS = Path.home() / "finviz_agent" / "reports" / "latest_tickers.txt"
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
# Only the two core V5 functions are required. Everything else is optional and has a fallback,
# so V5 variants (renamed settings, no regime filter, different sizing) work too.
REQUIRED = ["passes_v5_filters", "detect_macd_cross_up"]
FILTER_SETTINGS = ["MIN_BARS", "MIN_PRICE", "MIN_AVG_VOL_10D", "CHG_MIN", "MAX_CHG_PCT", "MAX_WEEK_PCT",
                   "MAX_VOL_RATIO"]  # used only to explain WHY a ticker was filtered out
BACKTEST_FUNCS = ["build_spy_regime", "run_backtest", "print_window_summary"]
GENERIC_BLOCK = "did not pass the strategy's price/volume filters"


class UserError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Loading inputs
# ─────────────────────────────────────────────────────────────────────────────
def load_strategy(path: Path):
    if not path.is_file():
        raise UserError(f"Strategy file not found: {path}\n"
                        f"Put this script in the same folder as macdgen_v5_regime_backtest.py, "
                        f"or pass --strategy /path/to/macdgen_v5_regime_backtest.py")
    spec = importlib.util.spec_from_file_location("macdgen_v5_strategy", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macdgen_v5_strategy"] = mod
    spec.loader.exec_module(mod)  # defines functions/constants only; its main() is not run
    missing = [n for n in REQUIRED if not callable(getattr(mod, n, None))]
    if missing:
        raise UserError(f"{path.name} is missing {missing}; is it a V5-style strategy?")
    problem = signature_problem(mod)
    if problem:
        raise UserError(problem)
    return mod


def signature_problem(mod) -> Optional[str]:
    """V5 calls passes_v5_filters(idx, closes, volumes) and detect_macd_cross_up(idx, closes)."""
    import inspect
    for fn, args in (("passes_v5_filters", (0, [], [])), ("detect_macd_cross_up", (0, []))):
        try:
            inspect.signature(getattr(mod, fn)).bind(*args)
        except TypeError:
            want = "idx, closes, volumes" if fn == "passes_v5_filters" else "idx, closes"
            return f"{fn} must accept ({want}) like V5 does; yours is {fn}{inspect.signature(getattr(mod, fn))}."
        except ValueError:
            pass  # signature not introspectable (e.g. C function): let it run
    return None


def setting(mod, name: str, default=None):
    return getattr(mod, name, default)


def call_filters(mod, idx, closes, volumes) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """V5 returns (ok, chg, wk, vr); a variant may return just True/False. Missing values are computed here."""
    res = mod.passes_v5_filters(idx, closes, volumes)
    vals = list(res) if isinstance(res, (tuple, list)) else [res]
    ok = bool(vals[0])
    chg, wk, vr = (vals + [None, None, None])[1:4]
    if ok and chg is None:
        c, p = closes[idx], closes[idx - 1]
        chg = (c - p) / p * 100 if p else 0.0
    if ok and wk is None:
        wk = (closes[idx] - closes[idx - 5]) / closes[idx - 5] * 100 if idx >= 5 and closes[idx - 5] else 0.0
    if ok and vr is None:
        avg = float(np.mean(volumes[max(0, idx - 19):idx + 1]))
        vr = volumes[idx] / avg if avg else 0.0
    return ok, chg, wk, vr


def load_mcap_csv(path: str) -> Optional[set]:
    if not path or not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {(r.get("Ticker") or "").strip().upper() for r in csv.DictReader(f) if (r.get("Ticker") or "").strip()}


def read_tickers(path: Path) -> Tuple[Optional[date], str, List[str], List[str]]:
    """Return (run date, header line, tickers in order without duplicates, rejected tokens)."""
    if path.is_dir():
        path = path / "tickers.txt"
    if not path.is_file():
        raise UserError(f"Tickers file not found: {path}\n"
                        f"Download it from your page (Latest tickers → tickers.txt) and pass --tickers PATH.")
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines()]
    header = lines[0] if lines else ""
    m = re.match(r"^Run:\s*(\d{4}-\d{2}-\d{2})", header)
    run_date = date.fromisoformat(m.group(1)) if m else None
    body = lines[1:] if m else lines
    tickers: List[str] = []
    rejected: List[str] = []
    for token in re.split(r"[,\s]+", " ".join(body)):
        t = token.strip().upper()
        if not t:
            continue
        if not TICKER_RE.match(t):
            rejected.append(token)
        elif t not in tickers:
            tickers.append(t)
    if not tickers:
        raise UserError(f"No tickers found in {path}")
    return run_date, header, tickers, rejected


def cache_symbol(sym: str, cache_syms: set) -> Optional[str]:
    """FinViz writes class shares as BRK-B; many data feeds use BRK.B. Try both."""
    for cand in (sym, sym.replace("-", "."), sym.replace(".", "-")):
        if cand in cache_syms:
            return cand
    return None


def build_arrays(cache: dict, all_dates: List[str], sym: str):
    """Same forward-fill construction as the original script's main()."""
    closes, highs, lows, volumes = [], [], [], []
    last_c = last_h = last_l = np.nan
    for dt in all_dates:
        bar = cache["history"].get(dt, {}).get(sym)
        if bar:
            last_c = float(bar["c"]); last_h = float(bar["h"]); last_l = float(bar["l"])
            closes.append(last_c); highs.append(last_h); lows.append(last_l)
            volumes.append(float(bar.get("v", 0)))
        else:
            closes.append(last_c); highs.append(last_h); lows.append(last_l)
            volumes.append(0.0)
    if all(np.isnan(x) for x in closes):
        return None
    return closes, highs, lows, volumes


def last_weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────────────────────────────────────────────────
#  Why a ticker did not signal (mirrors passes_v5_filters exactly; verified)
# ─────────────────────────────────────────────────────────────────────────────
def v5_block_reason(mod, idx, closes, volumes) -> Optional[str]:
    if not all(hasattr(mod, k) for k in FILTER_SETTINGS):
        return None  # settings named differently in this strategy: no detailed explanation
    n = len(closes)
    if idx < mod.MIN_BARS or idx >= n:
        return f"fewer than {mod.MIN_BARS} days of price history"
    c_today, c_prev = closes[idx], closes[idx - 1]
    if np.isnan(c_today) or np.isnan(c_prev):
        return "no price on the day or the day before"
    if c_today < mod.MIN_PRICE:
        return f"price ${c_today:,.2f} is under ${mod.MIN_PRICE:,.2f}"
    avg_vol_10d = float(np.mean(volumes[max(0, idx - 9):idx + 1]))
    if avg_vol_10d < mod.MIN_AVG_VOL_10D:
        return f"10-day avg volume {avg_vol_10d:,.0f} is under {mod.MIN_AVG_VOL_10D:,}"
    chg_pct = (c_today - c_prev) / c_prev * 100 if c_prev else 0
    if chg_pct < mod.CHG_MIN:
        return f"day change {chg_pct:+.2f}% is under +{mod.CHG_MIN:g}%"
    if chg_pct > mod.MAX_CHG_PCT:
        return f"day change {chg_pct:+.2f}% is over +{mod.MAX_CHG_PCT:g}% (too extended)"
    week_chg = (c_today - closes[idx - 5]) / closes[idx - 5] * 100 if idx >= 5 and closes[idx - 5] > 0 else 0.0
    if week_chg > mod.MAX_WEEK_PCT:
        return f"5-day change {week_chg:+.2f}% is over +{mod.MAX_WEEK_PCT:g}% (too extended)"
    avg_vol_20d = float(np.mean(volumes[max(0, idx - 19):idx + 1]))
    vol_ratio = volumes[idx] / avg_vol_20d if avg_vol_20d > 0 else 0
    if vol_ratio >= mod.MAX_VOL_RATIO:
        return f"volume is {vol_ratio:.1f}x its 20-day average (limit {mod.MAX_VOL_RATIO:g}x)"
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Signal scan
# ─────────────────────────────────────────────────────────────────────────────
FIELDS = ["AsOf", "Ticker", "CacheTicker", "Status", "Detail", "Price", "Chg_%", "Wk_%", "VR", "Tier",
          "Size_$", "Regime", "SPY_Close", "SPY_MA20", "SPY_ATR_%", "DaysTracked", "TradeState",
          "EffRet_%", "MaxGain_%", "TP_Hit", "ExitWhy", "PL_$"]


def _r(x, nd=2):
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def scan(mod, cache, all_dates, asof: str, tickers: List[str], mcap_universe) -> List[dict]:
    if callable(getattr(mod, "build_spy_regime", None)):
        regime_all = mod.build_spy_regime(cache, all_dates)
    else:  # this strategy has no market-regime filter: every day is allowed
        regime_all = {asof: {"allow": True, "reason": "NO FILTER", "spy_close": np.nan, "spy_ma": np.nan,
                             "atr_pct": np.nan}}
    reg = regime_all.get(asof, {"allow": False, "reason": "NO_DATA", "spy_close": np.nan,
                                "spy_ma": np.nan, "atr_pct": np.nan})
    idx = all_dates.index(asof)
    cache_syms = set(cache["tickers"])
    rows: List[dict] = []
    for sym in tickers:
        row = {k: "" for k in FIELDS}
        row.update(AsOf=asof, Ticker=sym, Regime=reg["reason"], SPY_Close=_r(reg["spy_close"]),
                   SPY_MA20=_r(reg["spy_ma"]), **{"SPY_ATR_%": _r(reg["atr_pct"])})
        csym = cache_symbol(sym, cache_syms)
        if csym is None:
            row.update(Status="NOT IN PRICE CACHE", Detail="ticker not in history_cache.json")
            rows.append(row)
            continue
        row["CacheTicker"] = csym
        if mcap_universe is not None and csym.upper() not in mcap_universe:
            row.update(Status="EXCLUDED", Detail="not in the >$50M market-cap list (V5 universe rule)")
            rows.append(row)
            continue
        arrays = build_arrays(cache, all_dates, csym)
        if arrays is None:
            row.update(Status="NOT IN PRICE CACHE", Detail="no price bars for this ticker")
            rows.append(row)
            continue
        closes, highs, lows, volumes = arrays
        if not np.isnan(closes[idx]):
            row["Price"] = _r(closes[idx])

        try:
            ok, chg, wk, vr = call_filters(mod, idx, closes, volumes)
        except (TypeError, ValueError, IndexError) as err:
            raise UserError(f"Your strategy's passes_v5_filters failed on {sym}: {type(err).__name__}: {err}") from None
        reason = None
        if not ok:  # the strategy's own filter decides; the explanation is only a description
            reason = v5_block_reason(mod, idx, closes, volumes) or GENERIC_BLOCK
            if csym not in cache["history"].get(asof, {}):
                # No real bar that day: the strategy sees a forward-filled flat price. Say so plainly.
                traded = [d for d in all_dates[:idx + 1] if csym in cache["history"].get(d, {})]
                reason = f"no trading data on {asof}" + (f" (last bar {traded[-1]})" if traded else "")
        if reason:
            row.update(Status="NO SIGNAL", Detail=reason)
            rows.append(row)
            continue
        row.update({"Chg_%": _r(chg), "Wk_%": _r(wk), "VR": _r(vr)})
        if not mod.detect_macd_cross_up(idx, closes):
            params = [setting(mod, k) for k in ("FAST_LEN", "SLOW_LEN", "SIGNAL_LEN")]
            label = f"MACD({','.join(str(p) for p in params)})" if None not in params else "MACD"
            row.update(Status="NO SIGNAL", Detail=f"passed price/volume rules but no {label} cross-up on this day")
            rows.append(row)
            continue
        tier, size = mod.get_tier(chg) if callable(getattr(mod, "get_tier", None)) else ("—", 0)
        row.update(Tier=tier, **{"Size_$": int(size)})
        if reg["allow"]:
            row.update(Status="BUY SIGNAL", Detail=f"V5 signal, regime OK ({tier})")
        else:
            row.update(Status="BLOCKED BY REGIME",
                       Detail=f"V5 signal, but market regime is {reg['reason']} so V5 would skip it")

        days_after = len(closes) - 1 - idx
        hold = int(setting(mod, "MAX_HOLD_DAYS", 10))
        row["DaysTracked"] = min(days_after, hold)
        if not callable(getattr(mod, "simulate_trade", None)):
            row["TradeState"] = "no trade tracking in this strategy"
        elif days_after <= 0:
            row["TradeState"] = "no later prices yet"
        else:
            sim = mod.simulate_trade(idx, closes, highs, lows)
            if sim is not None:
                if sim["why"] == "STOP":
                    state = f"closed: trailing stop on day {sim['day']}"
                elif days_after >= hold:
                    state = f"closed: {hold}-day time exit"
                else:
                    state = f"open: {days_after} of {hold} days so far"
                row.update(TradeState=state, **{"EffRet_%": _r(sim["eff_pct"]), "MaxGain_%": _r(sim["max_gain"]),
                                                "TP_Hit": "Y" if sim["tp_hit"] else "N", "ExitWhy": sim["why"],
                                                "PL_$": round(size * sim["eff_pct"] / 100, 0)})
        rows.append(row)
    return rows


def print_scan(rows: List[dict], asof: str, header: str, mod) -> None:
    r0 = rows[0]
    print("\n" + "=" * 96)
    print(f"  MACDgen V5 + Regime  ×  FinViz tickers  —  signals as of {asof}")
    print("=" * 96)
    print(f"  Tickers file : {header or '(no Run: line)'}")
    print(f"  Market regime on {asof}: {r0['Regime']}   (SPY {r0['SPY_Close']}  vs MA20 {r0['SPY_MA20']},"
          f"  ATR {r0['SPY_ATR_%']}%  limit {setting(mod, 'MAX_SPY_ATR_PCT', '—')}%)")
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    print(f"  {len(rows)} tickers: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))

    sig = [r for r in rows if r["Status"] in ("BUY SIGNAL", "BLOCKED BY REGIME")]
    if sig:
        print(f"\n  {'Ticker':<8}{'Status':<20}{'Price':>9}{'Chg%':>8}{'Wk%':>8}{'VR':>6}{'Tier':>8}{'Size':>8}"
              f"{'Ret%':>8}{'P/L$':>8}  Trade")
        print("  " + "-" * 110)
        for r in sorted(sig, key=lambda r: -(r["Chg_%"] or 0)):
            print(f"  {r['Ticker']:<8}{r['Status']:<20}{r['Price']:>9}{r['Chg_%']:>8}{r['Wk_%']:>8}{r['VR']:>6}"
                  f"{r['Tier']:>8}{r['Size_$']:>8}{str(r['EffRet_%']):>8}{str(r['PL_$']):>8}  {r['TradeState']}")
        tracked = [r for r in sig if r["Status"] == "BUY SIGNAL" and r["PL_$"] != ""]
        if tracked:
            pl = sum(r["PL_$"] for r in tracked)
            cap = sum(r["Size_$"] for r in tracked)
            print(f"\n  Tracked BUY SIGNAL trades: {len(tracked)}   capital ${cap:,.0f}   P/L so far ${pl:+,.0f}"
                  f"   ({pl / cap * 100:+.2f}%)" if cap else "")
    else:
        print("\n  No V5 signals among these tickers on this day.")

    reasons: Dict[str, int] = {}
    for r in rows:
        if r["Status"] == "NO SIGNAL":
            key = re.sub(r"[-+]?\d[\d,]*\.?\d*", "#", r["Detail"])
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("\n  Why the others did not signal (count):")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>4}  {k}")
    missing = [r["Ticker"] for r in rows if r["Status"] == "NOT IN PRICE CACHE"]
    if missing:
        print(f"\n  Not in your price cache ({len(missing)}): {', '.join(missing[:40])}"
              f"{' …' if len(missing) > 40 else ''}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Run MACDgen V5 + regime filter on FinViz agent tickers.")
    ap.add_argument("--tickers", default=str(DEFAULT_TICKERS),
                    help=f"tickers.txt from the FinViz agent, or a run folder (default: {DEFAULT_TICKERS})")
    ap.add_argument("--asof", help="trading day to evaluate, YYYY-MM-DD (default: from the file's Run: date)")
    ap.add_argument("--backtest", nargs=2, metavar=("START", "END"),
                    help="also run the original V5 window backtest on these tickers (hindsight-biased)")
    ap.add_argument("--strategy", default=str(DEFAULT_STRATEGY), help="path to macdgen_v5_regime_backtest.py")
    ap.add_argument("--cache", help="history_cache.json (default: the strategy file's CACHE_FILE)")
    ap.add_argument("--mcap", help="market-cap CSV (default: the strategy file's MCAP_FILE)")
    ap.add_argument("--no-mcap-filter", action="store_true", help="do not apply the >$50M market-cap list")
    ap.add_argument("--out", help="output folder (default: the strategy file's OUT_DIR)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    try:
        mod = load_strategy(Path(args.strategy).expanduser())
        if args.cache:
            mod.CACHE_FILE = str(Path(args.cache).expanduser())
        if args.mcap:
            mod.MCAP_FILE = str(Path(args.mcap).expanduser())
        out_dir = Path(args.out).expanduser() if args.out else Path(setting(mod, "OUT_DIR", "."))

        run_date, header, tickers, rejected = read_tickers(Path(args.tickers).expanduser())
        print(f"Tickers: {len(tickers)} from {args.tickers}" + (f"  (ignored {len(rejected)} invalid: "
                                                                f"{rejected[:5]})" if rejected else ""))
        if not getattr(mod, "CACHE_FILE", None):
            raise UserError("No price cache given: pass --cache PATH")
        if not Path(mod.CACHE_FILE).is_file():
            raise UserError(f"Price cache not found: {mod.CACHE_FILE}  (pass --cache PATH)")
        print(f"Loading price cache {mod.CACHE_FILE} ...", flush=True)
        import json
        with open(mod.CACHE_FILE) as f:
            cache = json.load(f)
        all_dates = sorted(cache["history"].keys())
        if not all_dates:
            raise UserError("The price cache has no dates.")

        target = date.fromisoformat(args.asof) if args.asof else (run_date or date.today())
        candidates = [d for d in all_dates if d <= target.isoformat()]
        if not candidates:
            raise UserError(f"The price cache has no dates on or before {target} (first: {all_dates[0]}).")
        asof = candidates[-1]
        expected = last_weekday_on_or_before(target)
        print(f"As-of trading day: {asof}" + (f"  (tickers file Run: {run_date})" if run_date else ""))
        if date.fromisoformat(asof) < expected:
            print(f"  ⚠️  Your price cache ends on {all_dates[-1]}, before {expected}. Update the cache first "
                  f"for {expected}'s signals (ignore this on a market holiday).")

        if args.no_mcap_filter:
            mcap_universe = None
        elif args.mcap:
            mcap_universe = load_mcap_csv(mod.MCAP_FILE)
        elif callable(getattr(mod, "load_mcap_universe", None)):
            mcap_universe = mod.load_mcap_universe()
        else:
            mcap_universe = None
        if mcap_universe is None and not args.no_mcap_filter:
            print("  ⚠️  Market-cap list not found; running without the >$50M universe rule.")

        rows = scan(mod, cache, all_dates, asof, tickers, mcap_universe)
        print_scan(rows, asof, header, mod)

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        out_csv = out_dir / f"macdgen_v5_finviz_signals_{asof}_{stamp}.csv"
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"\n  CSV saved: {out_csv}")

        if args.backtest:
            lacking = [f for f in BACKTEST_FUNCS if not callable(getattr(mod, f, None))]
            if lacking:
                raise UserError(f"--backtest needs {lacking} in the strategy file.")
            start, end = args.backtest
            for d in (start, end):
                date.fromisoformat(d)
            print("\n" + "!" * 96)
            print("  HINDSIGHT WARNING: these tickers were picked because they are strong NOW. Testing them on")
            print("  earlier dates overstates results. Use this only to compare settings, not to estimate returns.")
            print("!" * 96)
            cache_syms = set(cache["tickers"])
            data = {}
            for sym in tickers:
                csym = cache_symbol(sym, cache_syms)
                if csym is None or (mcap_universe is not None and csym.upper() not in mcap_universe):
                    continue
                arrays = build_arrays(cache, all_dates, csym)
                if arrays is not None:
                    data[csym] = arrays
            regime = mod.build_spy_regime(cache, all_dates)
            name = f"FinViz tickers {start} → {end}"
            trades, win_dates, skipped = mod.run_backtest(name, start, end, data, all_dates, regime)
            mod.print_window_summary(name, trades, win_dates, regime, skipped)
            bt_csv = out_dir / f"macdgen_v5_finviz_backtest_{start}_{end}_{stamp}.csv"
            fields = ["Window", "Date", "Ticker", "Price", "Chg_%", "Wk_%", "VR", "SPY_Close", "SPY_MA20",
                      "SPY_ATR_%", "Tier", "Size_$", "EffRet_%", "MaxGain_%", "TP_Hit", "ExitDay", "ExitWhy",
                      "Hit_3%", "PL_$"]
            with open(bt_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(trades)
            print(f"\n  Backtest CSV saved: {bt_csv}  ({len(data)} tickers with price data)")
    except UserError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    print(f"\n  Elapsed: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

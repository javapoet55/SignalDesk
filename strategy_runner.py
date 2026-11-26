#!/usr/bin/env python3
"""Runs one uploaded strategy in its own process (started by server.py with no secrets
in its environment, as an unprivileged user, with time and memory limits).

Two kinds of strategy file are supported:

1. V5-style (e.g. macdgen_v5_regime_backtest.py): defines passes_v5_filters,
   detect_macd_cross_up, simulate_trade, build_spy_regime, ... . It is run through
   macdgen_v5_finviz.py, which reuses its functions unchanged.

2. Generic: defines
       def finviz_signals(tickers: list[str], asof: str, cache: dict) -> list[dict]
   `cache` is in history_cache.json format; return one dict per row to show.

3. Whole script (e.g. v5_daily_dashboard.py): has a main() that reads a
   CACHE_FILE = '.../history_cache.json' setting, treats the last day in it as
   "today", and writes a CSV (into OUT_DIR if it has that setting). It is run
   unchanged: CACHE_FILE is pointed at the scan's tickers' prices cut off at the
   as-of day, OUT_DIR at the result folder, and the CSV it writes becomes the
   result table. If it also has v5_check / precompute_indicators / get_signals
   (the V5 dashboard layout), ticker_status.csv explains every scan ticker.

Commands
  strategy_runner.py check STRATEGY.py
  strategy_runner.py run STRATEGY.py --tickers T.txt --cache C.json --out DIR [--asof D] [--mcap M]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as _dtm
import types
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import macdgen_v5_finviz as v5  # noqa: E402


def _import(path: Path):
    spec = importlib.util.spec_from_file_location("uploaded_strategy", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["uploaded_strategy"] = mod
    spec.loader.exec_module(mod)
    return mod


CACHE_NAME_RE = re.compile(r"^[A-Z0-9_]*CACHE[A-Z0-9_]*$")
OUT_NAME_RE = re.compile(r"^(OUT|OUTPUT)(_?(DIR|FOLDER|PATH))?$")
PATHLIKE_NAME_RE = re.compile(r"^[A-Z0-9_]*(FILE|DIR|PATH|FOLDER|CSV)$")
DASHBOARD_FUNCS = ("v5_check", "precompute_indicators", "get_signals")


def script_settings(mod) -> Dict[str, List[str]]:
    """Module-level settings a whole-script strategy uses for its input cache and output folder."""
    cache, outdir, other = [], [], []
    for k, v in vars(mod).items():
        if not isinstance(v, str) or not k.isupper():
            continue
        if CACHE_NAME_RE.match(k) and v.lower().endswith(".json"):
            cache.append(k)
        elif OUT_NAME_RE.match(k):
            outdir.append(k)
        elif PATHLIKE_NAME_RE.match(k) and (v.startswith("/") or v.startswith("~")):
            other.append(k)
    return {"cache": cache, "out": outdir, "other": other}


def detect(mod) -> dict:
    if callable(getattr(mod, "finviz_signals", None)):
        return {"kind": "generic", "missing": []}
    missing = [n for n in v5.REQUIRED if not callable(getattr(mod, n, None))]
    if not missing:
        problem = v5.signature_problem(mod)
        if problem:
            return {"kind": None, "missing": [], "error": problem}
        optional = [f for f in ("build_spy_regime", "get_tier", "simulate_trade") if not callable(getattr(mod, f, None))]
        return {"kind": "v5", "missing": [], "without": optional}
    # A complete program: main() reads its price cache from a CACHE_FILE-style setting and
    # writes a CSV (into OUT_DIR if it has one). Run unchanged, with those two pointed here.
    if callable(getattr(mod, "main", None)):
        st = script_settings(mod)
        if st["cache"]:
            return {"kind": "script", "missing": [], "settings": st,
                    "dashboard": all(callable(getattr(mod, f, None)) for f in DASHBOARD_FUNCS)}
        return {"kind": None, "missing": missing,
                "script_problem": "it has a main() but no CACHE_FILE = '…history_cache.json' setting"}
    return {"kind": None, "missing": missing}


def cmd_check(path: Path) -> int:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as err:
        print(json.dumps({"kind": None, "error": f"Python syntax error on line {err.lineno}: {err.msg}"}))
        return 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mod = _import(path)
    except Exception as err:  # import-time failure
        print(json.dumps({"kind": None, "error": f"Could not load the file: {type(err).__name__}: {err}"}))
        return 0
    info = detect(mod)
    if info.get("kind") == "generic" and isinstance(getattr(mod, "CANDLE_DAYS", None), int):
        info["candles"] = True  # works on candles the agent builds (n-day or n-minute)
    tfs = getattr(mod, "TIMEFRAMES", None)
    if info.get("kind") == "generic" and tfs is not None:
        allowed = ("1m", "5m", "15m", "30m", "1d")
        ok = isinstance(tfs, (list, tuple)) and tfs and all(isinstance(t, str) and t in allowed for t in tfs)
        if not ok:
            print(json.dumps({"kind": None, "error": "TIMEFRAMES must be a list of bar sizes from "
                              + ", ".join(allowed) + f"; this file has {tfs!r}."}))
            return 0
        info["timeframes"] = sorted(set(tfs), key=allowed.index)  # extra bar sizes the agent supplies
    for attr, key, cap in (("TITLE", "title", 80), ("DESCRIPTION", "description", 200)):
        v = getattr(mod, attr, None)
        if isinstance(v, str) and v.strip():
            info[key] = " ".join(v.split())[:cap]  # shown on the result page (escaped there)
    print(json.dumps(info))
    return 0


def cmd_run(a) -> int:
    path = Path(a.strategy)
    mod = _import(path)
    info = detect(mod)
    out = Path(a.out)
    if info["kind"] == "v5":
        argv = ["--strategy", str(path), "--tickers", a.tickers, "--cache", a.cache, "--out", str(out)]
        if a.asof:
            argv += ["--asof", a.asof]
        argv += ["--mcap", a.mcap] if a.mcap else ["--no-mcap-filter"]
        code = v5.main(argv)
        produced = sorted(out.glob("macdgen_v5_finviz_signals_*.csv"))
        if produced:
            produced[-1].replace(out / "signals.csv")
        return code
    if info["kind"] == "generic":
        run_date, header, tickers, _ = v5.read_tickers(Path(a.tickers))
        cache = json.loads(Path(a.cache).read_text(encoding="utf-8"))
        days = sorted(cache["history"])
        asof = a.asof or ([d for d in days if d <= (run_date.isoformat() if run_date else days[-1])] or days)[-1]
        print(f"Generic strategy {path.name}: {len(tickers)} tickers, as of {asof}")
        if a.candle_label and isinstance(getattr(mod, "CANDLE_DAYS", None), int):
            # the agent already built the candles: each history entry is one candle
            mod.CANDLE_DAYS = 1
            mod.CANDLE_LABEL = a.candle_label
        rows = mod.finviz_signals(tickers, asof, cache)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            print("ERROR: finviz_signals must return a list of dicts", file=sys.stderr)
            return 2
        fields = []
        for r in rows:
            fields += [k for k in r if k not in fields]
        with (out / "signals.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields or ["Ticker"])
            w.writeheader()
            w.writerows(rows)
        print(f"{len(rows)} rows written")
        return 0
    if info["kind"] == "script":
        return run_script(mod, path, info, a, out)
    print(f"ERROR: {path.name} is neither a V5-style strategy nor defines finviz_signals(); "
          f"missing: {info['missing']}", file=sys.stderr)
    return 2


# ─────────────────────────────────────────────────────────────────────────────
#  Whole-script strategies (e.g. v5_daily_dashboard.py)
# ─────────────────────────────────────────────────────────────────────────────
STATUS_FIELDS = ["Ticker", "CacheTicker", "Status", "Detail", "Price", "Chg_%", "Wk_%", "VR"]
DASHBOARD_RULES = ("MIN_PRICE", "MIN_AVG_VOL_10D", "CHG_MIN", "MAX_CHG_PCT", "MAX_WEEK_PCT", "MAX_VOL_RATIO")


def pin_today(mod, asof: str) -> List[str]:
    """Make a whole-script strategy's "today" the as-of trading day.

    Scripts written for a Mac usually decide which day to scan from the clock (date.today(),
    datetime.now(), datetime.utcnow()). Here the day is the scan's as-of day and the prices are
    already in the cache, so those calls return the as-of DATE (with the real time of day, so
    run labels still show when it ran). Without this, a script asks for a day the cache doesn't
    have and tries to download it itself (e.g. from massive.com), which fails or uses the wrong day.
    Patches only this module's own names; returns the names patched."""
    d = _dtm.date.fromisoformat(asof)

    class AsOfDate(_dtm.date):
        @classmethod
        def today(cls):
            return cls(d.year, d.month, d.day)

    class AsOfDatetime(_dtm.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dtm.datetime.now(tz).replace(year=d.year, month=d.month, day=d.day)

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            return _dtm.datetime.now(_dtm.timezone.utc).replace(tzinfo=None, year=d.year, month=d.month, day=d.day)

    patched = []
    for name, value in list(vars(mod).items()):
        if value is _dtm.date:
            setattr(mod, name, AsOfDate)
        elif value is _dtm.datetime:
            setattr(mod, name, AsOfDatetime)
        elif value is _dtm:  # `import datetime` / `import datetime as dt`
            proxy = types.ModuleType("datetime")
            proxy.__dict__.update({k: v for k, v in vars(_dtm).items() if not k.startswith("__")})
            proxy.date, proxy.datetime = AsOfDate, AsOfDatetime
            setattr(mod, name, proxy)
        else:
            continue
        patched.append(name)
    return patched


def combine_csvs(files: List[Path], dest: Path) -> Optional[int]:
    """Several result CSVs that each have a Ticker column -> one table (all columns, plus Source =
    the file it came from). Returns the row count, or None if they can't be combined."""
    tables = []
    for f in files:
        with f.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            fields = list(reader.fieldnames or [])
            if "Ticker" not in fields:
                return None
            tables.append((f, fields, list(reader)))
    fields = ["Ticker"]
    for _, fl, _ in tables:
        fields += [x for x in fl if x not in fields]
    fields.append("Source")
    rows = [dict(r, Source=f.stem) for f, _, rs in tables for r in rs]
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def script_cache(cache: dict, asof: str, tickers: List[str]) -> Tuple[dict, List[str], List[str]]:
    """The cache the script sees: the scan's tickers only, and no day after the as-of day.

    The script treats the LAST day in its cache as "today", so cutting the history at the
    as-of day is what makes it evaluate that day. SPY's bars stay in the history (a script
    may look them up for a market filter) but SPY is only in "tickers" if the scan found it."""
    history = cache["history"]
    present = set(cache.get("tickers") or {s for bars in history.values() for s in bars})
    mapped: List[str] = []
    not_found: List[str] = []
    for t in tickers:
        cs = v5.cache_symbol(t, present)
        if cs is None:
            not_found.append(t)
        elif cs not in mapped:
            mapped.append(cs)
    days = [d for d in sorted(history) if d <= asof]
    return {"tickers": mapped, "history": {d: history[d] for d in days}}, mapped, not_found


def dashboard_block_reason(mod, idx, closes, volumes) -> Optional[str]:
    """Plain-English reason a ticker fails v5_check (same rules, the script's own settings).
    Only an explanation: whether a ticker passes is always decided by the script's v5_check."""
    if not all(isinstance(getattr(mod, k, None), (int, float)) for k in DASHBOARD_RULES):
        return None
    min_bars = v5.setting(mod, "MIN_BARS", 30)
    if idx < min_bars:
        return f"fewer than {min_bars} days of price history"
    c_today, c_prev = closes[idx], closes[idx - 1]
    if np.isnan(c_today) or np.isnan(c_prev):
        return "no price on the day or the day before"
    if c_today < mod.MIN_PRICE:
        return f"price ${c_today:,.2f} is under ${mod.MIN_PRICE:,.2f}"
    avg10 = float(np.mean(volumes[max(0, idx - 9):idx + 1]))
    if avg10 < mod.MIN_AVG_VOL_10D:
        return f"10-day avg volume {avg10:,.0f} is under {mod.MIN_AVG_VOL_10D:,}"
    chg = (c_today - c_prev) / c_prev * 100 if c_prev else 0
    if chg < mod.CHG_MIN:
        return f"day change {chg:+.2f}% is under +{mod.CHG_MIN:g}%"
    if chg > mod.MAX_CHG_PCT:
        return f"day change {chg:+.2f}% is over +{mod.MAX_CHG_PCT:g}% (too extended)"
    wk = (c_today - closes[idx - 5]) / closes[idx - 5] * 100 if idx >= 5 and closes[idx - 5] > 0 else 0.0
    if wk > mod.MAX_WEEK_PCT:
        return f"5-day change {wk:+.2f}% is over +{mod.MAX_WEEK_PCT:g}% (too extended)"
    avg20 = float(np.mean(volumes[max(0, idx - 19):idx + 1]))
    vr = volumes[idx] / avg20 if avg20 > 0 else 0
    if vr >= mod.MAX_VOL_RATIO:
        return f"volume is {vr:.1f}x its 20-day average (limit {mod.MAX_VOL_RATIO:g}x)"
    return None


def dashboard_status(mod, cache: dict, tickers: List[str], listed: Dict[str, dict]) -> List[dict]:
    """One row per scan ticker saying whether it is in the script's own output and, if not, why.
    Uses the script's own v5_check / precompute_indicators / get_signals on the same arrays its
    main() builds. The script's CSV stays the authority: a ticker is SIGNAL only if it is listed there."""
    days = sorted(cache["history"])
    idx = len(days) - 1
    present = set(cache["tickers"])
    rows = []
    for t in tickers:
        cs = v5.cache_symbol(t, present)
        row = {"Ticker": t, "CacheTicker": cs or ""}
        arrays = v5.build_arrays(cache, days, cs) if cs else None
        if arrays is None:
            row.update(Status="NOT IN PRICE CACHE", Detail="no prices for this ticker up to the as-of day")
            rows.append(row)
            continue
        closes, highs, lows, volumes = arrays
        c, p = closes[idx], closes[idx - 1] if idx >= 1 else np.nan
        if not (np.isnan(c) or np.isnan(p) or not p):
            row.update({"Price": round(float(c), 2), "Chg_%": round(float((c - p) / p * 100), 2)})
            if idx >= 5 and closes[idx - 5] > 0:
                row["Wk_%"] = round(float((c - closes[idx - 5]) / closes[idx - 5] * 100), 2)
        if cs in listed:
            r = listed[cs]
            row.update(Status="SIGNAL", Detail=f"{r.get('Confluence', '')} scanner(s): {r.get('Scanners', '')}"
                       + (f" · {r['Tier']} · ${r['Position_$']}" if r.get("Tier") and r.get("Position_$") else ""),
                       VR=r.get("VR", ""))
            rows.append(row)
            continue
        try:
            info = mod.v5_check(idx, closes, volumes)
        except Exception as err:  # noqa: BLE001 - report, keep going
            row.update(Status="ERROR", Detail=f"v5_check failed: {type(err).__name__}: {err}")
            rows.append(row)
            continue
        if info is None:
            row.update(Status="FILTERED OUT",
                       Detail=dashboard_block_reason(mod, idx, closes, volumes) or "did not pass the script's v5_check")
            rows.append(row)
            continue
        if isinstance(info, dict) and info.get("vr") is not None:
            row["VR"] = round(float(info["vr"]), 2)
        try:
            sigs = mod.get_signals(mod.precompute_indicators(closes, highs, lows), idx)
            hits = [k for k, v in sigs.items() if v]
        except Exception as err:  # noqa: BLE001 - the script itself skips these tickers
            row.update(Status="ERROR", Detail=f"indicators failed: {type(err).__name__}: {err}")
            rows.append(row)
            continue
        if hits:  # should not happen: the script would have listed it
            row.update(Status="CHECK", Detail="scanners fired (" + "|".join(hits) + ") but the script's CSV does not list it")
        else:
            row.update(Status="NO SCANNER FIRED", Detail="passed the V5 filters; none of the scanners fired today")
        rows.append(row)
    return rows


def run_script(mod, path: Path, info: dict, a, out: Path) -> int:
    run_date, header, tickers, _ = v5.read_tickers(Path(a.tickers))
    full = json.loads(Path(a.cache).read_text(encoding="utf-8"))
    days = sorted(full["history"])
    asof = a.asof or ([d for d in days if d <= (run_date.isoformat() if run_date else days[-1])] or days)[-1]
    cache, mapped, not_found = script_cache(full, asof, tickers)
    if not cache["history"]:
        print(f"ERROR: no price data on or before {asof}", file=sys.stderr)
        return 2
    cache_path = out / "script_cache.json"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    st = info["settings"]
    print(f"Script strategy {path.name}: running its main() unchanged on {len(mapped)} of {len(tickers)} "
          f"scan tickers, {len(cache['history'])} days of prices ({min(cache['history'])} → {asof}).")
    if not_found:
        print(f"  No prices for {len(not_found)}: {', '.join(not_found[:40])}" + (" …" if len(not_found) > 40 else ""))
    for k in st["cache"]:
        print(f"  {k} → script_cache.json in this result folder (was {getattr(mod, k)!r})")
        setattr(mod, k, str(cache_path))
    for k in st["out"]:
        print(f"  {k} → this result folder (was {getattr(mod, k)!r})")
        setattr(mod, k, str(out) + os.sep)
    for k in st["other"]:
        v = getattr(mod, k)
        if not os.path.exists(os.path.expanduser(v)):
            print(f"  Note: {k} = {v!r} does not exist on the server; anything in the script that needs it will fail.")
    pinned = pin_today(mod, asof)
    if pinned:
        print(f"  Dates: the script's 'today' is the as-of day {asof} ({', '.join(pinned)} return that date).")
    print("─" * 70, flush=True)
    before = {p.name: p.stat().st_mtime_ns for p in out.iterdir() if p.is_file()}
    saved_argv = sys.argv
    sys.argv = [path.name]
    code = 0
    try:
        ret = mod.main()
        code = ret if isinstance(ret, int) and not isinstance(ret, bool) else 0
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else (0 if ex.code is None else 1)
        if ex.code not in (None, 0) and not isinstance(ex.code, int):
            print(ex.code, file=sys.stderr)
    finally:
        sys.argv = saved_argv
    sys.stdout.flush()
    print("─" * 70)
    produced = sorted((p for p in out.glob("*.csv")
                       if p.name not in ("signals.csv", "ticker_status.csv")
                       and before.get(p.name) != p.stat().st_mtime_ns), key=lambda p: p.stat().st_mtime_ns)
    if (out / "signals.csv").is_file() and before.get("signals.csv") != (out / "signals.csv").stat().st_mtime_ns:
        produced.append(out / "signals.csv")  # the script itself chose that name
    if not produced:
        print("ERROR: the script finished but did not write a CSV file"
              + (" into OUT_DIR" if st["out"] else "") + ", so there is no result table.", file=sys.stderr)
        return code or 2
    chosen = produced[-1]
    combined = None
    if len(produced) > 1 and not info.get("dashboard") and all(p.name != "signals.csv" for p in produced):
        combined = combine_csvs(produced, out / "signals.csv")
    if combined is not None:
        print(f"Result table: {combined} rows from {len(produced)} CSV files combined into signals.csv "
              f"(column Source = the file): {', '.join(p.name for p in produced)}")
    else:
        if chosen.name != "signals.csv":
            shutil.copyfile(chosen, out / "signals.csv")
        print(f"Result table: {chosen.name}" + (" (also saved as signals.csv)" if chosen.name != "signals.csv" else "")
              + (f"; other CSV files written: {', '.join(p.name for p in produced[:-1])}" if len(produced) > 1 else ""))
    if info.get("dashboard"):
        with (out / "signals.csv").open(newline="", encoding="utf-8") as fh:
            listed = {(r.get("Ticker") or "").strip(): r for r in csv.DictReader(fh)}
        rows = dashboard_status(mod, cache, tickers, listed)
        with (out / "ticker_status.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=STATUS_FIELDS)
            w.writeheader()
            w.writerows(rows)
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["Status"]] = counts.get(r["Status"], 0) + 1
        print("All scan tickers: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + " (see ticker_status.csv)")
    return code


def replay_daily(a) -> int:
    """Evaluate a generic strategy on one fixed symbol, revealing daily history incrementally."""
    path = Path(a.strategy)
    cache = json.loads(Path(a.cache).read_text())
    if cache.get("timeframes"):
        raise ValueError("Historical replay currently requires a strategy using daily prices only.")
    history = cache["history"]
    sym = v5.cache_symbol(a.symbol, set(cache.get("tickers", [])) | {s for d in history.values() for s in d})
    dates = [d for d in sorted(history) if d <= a.asof and sym in history[d]]
    dates = set(dates[-a.sessions:])
    buys, past = [], {}
    for day in sorted(history):
        if day > a.asof:
            break
        past[day] = history[day]
        if day not in dates:
            continue
        mod = _import(path)
        if a.candle_label and isinstance(getattr(mod, "CANDLE_DAYS", None), int):
            mod.CANDLE_DAYS = 1
            mod.CANDLE_LABEL = a.candle_label
        rows = mod.finviz_signals([a.symbol], day, {"tickers": cache.get("tickers", []), "history": past})
        if not isinstance(rows, list):
            raise ValueError("The strategy did not return a signal list.")
        for row in rows:
            if str(row.get("Ticker","")).upper() == a.symbol and row.get("Status") in ("ERROR","NOT IN PRICE CACHE"):
                raise ValueError(f"Strategy could not evaluate {a.symbol} on {day}: {row.get('Error') or row['Status']}")
        has_status = any("Status" in row for row in rows)
        if any(str(row.get("Ticker","")).upper() == a.symbol and
               (not has_status or str(row.get("Status","")).strip() in {"BUY", "BUY SIGNAL", "SIGNAL"})
               for row in rows):
            buys.append(day)
    Path(a.output).write_text(json.dumps({"buys": buys, "days": sorted(dates)}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("strategy")
    r = sub.add_parser("run"); r.add_argument("strategy")
    r.add_argument("--tickers", required=True); r.add_argument("--cache", required=True)
    r.add_argument("--out", required=True); r.add_argument("--asof"); r.add_argument("--mcap")
    r.add_argument("--candle-label")
    replay = sub.add_parser("replay"); replay.add_argument("strategy")
    for key in ("symbol", "cache", "asof", "output"):
        replay.add_argument("--"+key, required=True)
    replay.add_argument("--sessions", type=int, choices=range(4, 253), required=True)
    replay.add_argument("--candle-label")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "replay":
            return replay_daily(a)
        return cmd_check(Path(a.strategy)) if a.cmd == "check" else cmd_run(a)
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())

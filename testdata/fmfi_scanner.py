#!/usr/bin/env python3
# This source code is subject to the Mozilla Public License 2.0.
# https://mozilla.org/MPL/2.0/
# Indicator attribution: © profitprotrading; Fourier settings credited to wbburgin.
"""Scan daily candles for original MFI crossing above smoothed Fourier MFI.

Standard library only (no pandas, no numpy), so it runs on the FinViz agent server.

Two ways to use it:

1. FinViz agent (Strategies tab): upload this file. The agent calls
       finviz_signals(tickers, asof, cache)
   with the scan's tickers and FinViz daily prices, and shows one row per ticker.
   Settings: FOURIER_PERIOD, LENGTH, SMOOTHING, LOOKBACK below.

2. On your own computer, from local CSVs (one TICKER.csv per ticker with
   Date, High, Low, Close, Volume):
       python3 fmfi_scanner.py --data-dir ./prices --tickers AAPL MSFT
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Shown on the FinViz agent's result page.
TITLE = "Fourier Money Flow"
DESCRIPTION = "Original MFI crosses above Fourier MF"

# ── Settings used by the FinViz agent (the command line has its own flags) ──
FOURIER_PERIOD = 3
LENGTH = 14
SMOOTHING = 7
LOOKBACK = 1   # report a cross if it happened in the last N candles up to the as-of day
CANDLE_DAYS = 1  # candle size in trading days for the command line; on the FinViz agent, pick Candles on the
                 # Strategies tab instead (days or minutes) - the agent builds the candles and sets this to 1.

NAN = float('nan')
FIELDS = ['Ticker', 'Status', 'As_Of', 'Close', 'Original_MFI', 'Fourier_MF', 'Buy_Latest',
          'Buy_In_Lookback', 'Signal_Dates', 'Candles', 'Note', 'Error']


def _nan(x):
    return x != x


def rolling_mean(values, n):
    out = [NAN] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        if not any(_nan(v) for v in window):
            out[i] = math.fsum(window) / n
    return out


def mfi(source, volume, length, change=None):
    """Money Flow Index over `length` candles (NaN until a full window of valid values).
    `change[i]` (optional) gives the direction of source[i] vs source[i-1]; default is their difference."""
    n = len(source)
    positive, negative = [NAN] * n, [NAN] * n
    for i in range(n):
        s = source[i]
        if _nan(s):
            continue
        if change is not None:
            direction = change[i]
        else:
            direction = s - source[i - 1] if i > 0 else NAN   # NaN compares False -> no flow
        flow = s * volume[i]
        positive[i] = flow if direction > 0 else 0.0
        negative[i] = flow if direction < 0 else 0.0
    out = [NAN] * n
    for i in range(length - 1, n):
        up_w, down_w = positive[i - length + 1:i + 1], negative[i - length + 1:i + 1]
        if any(_nan(v) for v in up_w) or any(_nan(v) for v in down_w):
            continue
        up, down = math.fsum(up_w), math.fsum(down_w)
        # Equivalent to 100 - 100 / (1 + up/down), including one-sided flows.
        out[i] = 100.0 * up / (up + down) if up + down != 0 else NAN
    return out


def pine_ema(series, length):
    """Seed with the first valid value; preserve EMA across missing values."""
    alpha = 2.0 / (length + 1)
    previous = NAN
    result = []
    for value in series:
        if not _nan(value):
            previous = value if _nan(previous) else alpha * value + (1 - alpha) * previous
        result.append(previous)
    return result


def calculate(highs, lows, closes, volumes, fourier_period=3, length=14, smoothing=7):
    """Return (original_mfi, fourier_mf, buy) lists aligned with the input candles (oldest first)."""
    source = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    # Pine only uses magnitude[0]: the normalized DFT's DC coefficient.
    # Its magnitude is abs(mean(source over N candles)); other bins are unused.
    window_mean = rolling_mean(source, fourier_period)
    ft = [abs(v) for v in window_mean]
    # Up/down for the Fourier line: the window mean moves by (newest - dropped) / N, so with
    # positive means its direction is exactly the sign of source[i] - source[i-N]. Deciding it
    # this way avoids rounding noise flipping "up"/"down" when the average is unchanged.
    ft_change = [NAN] * len(ft)
    for i in range(1, len(ft)):
        if _nan(ft[i]) or _nan(ft[i - 1]):
            continue
        ft_change[i] = (source[i] - source[i - fourier_period]
                        if window_mean[i] > 0 and window_mean[i - 1] > 0 else ft[i] - ft[i - 1])
    green = mfi(source, volumes, length)
    mf = pine_ema(mfi(ft, volumes, length, ft_change), smoothing)
    buy = [False] + [green[i] > mf[i] and green[i - 1] <= mf[i - 1] for i in range(1, len(source))]
    return green, mf, buy


def group_candles(days, highs, lows, closes, volumes, n):
    """Daily bars (oldest first) -> candles of n trading days each, the newest ending on the last day.
    Each candle: high = highest high, low = lowest low, close = last close, volume = total volume,
    dated by its last day. Oldest days that don't fill a whole candle are left out."""
    if n <= 1:
        return days, highs, lows, closes, volumes
    out = ([], [], [], [], [])
    for i in range(len(days) % n, len(days), n):
        j = i + n
        for lst, v in zip(out, (days[j - 1], max(highs[i:j]), min(lows[i:j]), closes[j - 1],
                                math.fsum(volumes[i:j]))):
            lst.append(v)
    return out


def evaluate(days, highs, lows, closes, volumes, fourier_period, length, smoothing, lookback, candle_days=1):
    """Signal row fields for one ticker's candles (oldest first). Raises ValueError on bad input."""
    if not days:
        raise ValueError('No data returned')
    if len(days) < fourier_period + length + smoothing + lookback:
        size = f'{candle_days}-day ' if isinstance(candle_days, int) and candle_days > 1 else ''
        raise ValueError(f'Insufficient history ({len(days)} {size}candles); need at least '
                         f'{fourier_period + length + smoothing + lookback}')
    green, mf, buy = calculate(highs, lows, closes, volumes, fourier_period, length, smoothing)
    if _nan(green[-1]) or _nan(mf[-1]):
        raise ValueError('MFI unavailable, possibly zero volume or no directional flow')
    signal_dates = [days[i] for i in range(len(days) - lookback, len(days)) if buy[i]]
    return {'As_Of': days[-1], 'Close': round(closes[-1], 4),
            'Original_MFI': round(green[-1], 4), 'Fourier_MF': round(mf[-1], 4),
            'Buy_Latest': bool(buy[-1]), 'Buy_In_Lookback': bool(signal_dates),
            'Signal_Dates': ';'.join(signal_dates)}


def _check_bar(high, low, close, volume):
    if not all(math.isfinite(x) for x in (high, low, close, volume)):
        raise ValueError('Missing or non-finite price/volume values')
    if volume < 0:
        raise ValueError('Volume cannot be negative')


# ─────────────────────────────────────────────────────────────────────────────
#  FinViz agent entry point
# ─────────────────────────────────────────────────────────────────────────────
def _cache_symbol(sym, present):
    """FinViz writes class shares as BRK-B; some feeds use BRK.B. Try both."""
    for cand in (sym, sym.replace('-', '.'), sym.replace('.', '-')):
        if cand in present:
            return cand
    return None


def finviz_signals(tickers, asof, cache):
    """Called by the FinViz agent. `cache` is {"tickers": [...], "history": {day: {sym: {o,h,l,c,v}}}}."""
    history = cache.get('history') or {}
    days = sorted(d for d in history if d <= asof)
    present = set(cache.get('tickers') or {s for bars in history.values() for s in bars})
    n = max(1, int(CANDLE_DAYS))
    label = globals().get('CANDLE_LABEL') or ('Daily' if n == 1 else f'{n}-day')
    print(f'Fourier MFI cross scan: {len(tickers)} tickers, as of {asof}, '
          f'{label} candles, '
          f'Fourier period {FOURIER_PERIOD}, MFI length {LENGTH}, smoothing {SMOOTHING}, '
          f'lookback {LOOKBACK} candle(s); {len(days)} {"days" if label == "Daily" else "candles"} of prices')
    rows = []
    for ticker in tickers:
        row = {k: '' for k in FIELDS}
        row.update(Ticker=ticker, Status='ERROR')
        sym = _cache_symbol(ticker, present)
        if sym is None:
            row.update(Status='NOT IN PRICE CACHE', Error='no FinViz prices for this ticker')
            rows.append(row)
            continue
        try:
            own_days, highs, lows, closes, volumes = [], [], [], [], []
            for d in days:  # this ticker's own candles only, like its own CSV
                bar = history[d].get(sym)
                if not bar:
                    continue
                h, l, c, v = (float(bar['h']), float(bar['l']), float(bar['c']), float(bar.get('v') or 0.0))
                _check_bar(h, l, c, v)
                own_days.append(d); highs.append(h); lows.append(l); closes.append(c); volumes.append(v)
            row.update(evaluate(*group_candles(own_days, highs, lows, closes, volumes, n),
                                FOURIER_PERIOD, LENGTH, SMOOTHING, LOOKBACK, n))
            row['Candles'] = label
            row['Status'] = 'BUY' if row['Buy_In_Lookback'] else 'NO SIGNAL'
            if row['As_Of'] != days[-1]:
                row['Note'] = f'last candle {row["As_Of"]}, before the as-of day {days[-1]}'
        except (ValueError, KeyError, TypeError) as exc:
            row['Error'] = str(exc)
        rows.append(row)
    counts = {}
    for r in rows:
        counts[r['Status']] = counts.get(r['Status'], 0) + 1
    order = ['BUY', 'NO SIGNAL', 'ERROR', 'NOT IN PRICE CACHE']
    print(f'Result for all {len(rows)} tickers: '
          + ', '.join(f'{k} {counts[k]}' for k in order if k in counts))
    for r in rows:
        if r['Status'] == 'BUY':
            print(f"  BUY {r['Ticker']:10} as of {r['As_Of']}  MFI={r['Original_MFI']}  "
                  f"MF={r['Fourier_MF']}  {r['Signal_Dates']}")
    for r in rows:
        if r['Status'] in ('ERROR', 'NOT IN PRICE CACHE'):
            print(f"  {r['Status']} {r['Ticker']:10} {r['Error']}")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  Command line: local CSV files
# ─────────────────────────────────────────────────────────────────────────────
DATE_FORMATS = ('%m/%d/%Y', '%m/%d/%y', '%Y/%m/%d', '%Y%m%d', '%d-%b-%Y', '%b %d, %Y')


def parse_date(raw):
    s = str(raw).strip()
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.split(' ')[0] if '/' in s else s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'Unrecognised date: {raw!r}')


def read_tickers(path):
    # One per line, or comma/whitespace separated. Optional Ticker/Symbol header.
    tokens = re.split(r'[,\s]+', Path(path).read_text().strip())
    return list(dict.fromkeys(t.upper() for t in tokens
                             if t and t.lower() not in {'ticker', 'symbol'}))


def load_prices(data_dir, ticker):
    """Read one local daily OHLCV CSV per ticker; never access the network.
    Returns (dates, highs, lows, closes, volumes), oldest first."""
    if Path(ticker).name != ticker or '/' in ticker or '\\' in ticker:
        raise ValueError('Ticker must not contain path separators')
    with open(Path(data_dir) / f'{ticker}.csv', newline='', encoding='utf-8-sig') as fh:
        reader = csv.reader(fh)
        header = [str(c).strip().lower() for c in next(reader, [])]
        required = ['date', 'high', 'low', 'close', 'volume']
        missing = set(required) - set(header)
        if missing:
            raise ValueError(f'Missing CSV columns: {", ".join(sorted(missing))}')
        col = {name: header.index(name) for name in required}
        bars = {}
        for line in reader:
            if not any(cell.strip() for cell in line):
                continue
            day = parse_date(line[col['date']])
            if day in bars:
                raise ValueError('Duplicate candle dates in CSV')
            try:
                h, l, c, v = (float(line[col[k]]) for k in ('high', 'low', 'close', 'volume'))
            except (ValueError, IndexError):
                raise ValueError('CSV contains missing or non-finite price/volume values') from None
            _check_bar(h, l, c, v)
            bars[day] = (h, l, c, v)
    days = sorted(bars)
    return (days, [bars[d][0] for d in days], [bars[d][1] for d in days],
            [bars[d][2] for d in days], [bars[d][3] for d in days])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tickers', nargs='+', help='Symbols matching your CSV filenames')
    parser.add_argument('--file', help='Ticker-only text/CSV file')
    parser.add_argument('--data-dir', required=True,
                        help='Folder containing TICKER.csv files with Date, High, Low, Close, Volume')
    parser.add_argument('--timezone', default='America/New_York',
                        help='Exchange timezone used to exclude today (default America/New_York)')
    parser.add_argument('--fourier-period', type=int, default=FOURIER_PERIOD)
    parser.add_argument('--length', type=int, default=LENGTH)
    parser.add_argument('--smoothing', type=int, default=SMOOTHING)
    parser.add_argument('--lookback', type=int, default=LOOKBACK,
                        help='Find crosses in last N available candles (default 1)')
    parser.add_argument('--candle-days', type=int, default=CANDLE_DAYS,
                        help='Trading days per candle: 1 = daily (default), 5 = 5-day candles')
    parser.add_argument('--include-today', action='store_true',
                        help='Include today; run after the exchange closes for confirmed signals')
    parser.add_argument('--output', default='fmfi_scan.csv')
    args = parser.parse_args()
    if min(args.fourier_period, args.length, args.smoothing, args.lookback, args.candle_days) < 1:
        parser.error('All numeric settings must be at least 1')
    tickers = list(dict.fromkeys((args.tickers or []) + (read_tickers(args.file) if args.file else [])))
    if not tickers:
        parser.error('Supply --tickers or --file')

    exchange_timezone = ZoneInfo(args.timezone)

    rows = []
    failures = 0
    for ticker in tickers:
        row = {'Ticker': ticker, 'Status': 'ERROR', 'As_Of': '', 'Close': '',
               'Original_MFI': '', 'Fourier_MF': '', 'Buy_Latest': '',
               'Buy_In_Lookback': '', 'Signal_Dates': '', 'Error': ''}
        try:
            days, highs, lows, closes, volumes = load_prices(args.data_dir, ticker)
            if not days:
                raise ValueError('No data returned')
            if not args.include_today:
                today = datetime.now(exchange_timezone).date()
                keep = [i for i, d in enumerate(days) if d < today]
                days, highs, lows, closes, volumes = ([seq[i] for i in keep]
                                                      for seq in (days, highs, lows, closes, volumes))
            days, highs, lows, closes, volumes = group_candles(days, highs, lows, closes, volumes,
                                                               args.candle_days)
            if len(days) < args.fourier_period + args.length + args.smoothing + args.lookback:
                raise ValueError('Insufficient history; add more candles to the CSV')
            result = evaluate([d.isoformat() for d in days], highs, lows, closes, volumes,
                              args.fourier_period, args.length, args.smoothing, args.lookback)
            row.update(result, Status='OK')
            print(f"{ticker:12} {'BUY' if row['Buy_In_Lookback'] else '-':4} as of {row['As_Of']} "
                  f"MFI={row['Original_MFI']} MF={row['Fourier_MF']} {row['Signal_Dates']}")
        except Exception as exc:
            failures += 1
            row['Error'] = str(exc)
            print(f'{ticker}: {exc}', file=sys.stderr)
        rows.append(row)
    with open(args.output, 'w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f'Saved {len(rows)} results to {args.output}; {failures} failures.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""
MACDgen V5 + Market Regime Filter — Backtest (April + Last Week)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Adds MARKET REGIME FILTER on top of the V5 framework:

  ❌ Skip signals when SPY < SPY's 20-day SMA  (bearish regime)
  ❌ Skip signals when SPY ATR(14)% > 1.5%      (high-fear proxy for VIX > 25)

Reason: Original V5 MACDgen lost money in last week's choppy/bearish week
        despite April's +84.5% accuracy. Regime filter removes these losing
        days entirely.

VIX EQUIVALENT:
  Pine's "VIX > 25" rule has no direct equivalent in our daily cache (Massive.com
  API doesn't serve VIX). Instead we use SPY's ATR(14)/Close % as a proxy:
    • Normal markets: ATR%/Close ≈ 0.5-1.0%   (VIX 10-20)
    • Stressed mkts : ATR%/Close ≈ 1.5-2.5%   (VIX 25-40)
    • Crisis        : ATR%/Close ≈ 3-5%+      (VIX 50+)

All other V5 logic (filters, tier sizing, trail stop) unchanged.

Output: /Users/sri/stock data/macdgen_v5_regime_backtest_<HHMMSS>.csv
"""
import json, csv, os, time
import numpy as np
from datetime import datetime

CACHE_FILE = '/Users/sri/stock-scanners/history_cache.json'
MCAP_FILE  = '/Users/sri/stock data/tickers_with_mcap abv 50m.csv'
OUT_DIR    = '/Users/sri/stock data/'

# ── MACD parameters ──────────────────────────────────────────────────────────
FAST_LEN, SLOW_LEN, SIGNAL_LEN = 5, 10, 9

# ── V5 filters ───────────────────────────────────────────────────────────────
MAX_CHG_PCT, MAX_WEEK_PCT, MAX_VOL_RATIO = 30.0, 45.0, 5.0
MIN_PRICE, MIN_AVG_VOL_10D = 1.00, 300_000
CHG_MIN = 3.0

# ── V5 strategy ──────────────────────────────────────────────────────────────
MAX_HOLD_DAYS, TAKE_PROFIT_PCT, PARTIAL_FRACTION, TRAIL_STOP_PCT = 10, 30.0, 0.50, 15.0
TIER_STRONG_SIZE, TIER_GOOD_SIZE, TIER_FAIR_SIZE = 7500.0, 5000.0, 3000.0

# ── Market regime filter (NEW) ───────────────────────────────────────────────
SPY_MA_LEN              = 20      # SPY must be above SPY's 20-day SMA
USE_VOLATILITY_FILTER   = True
MAX_SPY_ATR_PCT         = 1.5     # SPY ATR(14)/Close, %, equivalent of "VIX > 25"
SPY_TICKER              = 'SPY'

TARGET_GAIN_PCT = 3.0
MIN_BARS = 30


# ════════════════════════════════════════════════════════════════════════════
#  MATH HELPERS
# ════════════════════════════════════════════════════════════════════════════
def ema(series, period):
    s = np.asarray(series, dtype=float)
    out = np.full(len(s), np.nan)
    alpha = 2.0/(period+1); seed = None
    for i, v in enumerate(s):
        if np.isnan(v): continue
        seed = v if seed is None else alpha*v + (1-alpha)*seed
        out[i] = seed
    return out

def sma(series, period):
    s = np.asarray(series, dtype=float)
    out = np.full(len(s), np.nan)
    for i in range(period-1, len(s)):
        w = s[i-period+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.mean()
    return out

def rma(series, period):
    out = np.full(len(series), np.nan)
    alpha = 1.0/period; seed = None
    for i, v in enumerate(series):
        if np.isnan(v): continue
        seed = v if seed is None else alpha*v + (1-alpha)*seed
        out[i] = seed
    return out

def atr_wilder(highs, lows, closes, period=14):
    n = len(closes); tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i],
                    abs(highs[i]-closes[i-1]),
                    abs(lows[i] -closes[i-1]))
    return rma(tr, period)


# ════════════════════════════════════════════════════════════════════════════
#  V5 SIGNAL & STRATEGY LOGIC
# ════════════════════════════════════════════════════════════════════════════
def passes_v5_filters(idx, closes, volumes):
    n = len(closes)
    if idx < MIN_BARS or idx >= n: return False, None, None, None
    c_today = closes[idx]; c_prev = closes[idx-1]
    if np.isnan(c_today) or np.isnan(c_prev): return False, None, None, None
    if c_today < MIN_PRICE: return False, None, None, None
    avg_vol_10d = float(np.mean(volumes[max(0,idx-9):idx+1]))
    if avg_vol_10d < MIN_AVG_VOL_10D: return False, None, None, None
    chg_pct = (c_today-c_prev)/c_prev*100 if c_prev else 0
    if chg_pct < CHG_MIN or chg_pct > MAX_CHG_PCT: return False, None, None, None
    week_chg = (c_today-closes[idx-5])/closes[idx-5]*100 if idx >= 5 and closes[idx-5] > 0 else 0.0
    if week_chg > MAX_WEEK_PCT: return False, None, None, None
    avg_vol_20d = float(np.mean(volumes[max(0,idx-19):idx+1]))
    vol_ratio = volumes[idx]/avg_vol_20d if avg_vol_20d > 0 else 0
    if vol_ratio >= MAX_VOL_RATIO: return False, None, None, None
    return True, chg_pct, week_chg, vol_ratio


def get_tier(chg_pct):
    if chg_pct >= 10: return 'STRONG', TIER_STRONG_SIZE
    if chg_pct >= 5:  return 'GOOD',   TIER_GOOD_SIZE
    return 'FAIR', TIER_FAIR_SIZE


def detect_macd_cross_up(idx, closes):
    if idx < MIN_BARS or idx >= len(closes): return False
    c = np.asarray(closes[:idx+1], dtype=float)
    macd_arr = ema(c, FAST_LEN) - ema(c, SLOW_LEN)
    sig_arr  = sma(macd_arr, SIGNAL_LEN)
    j = len(c)-1
    if any(np.isnan(x) for x in [macd_arr[j], macd_arr[j-1], sig_arr[j], sig_arr[j-1]]):
        return False
    return (macd_arr[j] > sig_arr[j]) and (macd_arr[j-1] <= sig_arr[j-1])


def simulate_trade(idx, closes, highs, lows):
    n = len(closes); entry = closes[idx]
    if entry <= 0: return None
    tp_price = entry*(1+TAKE_PROFIT_PCT/100)
    partial_taken = False; partial_pct = 0.0; highest = entry; max_gain = 0.0
    for offset in range(1, MAX_HOLD_DAYS+1):
        bar = idx + offset
        if bar >= n: break
        if not np.isnan(highs[bar]):
            gain = (highs[bar]-entry)/entry*100
            if gain > max_gain: max_gain = gain
        if not np.isnan(closes[bar]) and closes[bar] > highest:
            highest = closes[bar]
        trail = highest*(1-TRAIL_STOP_PCT/100)
        if not partial_taken and not np.isnan(highs[bar]) and highs[bar] >= tp_price:
            partial_pct = TAKE_PROFIT_PCT * PARTIAL_FRACTION
            partial_taken = True
        if not np.isnan(lows[bar]) and lows[bar] <= trail:
            rem = ((trail-entry)/entry*100)*(1-PARTIAL_FRACTION) if partial_taken else ((trail-entry)/entry*100)
            return {'eff_pct': partial_pct+rem, 'why': 'STOP', 'day': offset,
                    'tp_hit': partial_taken, 'high_pct': (highest-entry)/entry*100,
                    'max_gain': max_gain}
    last_bar = min(idx+MAX_HOLD_DAYS, n-1)
    if np.isnan(closes[last_bar]):
        return {'eff_pct': 0.0, 'why': 'NO_DATA', 'day': MAX_HOLD_DAYS,
                'tp_hit': partial_taken, 'high_pct': (highest-entry)/entry*100,
                'max_gain': max_gain}
    close_pct = (closes[last_bar]-entry)/entry*100
    rem = close_pct*(1-PARTIAL_FRACTION) if partial_taken else close_pct
    return {'eff_pct': partial_pct+rem, 'why': 'TIME', 'day': MAX_HOLD_DAYS,
            'tp_hit': partial_taken, 'high_pct': (highest-entry)/entry*100,
            'max_gain': max_gain}


def load_mcap_universe():
    if not os.path.exists(MCAP_FILE): return None
    universe = set()
    with open(MCAP_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = row.get('Ticker', '').strip().upper()
            if t: universe.add(t)
    return universe


# ════════════════════════════════════════════════════════════════════════════
#  BUILD SPY REGIME ARRAYS
# ════════════════════════════════════════════════════════════════════════════
def build_spy_regime(cache, all_dates):
    """
    Returns dict: date → {'allow': True/False, 'spy_close', 'spy_ma', 'atr_pct'}
    The regime is BULLISH (allow=True) when:
      • SPY > SPY 20-day SMA
      • SPY ATR(14)/Close < MAX_SPY_ATR_PCT
    """
    spy_c = []; spy_h = []; spy_l = []
    last_c = last_h = last_l = np.nan
    for dt in all_dates:
        bar = cache['history'].get(dt, {}).get(SPY_TICKER)
        if bar:
            last_c = float(bar['c']); last_h = float(bar['h']); last_l = float(bar['l'])
        spy_c.append(last_c); spy_h.append(last_h); spy_l.append(last_l)

    spy_ma = sma(spy_c, SPY_MA_LEN)
    spy_atr = atr_wilder(np.asarray(spy_h, dtype=float), np.asarray(spy_l, dtype=float),
                          np.asarray(spy_c, dtype=float), 14)

    regime = {}
    for i, dt in enumerate(all_dates):
        c, m, a = spy_c[i], spy_ma[i], spy_atr[i]
        if np.isnan(c) or np.isnan(m) or np.isnan(a):
            allow = False
            reason = 'NO_DATA'
            atr_pct = np.nan
        else:
            atr_pct = a / c * 100
            bull = c > m
            calm = (not USE_VOLATILITY_FILTER) or (atr_pct < MAX_SPY_ATR_PCT)
            allow = bull and calm
            if not bull and not calm:
                reason = 'BEAR+VOL'
            elif not bull:
                reason = 'BEAR'
            elif not calm:
                reason = 'HIGH_VOL'
            else:
                reason = 'OK'
        regime[dt] = {
            'allow': allow, 'reason': reason,
            'spy_close': c, 'spy_ma': m, 'atr_pct': atr_pct,
        }
    return regime


# ════════════════════════════════════════════════════════════════════════════
#  BACKTEST A WINDOW
# ════════════════════════════════════════════════════════════════════════════
def run_backtest(window_name, start_date, end_date, ticker_data, all_dates, regime):
    date_idx = {d: i for i, d in enumerate(all_dates)}
    win_dates = [d for d in all_dates if start_date <= d <= end_date]
    trades = []
    regime_skipped = {'BEAR': 0, 'HIGH_VOL': 0, 'BEAR+VOL': 0, 'NO_DATA': 0}

    for sym, (closes, highs, lows, volumes) in ticker_data.items():
        for trig_date in win_dates:
            idx = date_idx[trig_date]
            if idx >= len(closes) or np.isnan(closes[idx]): continue

            # ── NEW: MARKET REGIME FILTER ──
            reg = regime.get(trig_date, {'allow': False, 'reason': 'NO_DATA'})
            if not reg['allow']:
                regime_skipped[reg['reason']] = regime_skipped.get(reg['reason'], 0) + 1
                continue

            ok, chg, wk, vr = passes_v5_filters(idx, closes, volumes)
            if not ok: continue

            if not detect_macd_cross_up(idx, closes): continue

            tier, size = get_tier(chg)
            sim = simulate_trade(idx, closes, highs, lows)
            if sim is None: continue
            pl = size * sim['eff_pct'] / 100
            hit_3pct = sim['max_gain'] >= TARGET_GAIN_PCT

            trades.append({
                'Window': window_name, 'Date': trig_date, 'Ticker': sym,
                'Price': round(closes[idx], 2),
                'Chg_%': round(chg, 2), 'Wk_%': round(wk, 2), 'VR': round(vr, 2),
                'SPY_Close': round(reg['spy_close'], 2),
                'SPY_MA20': round(reg['spy_ma'], 2),
                'SPY_ATR_%': round(reg['atr_pct'], 2),
                'Tier': tier, 'Size_$': int(size),
                'EffRet_%': round(sim['eff_pct'], 2),
                'MaxGain_%': round(sim['max_gain'], 2),
                'TP_Hit': 'Y' if sim['tp_hit'] else 'N',
                'ExitDay': sim['day'], 'ExitWhy': sim['why'],
                'Hit_3%': 'YES' if hit_3pct else 'no',
                'PL_$': round(pl, 0),
            })
    return trades, win_dates, regime_skipped


def print_window_summary(window_name, trades, win_dates, regime, regime_skipped):
    print('\n' + '='*100)
    print(f'  V5 MACDgen + REGIME FILTER — {window_name}')
    print('='*100)

    # Show regime for each day
    print(f'\n  ── MARKET REGIME PER DAY ──')
    print(f'  {"Date":<12}{"SPY":>9}{"SPY MA20":>10}{"ATR%":>8}{"Regime":>10}{"Trades":>8}')
    print('  ' + '-'*55)
    for d in win_dates:
        r = regime.get(d, {})
        spy_c = r.get('spy_close', np.nan)
        spy_m = r.get('spy_ma',   np.nan)
        atr   = r.get('atr_pct',  np.nan)
        reason = r.get('reason', 'NO_DATA')
        sigs = sum(1 for t in trades if t['Date'] == d)
        spy_s = f'{spy_c:>9.2f}' if not np.isnan(spy_c) else f'{"na":>9}'
        ma_s  = f'{spy_m:>10.2f}' if not np.isnan(spy_m) else f'{"na":>10}'
        atr_s = f'{atr:>7.2f}%'   if not np.isnan(atr)   else f'{"na":>8}'
        print(f'  {d:<12}{spy_s}{ma_s}{atr_s}{reason:>10}{sigs:>8}')

    # Skip stats
    total_skipped = sum(regime_skipped.values())
    if total_skipped > 0:
        print(f'\n  Regime skips: {dict(regime_skipped)}')

    if not trades:
        print('  ❌ No trades — market regime was bearish or volatile all week.')
        return

    n = len(trades)
    capital = sum(t['Size_$'] for t in trades)
    total_pl = sum(t['PL_$'] for t in trades)
    wins = [t for t in trades if t['PL_$'] > 0]
    losses = [t for t in trades if t['PL_$'] < 0]
    win_rate_pl = len(wins)/n*100
    n_hit_3 = sum(1 for t in trades if t['Hit_3%'] == 'YES')
    accuracy_3 = n_hit_3/n*100
    pf = (sum(t['PL_$'] for t in wins) / abs(sum(t['PL_$'] for t in losses))) if losses else float('inf')

    print(f'\n  ── OVERALL ──')
    print(f'  Total signals     : {n}')
    print(f'  Wins / Losses     : {len(wins)} / {len(losses)}')
    print(f'  P/L Win Rate      : {win_rate_pl:.1f}%')
    print(f'  🎯 ACCURACY (>=3% max gain) : {accuracy_3:.1f}%  ({n_hit_3} of {n})')
    print(f'  Capital deployed  : ${capital:,.0f}')
    print(f'  Net P/L           : ${total_pl:+,.0f}')
    if capital:
        print(f'  ROI               : {total_pl/capital*100:+.2f}%')
    if wins:
        print(f'  Avg winner        : ${sum(t["PL_$"] for t in wins)/len(wins):+,.0f}')
    if losses:
        print(f'  Avg loser         : ${sum(t["PL_$"] for t in losses)/len(losses):+,.0f}')
        print(f'  Profit factor     : {pf:.2f}x')
    print(f'  Best win          : ${max(t["PL_$"] for t in trades):+,.0f}')
    print(f'  Worst loss        : ${min(t["PL_$"] for t in trades):+,.0f}')


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    print('Loading cache ...', flush=True)
    with open(CACHE_FILE) as f:
        cache = json.load(f)
    all_dates = sorted(cache['history'].keys())

    print('Building SPY regime...', flush=True)
    regime = build_spy_regime(cache, all_dates)
    n_bull = sum(1 for r in regime.values() if r['allow'])
    n_bear = sum(1 for r in regime.values() if r['reason'] in ('BEAR','BEAR+VOL'))
    n_vol  = sum(1 for r in regime.values() if r['reason'] == 'HIGH_VOL')
    print(f'  Regime stats across {len(regime)} days: OK={n_bull}, BEAR={n_bear}, HIGH_VOL={n_vol}')
    print(f'  Filters: SPY > SPY SMA({SPY_MA_LEN})  AND  SPY ATR%(14) < {MAX_SPY_ATR_PCT}%\n')

    print('Loading mcap filter ...', flush=True)
    mcap_universe = load_mcap_universe()
    if mcap_universe is None:
        print('  ⚠️ No mcap CSV — proceeding without mcap filter')
    else:
        print(f'  ✅ {len(mcap_universe):,} tickers > $50M\n')

    print('Building ticker arrays (forward-fill) ...', flush=True)
    ticker_data = {}
    total = len(cache['tickers'])
    for idx_t, sym in enumerate(cache['tickers']):
        if idx_t % 500 == 0: print(f'  {idx_t}/{total}', flush=True)
        if mcap_universe is not None and sym.upper() not in mcap_universe:
            continue
        closes = []; highs = []; lows = []; volumes = []
        last_c = last_h = last_l = np.nan
        for dt in all_dates:
            bar = cache['history'].get(dt, {}).get(sym)
            if bar:
                last_c = float(bar['c']); last_h = float(bar['h']); last_l = float(bar['l'])
                closes.append(last_c); highs.append(last_h); lows.append(last_l)
                volumes.append(float(bar.get('v', 0)))
            else:
                closes.append(last_c); highs.append(last_h); lows.append(last_l)
                volumes.append(0.0)
        if all(np.isnan(x) for x in closes): continue
        ticker_data[sym] = (closes, highs, lows, volumes)
    print(f'  Universe ready: {len(ticker_data):,} tickers\n')

    print('Running backtest 1: APRIL 2026 ...', flush=True)
    apr_trades, apr_dates, apr_skip = run_backtest('APRIL 2026 (4/1 - 4/30)',
        '2026-04-01', '2026-04-30', ticker_data, all_dates, regime)

    print('Running backtest 2: LAST WEEK ...', flush=True)
    week_trades, week_dates, week_skip = run_backtest('LAST WEEK (5/11 - 5/15)',
        '2026-05-11', '2026-05-15', ticker_data, all_dates, regime)

    # Save CSV
    ts = datetime.now().strftime('%H%M%S')
    outfile = os.path.join(OUT_DIR, f'macdgen_v5_regime_backtest_{ts}.csv')
    fields = ['Window','Date','Ticker','Price','Chg_%','Wk_%','VR',
              'SPY_Close','SPY_MA20','SPY_ATR_%',
              'Tier','Size_$','EffRet_%','MaxGain_%','TP_Hit','ExitDay','ExitWhy',
              'Hit_3%','PL_$']
    with open(outfile, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(apr_trades + week_trades)

    # Print summaries
    print_window_summary('APRIL 2026 (4/1 → 4/30)', apr_trades, apr_dates, regime, apr_skip)
    print_window_summary('LAST WEEK (5/11 → 5/15)', week_trades, week_dates, regime, week_skip)

    # ── Final comparison ──
    print('\n' + '='*100)
    print(f'  COMPARISON  —  No-Regime V5  vs  V5 + Regime Filter')
    print('='*100)

    apr_n = len(apr_trades); apr_hits = sum(1 for t in apr_trades if t['Hit_3%']=='YES')
    apr_pl = sum(t['PL_$'] for t in apr_trades); apr_cap = sum(t['Size_$'] for t in apr_trades)
    apr_acc = (apr_hits/apr_n*100) if apr_n else 0
    apr_roi = (apr_pl/apr_cap*100) if apr_cap else 0

    week_n = len(week_trades); week_hits = sum(1 for t in week_trades if t['Hit_3%']=='YES')
    week_pl = sum(t['PL_$'] for t in week_trades); week_cap = sum(t['Size_$'] for t in week_trades)
    week_acc = (week_hits/week_n*100) if week_n else 0
    week_roi = (week_pl/week_cap*100) if week_cap else 0

    print(f'\n                              APRIL 2026             LAST WEEK')
    print(f'  No-Regime V5:')
    print(f'    Signals:                     1113                   177')
    print(f'    Accuracy (3%+):              84.5%                 33.9%')
    print(f'    Net P/L:                  +$206,964              -$24,641')
    print(f'    ROI:                         +4.20%                -2.87%')
    print(f'\n  V5 + REGIME FILTER:')
    print(f'    Signals:                       {apr_n:>4}                  {week_n:>4}')
    print(f'    Accuracy (3%+):             {apr_acc:>5.1f}%               {week_acc:>5.1f}%')
    print(f'    Net P/L:                  ${apr_pl:>+9,.0f}            ${week_pl:>+9,.0f}')
    print(f'    ROI:                         {apr_roi:>+5.2f}%               {week_roi:>+5.2f}%')

    print(f'\n  Elapsed: {time.time()-t0:.1f}s')
    print(f'  CSV saved: {outfile}')


if __name__ == '__main__':
    main()

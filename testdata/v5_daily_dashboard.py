#!/usr/bin/env python3
"""
V5 DAILY DASHBOARD — All 7 V5 Scanners + Confluence Bonus
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Runs ALL V5-validated scanners against today's cache:
  1.  Stoch %K Cross-45
  2.  DIDI BOT
  3.  PTT Buy
  4.  KDJ K×D Cross
  5.  MACD Yellow Ghist "B"
  6.  Z-Score Bullish Reversal
  7.  Z-Score Buy-at-Zero

For each ticker, aggregates ALL scanner hits → assigns:
  • Tier   (STRONG / GOOD / FAIR based on daily change)
  • Confluence Score (# of V5 scanners that fired on it today)
  • Position size multiplier:
      1 scanner   → 1.0× tier size
      2 scanners  → 1.5× tier size  (CONFLUENCE BONUS)
      3+ scanners → 2.0× tier size  (HIGH CONFLUENCE)

Daily trade plan sorted by Confluence × Tier.

Output:
  /Users/sri/stock data/v5_daily_dashboard_<date>_<HHMMSS>.csv
"""
import json, os, csv, time
import numpy as np
from datetime import datetime

CACHE_FILE = '/Users/sri/stock-scanners/history_cache.json'
OUT_DIR    = '/Users/sri/stock data/'

# ── V5 FRAMEWORK ─────────────────────────────────────────────────────────
MAX_CHG_PCT      = 30.0
MAX_WEEK_PCT     = 45.0
MAX_VOL_RATIO    = 5.0
MIN_PRICE        = 1.00
MIN_AVG_VOL_10D  = 300_000
CHG_MIN          = 3.0
TIER_STRONG_SIZE = 7500.0
TIER_GOOD_SIZE   = 5000.0
TIER_FAIR_SIZE   = 3000.0
# Confluence multipliers
CONFLUENCE_2X    = 1.5
CONFLUENCE_3X    = 2.0


# ══════════════════════════════════════════════════════════════════════════════
#  MATH HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def sma(arr, n):
    out = np.full(len(arr), np.nan)
    for i in range(n-1, len(arr)):
        w = arr[i-n+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.mean()
    return out

def ema(arr, span):
    out = np.full(len(arr), np.nan)
    alpha = 2/(span+1)
    seed = None
    for i, v in enumerate(arr):
        if np.isnan(v): continue
        seed = v if seed is None else alpha*v + (1-alpha)*seed
        out[i] = seed
    return out

def rolling_max(arr, n):
    out = np.full(len(arr), np.nan)
    for i in range(n-1, len(arr)):
        w = arr[i-n+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.max()
    return out

def rolling_min(arr, n):
    out = np.full(len(arr), np.nan)
    for i in range(n-1, len(arr)):
        w = arr[i-n+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.min()
    return out

def rolling_std(arr, n):
    out = np.full(len(arr), np.nan)
    for i in range(n-1, len(arr)):
        w = arr[i-n+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.std()
    return out

def stdev_pop(arr, n):
    out = np.full(len(arr), np.nan)
    for i in range(n-1, len(arr)):
        w = arr[i-n+1:i+1]
        if np.any(np.isnan(w)): continue
        out[i] = w.std(ddof=0)
    return out

def stoch_pine(src, high, low, length):
    lo = rolling_min(low, length); hi = rolling_max(high, length)
    rng = hi - lo
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(rng > 0, 100*(src-lo)/rng, np.nan)

def wma(series, length):
    s = np.asarray(series, dtype=float)
    out = np.full(len(s), np.nan)
    w = np.arange(1, length+1, dtype=float); wsum = w.sum()
    for i in range(length-1, len(s)):
        win = s[i-length+1:i+1]
        if np.any(np.isnan(win)): continue
        out[i] = (win*w).sum()/wsum
    return out

def hma(series, length):
    half = max(int(length//2), 1)
    sqrt_len = max(int(np.floor(np.sqrt(length))), 1)
    raw = 2*wma(series, half) - wma(series, length)
    return wma(raw, sqrt_len)

def alma(series, length, offset=0.0, sigma=0.1):
    s = np.asarray(series, dtype=float)
    m = offset*(length-1); ss = length/sigma
    iarr = np.arange(length, dtype=float)
    weights = np.exp(-((iarr - m)**2)/(2*ss*ss))
    wsum = weights.sum()
    out = np.full(len(s), np.nan)
    for t in range(length-1, len(s)):
        win = s[t-length+1:t+1]
        if np.any(np.isnan(win)): continue
        out[t] = (win*weights).sum()/wsum
    return out

def xover_at(a, b, idx):
    if idx < 1: return False
    if np.isnan(a[idx]) or np.isnan(b[idx]) or np.isnan(a[idx-1]) or np.isnan(b[idx-1]):
        return False
    return a[idx] > b[idx] and a[idx-1] <= b[idx-1]


# ── DIDI helpers ─────────────────────────────────────────────────────────
CURTA_LEN, MEDIA_LEN, LONGA_LEN = 3, 8, 20
SIGNAL_LEN, SF, QQE_FACTOR = 14, 5, 4.236

def qqe1(src, length=14, sf=5, factor=4.236):
    s = ema(src, sf)
    delta = np.diff(s, prepend=np.nan)
    atr_smooth = ema(np.abs(delta), length)
    atr_smooth_f = atr_smooth * factor
    n = len(s); out = np.full(n, np.nan)
    for i in range(1, n):
        if np.isnan(s[i]) or np.isnan(atr_smooth_f[i]): continue
        prev = out[i-1] if not np.isnan(out[i-1]) else s[i]
        up = s[i] + atr_smooth_f[i]; dn = s[i] - atr_smooth_f[i]
        if s[i] > prev: out[i] = max(dn, prev)
        elif s[i] < prev: out[i] = min(up, prev)
        else: out[i] = prev
    return out


def xsa(src, length, wei):
    s = np.asarray(src, dtype=float); n = len(s)
    out = np.full(n, np.nan); sumf = 0.0
    have_prev = False; prev_out = 0.0
    for i in range(n):
        cur = 0.0 if np.isnan(s[i]) else s[i]
        if i >= length:
            lag = 0.0 if np.isnan(s[i-length]) else s[i-length]
            sumf = sumf - lag + cur
        else: sumf = sumf + cur
        if i < length or np.isnan(s[i-length]): continue
        ma = sumf / length
        if not have_prev:
            out[i] = ma; prev_out = ma; have_prev = True
        else:
            new_out = (cur*wei + prev_out*(length-wei)) / length
            out[i] = new_out; prev_out = new_out
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL DETECTORS — full series (returns dict {scanner_name: True/False} at idx)
# ══════════════════════════════════════════════════════════════════════════════
def precompute_indicators(closes, highs, lows):
    """
    Precompute all indicators once per ticker for the FULL series.
    Returns dict of arrays.
    """
    c = np.asarray(closes, dtype=float)
    h = np.asarray(highs,  dtype=float)
    l = np.asarray(lows,   dtype=float)
    n = len(c)
    out = {}

    # ── Stoch %K (14/5/3) for STOCH_K45 ──
    low14 = rolling_min(l, 14); high14 = rolling_max(h, 14)
    denom = high14 - low14
    with np.errstate(invalid='ignore', divide='ignore'):
        raw_k = np.where(denom > 0, (c-low14)/denom, np.nan)
    out['stoch_k'] = sma(raw_k*100, 5)

    # ── DIDI (3/8/20 + QQE) ──
    media = sma(c, MEDIA_LEN)
    out['didi_curta'] = sma(c, CURTA_LEN) - media
    out['didi_longa'] = sma(c, LONGA_LEN) - media
    out['didi_qqe']   = qqe1(out['didi_curta'], SIGNAL_LEN, SF, QQE_FACTOR)

    # ── PTT ──
    VAR = ema(c, 2)
    E1  = rolling_min(h, 5); E2 = rolling_max(E1, 5)
    E2c = np.where(np.isnan(E2), 0, E2)
    BBb = ema(E2c, 2) - 2.0 * rolling_std(E2, 2)
    PTTh = rolling_max(BBb, 10)
    out['ptt_VAR'] = VAR; out['ptt_PTTh'] = PTTh

    # ── KDJ (period 14) ──
    ed = np.full(n, np.nan)
    for i in range(4, n):
        ed[i] = -0.4*c[i-4] - 0.4*c[i-3] - 1.1*c[i-2] + 0.9*c[i-1] + 2.0*c[i]
    ed_smooth = xsa(ed, 4, 1)
    lo_k = np.full(n, np.nan); hi_k = np.full(n, np.nan)
    for i in range(13, n):
        lo_k[i] = l[i-13:i+1].min(); hi_k[i] = h[i-13:i+1].max()
    rsv = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(ed_smooth[i]) or np.isnan(lo_k[i]) or np.isnan(hi_k[i]): continue
        rng = hi_k[i] - lo_k[i]
        if rng > 0: rsv[i] = (ed_smooth[i] - lo_k[i])/rng * 100
    out['kdj_K'] = xsa(rsv, 3, 1); out['kdj_D'] = xsa(out['kdj_K'], 3, 1)

    # ── MACD (8/17/9) ──
    diff = ema(c, 8) - ema(c, 17)
    dea  = ema(diff, 9)
    out['macd_diff'] = diff; out['macd_dea'] = dea
    out['macd_ghist'] = (diff - dea) * 2.0

    # ── Z-Score chain (len=21) ──
    src = (h + l + c) / 3.0
    basis = sma(src, 21)
    sd = stdev_pop(src, 21)
    with np.errstate(invalid='ignore', divide='ignore'):
        z = np.where(sd > 0, (src - basis) / sd, np.nan)
    stz = stoch_pine(z, z, z, 21)
    scaled = stz/25.0 - 2.0
    out['zscore_smooth'] = hma(scaled, 21)
    out['zscore_ltm']    = alma(z, 21, 0, 0.1)

    return out


def get_signals(ind, idx):
    """Returns dict of {scanner_name: True/False} at index idx."""
    sig = {}
    # STOCH_K45
    sk = ind['stoch_k']
    sig['STOCH_K45'] = (
        idx >= 1 and not np.isnan(sk[idx]) and not np.isnan(sk[idx-1])
        and sk[idx] > 45 and sk[idx-1] <= 45 and sk[idx] >= 50
    )
    # DIDI_BOT
    curta, longa, qqe = ind['didi_curta'], ind['didi_longa'], ind['didi_qqe']
    sig['DIDI_BOT'] = (
        idx >= 22 and xover_at(curta, qqe, idx) and not np.isnan(longa[idx])
        and curta[idx] > longa[idx]
    )
    # PTT_BUY
    sig['PTT_BUY'] = xover_at(ind['ptt_VAR'], ind['ptt_PTTh'], idx) if idx >= 25 else False
    # KDJ_CROSS
    sig['KDJ_CROSS'] = xover_at(ind['kdj_K'], ind['kdj_D'], idx) if idx >= 25 else False
    # MACD_B  (Yellow ghist + cross above DEA)
    gh = ind['macd_ghist']; de = ind['macd_dea']
    sig['MACD_B'] = False
    if idx >= 1:
        if not (np.isnan(gh[idx]) or np.isnan(gh[idx-1]) or np.isnan(de[idx]) or np.isnan(de[idx-1])):
            is_yellow = (gh[idx] <= 0) and (gh[idx] >= gh[idx-1])
            cross_dea = (gh[idx] > de[idx]) and (gh[idx-1] <= de[idx-1])
            sig['MACD_B'] = is_yellow and cross_dea
    # ZSCORE_REVERSAL
    sm = ind['zscore_smooth']; lt = ind['zscore_ltm']
    sig['ZSCORE_REVERSAL'] = False
    if idx >= 2 and not any(np.isnan(x) for x in [sm[idx], sm[idx-1], sm[idx-2], lt[idx]]):
        cur, prev, pp = sm[idx], sm[idx-1], sm[idx-2]
        sig['ZSCORE_REVERSAL'] = (cur > prev) and (prev <= pp) and (lt[idx] > 0) and (prev < -2)
    # ZSCORE_AT_ZERO
    sig['ZSCORE_AT_ZERO'] = False
    if idx >= 1 and not (np.isnan(sm[idx]) or np.isnan(sm[idx-1])):
        sig['ZSCORE_AT_ZERO'] = (sm[idx] > 0) and (sm[idx-1] <= 0)
    return sig


# ── V5 universal filters + tier ─────────────────────────────────────────
def v5_check(idx, closes, volumes):
    n = len(closes)
    if idx < 30 or idx >= n: return None
    c_today = closes[idx]; c_prev = closes[idx-1]
    if np.isnan(c_today) or np.isnan(c_prev): return None
    if c_today < MIN_PRICE: return None
    avg_vol_10d = float(np.mean(volumes[max(0,idx-9):idx+1]))
    if avg_vol_10d < MIN_AVG_VOL_10D: return None
    chg_pct = (c_today - c_prev)/c_prev*100 if c_prev else 0
    if chg_pct < CHG_MIN or chg_pct > MAX_CHG_PCT: return None
    wk = 0.0
    if idx >= 5 and closes[idx-5] > 0:
        wk = (c_today - closes[idx-5])/closes[idx-5]*100
    if wk > MAX_WEEK_PCT: return None
    avg_vol_20d = float(np.mean(volumes[max(0,idx-19):idx+1]))
    vr = volumes[idx]/avg_vol_20d if avg_vol_20d > 0 else 0
    if vr >= MAX_VOL_RATIO: return None
    return {'price': c_today, 'chg': chg_pct, 'wk': wk, 'vr': vr, 'avg_vol_10d': avg_vol_10d}


def get_tier(chg_pct):
    if chg_pct >= 10: return 'STRONG', TIER_STRONG_SIZE
    if chg_pct >= 5:  return 'GOOD',   TIER_GOOD_SIZE
    return 'FAIR',                     TIER_FAIR_SIZE


def confluence_multiplier(n_scanners):
    if n_scanners >= 3: return CONFLUENCE_3X
    if n_scanners >= 2: return CONFLUENCE_2X
    return 1.0


SCANNER_LABELS = ['STOCH_K45','DIDI_BOT','PTT_BUY','KDJ_CROSS','MACD_B','ZSCORE_REVERSAL','ZSCORE_AT_ZERO']


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    print('Loading cache ...', flush=True)
    with open(CACHE_FILE) as f:
        cache = json.load(f)
    all_dates = sorted(cache['history'].keys())
    scan_date = all_dates[-1]
    last_idx  = len(all_dates) - 1
    print(f'  Cache    : {all_dates[0]} → {scan_date}  ({len(all_dates)} days)')
    print(f'  Universe : {len(cache["tickers"]):,} tickers')
    print(f'  Scan day : {scan_date}')
    print(f'  V5 Scanners: {len(SCANNER_LABELS)}')
    print('')

    aggregated = {}   # ticker -> {scanners: [...], price, chg, wk, vr, avg_vol}
    skipped = 0

    total = len(cache['tickers'])
    for idx_t, sym in enumerate(cache['tickers']):
        if idx_t % 500 == 0:
            print(f'  Scanning {idx_t:>5}/{total:,} ...', flush=True)
        closes = []; highs = []; lows = []; volumes = []
        last_c = last_h = last_l = np.nan
        for dt in all_dates:
            bar = cache['history'].get(dt, {}).get(sym)
            if bar:
                last_c=float(bar['c']); last_h=float(bar['h']); last_l=float(bar['l'])
                closes.append(last_c); highs.append(last_h); lows.append(last_l)
                volumes.append(float(bar.get('v', 0)))
            else:
                closes.append(last_c); highs.append(last_h); lows.append(last_l)
                volumes.append(0.0)
        if all(np.isnan(x) for x in closes):
            skipped += 1; continue

        v5info = v5_check(last_idx, closes, volumes)
        if v5info is None:
            continue

        try:
            ind = precompute_indicators(closes, highs, lows)
            sigs = get_signals(ind, last_idx)
        except Exception:
            skipped += 1; continue

        hits = [name for name, fired in sigs.items() if fired]
        if not hits:
            continue

        aggregated[sym] = {
            'scanners': hits,
            'count': len(hits),
            'price': v5info['price'],
            'chg': v5info['chg'],
            'wk': v5info['wk'],
            'vr': v5info['vr'],
            'avg_vol_10d': v5info['avg_vol_10d'],
        }

    # ── Apply tier & confluence multiplier ────────────────────────────
    rows = []
    for sym, info in aggregated.items():
        tier, base_size = get_tier(info['chg'])
        mult = confluence_multiplier(info['count'])
        position = base_size * mult
        # composite score = tier rank × confluence
        tier_rank = {'STRONG': 3, 'GOOD': 2, 'FAIR': 1}[tier]
        composite = tier_rank * info['count']

        rows.append({
            'Ticker'     : sym,
            'Date'       : scan_date,
            'Price'      : round(info['price'], 2),
            'Chg_%'      : round(info['chg'], 2),
            'Wk_%'       : round(info['wk'], 2),
            'VR'         : round(info['vr'], 2),
            'AvgVol10'   : int(info['avg_vol_10d']),
            'Tier'       : tier,
            'Base_Size'  : int(base_size),
            'Confluence' : info['count'],
            'Conf_Mult'  : mult,
            'Position_$' : int(position),
            'Score'      : composite,
            'Scanners'   : '|'.join(info['scanners']),
        })

    # Sort: composite score desc, then by position size desc
    rows.sort(key=lambda r: (-r['Score'], -r['Position_$']))

    # ── Write CSV ──────────────────────────────────────────────────
    ts = datetime.now().strftime('%H%M%S')
    outfile = os.path.join(OUT_DIR, f'v5_daily_dashboard_{scan_date}_{ts}.csv')
    fields = ['Ticker','Date','Price','Chg_%','Wk_%','VR','AvgVol10',
              'Tier','Base_Size','Confluence','Conf_Mult','Position_$','Score','Scanners']
    with open(outfile, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)

    # ── Console report ─────────────────────────────────────────────
    n = len(rows)
    by_conf = {}
    by_tier = {'STRONG':[], 'GOOD':[], 'FAIR':[]}
    for r in rows:
        by_conf.setdefault(r['Confluence'], []).append(r)
        by_tier[r['Tier']].append(r)
    total_capital = sum(r['Position_$'] for r in rows)

    print('\n' + '='*96)
    print(f'  V5 DAILY DASHBOARD — {scan_date}')
    print(f'  All 7 V5-validated scanners run + confluence aggregation')
    print('='*96)
    print(f'\n  ── SUMMARY ──')
    print(f'  Unique tickers with V5 signal : {n}')
    print(f'  Skipped (no cache data)        : {skipped}')
    print(f'  Total recommended capital      : ${total_capital:,.0f}')
    print(f'  Elapsed                        : {time.time()-t0:.1f}s')
    print(f'  CSV                            : {outfile}')

    print(f'\n  ── BY CONFLUENCE ──')
    for k in sorted(by_conf.keys(), reverse=True):
        sublist = by_conf[k]
        marker = '⭐⭐⭐' if k >= 3 else ('⭐⭐' if k == 2 else '⭐')
        print(f'  {marker}  {k} scanner(s)  : {len(sublist):>3} tickers'
              + (f'   ({CONFLUENCE_3X}× position size)' if k >= 3 else
                 (f'   ({CONFLUENCE_2X}× position size)' if k == 2 else
                  f'   (1× position size)'))
              )

    print(f'\n  ── BY TIER ──')
    for tier in ['STRONG','GOOD','FAIR']:
        sub = by_tier[tier]
        if not sub: continue
        cap = sum(r['Position_$'] for r in sub)
        avg_conf = sum(r['Confluence'] for r in sub)/len(sub)
        print(f'  {tier:<7}  {len(sub):>3} tickers  |  Avg conf={avg_conf:.1f}  |  Capital ${cap:,.0f}')

    # Print top 30 rows
    print(f'\n  ── DAILY TRADE PLAN (Top {min(30,n)} by Score) ──')
    print(f'  {"Ticker":<8}{"Px":>9}{"Chg%":>7}{"Wk%":>7}{"VR":>5}  '
          f'{"Tier":<6}{"Conf":>5}{"Mult":>6}{"Pos$":>8}  Scanners')
    print('  ' + '-'*100)
    for r in rows[:30]:
        scanners_short = r['Scanners'].replace('ZSCORE_AT_ZERO','Z_ZERO').replace('ZSCORE_REVERSAL','Z_REV').replace('STOCH_K45','STK45')
        print(f'  {r["Ticker"]:<8}{r["Price"]:>9.2f}'
              f'{r["Chg_%"]:>+6.2f}%{r["Wk_%"]:>+6.2f}%{r["VR"]:>5.1f}  '
              f'{r["Tier"]:<6}{r["Confluence"]:>5}{r["Conf_Mult"]:>5.1f}x'
              f'{r["Position_$"]:>8,}  {scanners_short}')

    # Print confluence stars highlighted
    high_conf = [r for r in rows if r['Confluence'] >= 3]
    if high_conf:
        print(f'\n  ── 🏆 HIGH-CONFLUENCE (3+ scanners) — PREMIUM SIGNALS ──')
        for r in high_conf:
            print(f'  {r["Ticker"]:<8}  ${r["Price"]:>8.2f}  Chg={r["Chg_%"]:>+6.2f}%  '
                  f'Tier={r["Tier"]:<6}  Conf={r["Confluence"]}/{len(SCANNER_LABELS)}  '
                  f'Pos=${r["Position_$"]:,}\n           Scanners: {r["Scanners"]}')

    medium_conf = [r for r in rows if r['Confluence'] == 2]
    if medium_conf:
        print(f'\n  ── ⭐⭐ MEDIUM-CONFLUENCE (2 scanners) — high quality ──')
        for r in medium_conf[:15]:
            print(f'  {r["Ticker"]:<8}  ${r["Price"]:>8.2f}  Chg={r["Chg_%"]:>+6.2f}%  '
                  f'Tier={r["Tier"]:<6}  Pos=${r["Position_$"]:,}  ({r["Scanners"]})')

    print(f'\n  💡 Trade plan saved to: {outfile}\n')


if __name__ == '__main__':
    main()

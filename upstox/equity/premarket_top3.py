#!/usr/bin/env python3
"""Pre-market top-3 shortlist (READ ONLY - places NO orders).

Stage 1 (this script): rank the liquid NSE universe by the same technical score the
MTF rotation uses - 1h close above the 20 EMA with a green Supertrend, daily trend up,
ADX strength, MACD histogram, distance above the 20 EMA - and print the strongest names
with levels. Stage 2 (the routine): attach news to those names, re-rank, take the top 3.

Output per candidate: symbol, technical score, close, 1h EMA20, 1h Supertrend (also the
default stop), ADX, MACD histogram, an entry zone and a +10% first target.
CLI: python3 premarket_top3.py [--n 12] [--json] [--universe NIFTY100|ALL]
"""
import json, sys, time, gzip, glob, os

sys.path.insert(0, '/workspace/equity')
sys.path.insert(0, '/workspace/upstox')
import rotation

D = '/workspace/upstox'


def universe(kind='NIFTY100'):
    if kind == 'NIFTY100':
        return list(rotation.NIFTY100)
    km = rotation.keymap()
    return sorted(km)


def levels(c):
    close = c['close']
    st = None
    try:
        import ind
        h = ind.analyze(c['key'], 'hours', 1, 30)
        st = h['st']
    except Exception:
        pass
    stop = st if (st and st < close) else round(close * 0.95, 2)
    return dict(entry=round(close, 2), stop=round(stop, 2),
                stop_pct=round(100 * (close - stop) / close, 2),
                target1=round(close * 1.10, 2))


def main():
    a = sys.argv[1:]
    def opt(name, default):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    n = int(opt('--n', 12))
    kind = opt('--universe', 'NIFTY100')
    JS = '--json' in a
    syms = universe(kind)
    cands, held = rotation.scan(syms)
    strong = [c for c in cands if c['strong']][:n]
    rows = []
    for c in strong:
        r = dict(sym=c['sym'], score=c['score'], close=round(c['close'], 2), adx=round(c['adx'], 1),
                 macd_hist=round(c['hist'], 2), dist_ema20=c['dist'],
                 up_1h=c['up1h'], up_daily=c['upd'])
        r.update(levels(c))
        rows.append(r)
    out = dict(read_only=True, scanned=len(syms), strong=len([c for c in cands if c['strong']]),
               held=sorted(held), candidates=rows)
    if JS:
        print(json.dumps(out, indent=1)); return
    print(f"Scanned {out['scanned']} names, {out['strong']} pass the trend filter. Top {len(rows)}:")
    for r in rows:
        print(f" {r['sym']:<12} score {r['score']:>6}  close {r['close']:>9}  ADX {r['adx']:>5}  "
              f"MACD {r['macd_hist']:>6}  vsEMA20 {r['dist_ema20']:>5}%  "
              f"stop {r['stop']} (-{r['stop_pct']}%)  target {r['target1']}")
    print("\nHeld (excluded):", ', '.join(out['held']) or 'none')


if __name__ == '__main__':
    main()

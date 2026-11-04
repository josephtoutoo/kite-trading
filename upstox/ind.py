"""Indicators + Upstox candles, shared by every scanner (5 Nov 2026).

`analyze()` is the single decision input for auto_entry, trade_lock R4, risk_manager's trend
exit and every equity scanner, so the numbers must be bit-identical to the pre-refactor code.
They are: the algorithms below are unchanged, only the transport and a short-lived candle
cache were added.

Caching (in-process only, never persisted, never shared across processes):
  * candles() memoises the merged historical+intraday list for CANDLE_TTL seconds, keyed on
    (instrument, unit, interval, days). Callers in one run re-ask for the same series
    constantly (auto_entry reads each index once, mtf_dual_entry re-reads the 1h series per
    leg), and the exchange only closes a candle every interval anyway.
  * analyze() itself is not cached: it also decides whether the last candle is still forming
    from the wall clock, and the indicator maths costs under 1 ms on 600 bars.
Set CANDLE_TTL = 0 to disable the candle cache (every call refetches, the old behaviour).
"""
import time, urllib.parse, datetime as dt, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api

CANDLE_TTL = 3.0            # seconds; a run re-asking for the same series is served from memory
_candles = {}               # (key, unit, interval, days) -> (expires_at, candles)


def get(path):
    """GET api.upstox.com + path -> parsed JSON. Raises on HTTP failure (unchanged contract)."""
    return api.get(path)


def candles(key, unit, interval, days):
    ck = (key, unit, interval, int(days))
    hit = _candles.get(ck)
    now = time.monotonic()
    if hit and hit[0] > now:
        return list(hit[1])
    k = urllib.parse.quote(key, safe='')
    to = dt.date.today(); fr = to - dt.timedelta(days=days)
    hist = get(f'/v3/historical-candle/{k}/{unit}/{interval}/{to-dt.timedelta(days=1)}/{fr}')['data']['candles']
    try:
        intra = get(f'/v3/historical-candle/intraday/{k}/{unit}/{interval}')['data']['candles']
    except Exception as e:
        print(f'ind: intraday candles unavailable for {key} {unit}/{interval}: {type(e).__name__}: {e}',
              file=sys.stderr)
        intra = []
    c = sorted(hist + intra, key=lambda x: x[0])
    if CANDLE_TTL > 0:
        _candles[ck] = (now + CANDLE_TTL, c)
        if len(_candles) > 256:              # bound memory on a long scanner run
            for kk in [kk for kk, v in _candles.items() if v[0] <= now]:
                _candles.pop(kk, None)
    return list(c)  # ts,o,h,l,c,v,oi


def ema(x, n):
    a = 2 / (n + 1); out = [x[0]]
    for v in x[1:]: out.append(out[-1] + a * (v - out[-1]))
    return out


def rma(x, n):
    out = [sum(x[:n]) / n] * n
    for v in x[n:]: out.append((out[-1] * (n - 1) + v) / n)
    return out


def supertrend(h, l, c, n=10, m=3):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    atr = rma(tr, n); ub = [0] * len(c); lb = [0] * len(c); st = [0] * len(c); d = [1] * len(c)
    for i in range(len(c)):
        hl = (h[i] + l[i]) / 2; bu = hl + m * atr[i]; bl = hl - m * atr[i]
        if i == 0: ub[i], lb[i] = bu, bl; continue
        ub[i] = bu if (bu < ub[i-1] or c[i-1] > ub[i-1]) else ub[i-1]
        lb[i] = bl if (bl > lb[i-1] or c[i-1] < lb[i-1]) else lb[i-1]
        if d[i-1] == 1: d[i] = -1 if c[i] < lb[i] else 1
        else: d[i] = 1 if c[i] > ub[i] else -1
        st[i] = lb[i] if d[i] == 1 else ub[i]
    return st, d


def macd(c):
    m = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]; s = ema(m, 9); return m, s, [a - b for a, b in zip(m, s)]


def dmi(h, l, c, n=14):
    pdm = [0]; mdm = [0]; tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        up = h[i] - h[i-1]; dn = l[i-1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0); mdm.append(dn if dn > up and dn > 0 else 0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    a = rma(tr, n); p = rma(pdm, n); mm = rma(mdm, n)
    pdi = [100 * x / y if y else 0 for x, y in zip(p, a)]; mdi = [100 * x / y if y else 0 for x, y in zip(mm, a)]
    dx = [100 * abs(x - y) / (x + y) if x + y else 0 for x, y in zip(pdi, mdi)]
    return pdi, mdi, rma(dx, n)


def strong(a, min_adx=25):
    """(t295u) Is this a genuinely STRONG trend (double index-option size) rather than an ordinary one?
    Backtest on 93 fresh 5m signals (NIFTY/BANKNIFTY/SENSEX, 20 days): ADX>=25 AND rising gave
    +0.080% avg forward 60-min index move, 62% win, firing on 14% of signals; the ordinary remainder
    was -0.016%. A price-vs-EMA20 distance filter fired more often and performed WORSE (-0.19% avg), so
    it is deliberately NOT part of the test. Returns (bool, side, detail)."""
    gap = (a["close"] - a["ema20"]) / a["ema20"] * 100 if a["ema20"] else 0
    side = "CE" if (gap > 0 and a["dir"] == 1) else ("PE" if (gap < 0 and a["dir"] == -1) else None)
    adx_ok = a["adx"] >= min_adx and a["adx"] > a["prev_adx"]
    return bool(side and adx_ok), side, dict(adx=a["adx"], prev_adx=a["prev_adx"], gap_pct=round(gap, 3), adx_ok=adx_ok)


def analyze(key, unit='minutes', interval=15, days=10):
    cs = candles(key, unit, interval, days)
    now = dt.datetime.now().astimezone()
    # drop incomplete last candle
    step = dt.timedelta(minutes=interval) if unit == 'minutes' else dt.timedelta(hours=interval)
    if cs and dt.datetime.fromisoformat(cs[-1][0]) + step > now: cs = cs[:-1]
    h = [x[2] for x in cs]; l = [x[3] for x in cs]; c = [x[4] for x in cs]
    st, d = supertrend(h, l, c); e = ema(c, 20); m, s, hist = macd(c); pdi, mdi, adx = dmi(h, l, c)
    r = lambda v: round(v, 2)
    return dict(last_ts=cs[-1][0], close=c[-1], st=r(st[-1]), dir=d[-1], prev_dir=d[-2], ema20=r(e[-1]), prev_close=c[-2], prev_ema=r(e[-2]),
        macd=r(m[-1]), sig=r(s[-1]), hist=r(hist[-1]), prev_hist=r(hist[-2]), pdi=r(pdi[-1]), mdi=r(mdi[-1]), adx=r(adx[-1]), prev_adx=r(adx[-2]), prev_pdi=r(pdi[-2]))


if __name__ == '__main__':
    for k in sys.argv[1:]:
        print(k, analyze(k))

#!/usr/bin/env python3
"""Stop audit (5 Nov 2026): is every open position actually protected at the broker?

Answers one question per position: is there a LIVE sell stop (SL / SL-M, trigger pending or open)
covering the full open quantity? It reads both brokers and all three books - Upstox MTF equity,
Upstox options/futures, Kite MTF equity, Kite options/futures - and prints a compact report.

Why this exists: every protective stop here is a DAY order. They die at the close, Kite cancels
SLs outright after 15:15, and nothing arms overnight. So each morning, between the open and the
first stop-trail run, positions can be naked. This audit is the tripwire: it runs before the
trading routines and shouts if any position has no stop.

Read only. Never places, modifies or cancels an order.

CLI: python3 stop_audit.py [--json]
Exit code 1 when any position is unprotected, 0 when everything is covered, 2 on a read failure.
"""
import os, sys, json

sys.path.insert(0, '/workspace/upstox')
import api

DRY = False
LIVE = ('trigger pending', 'open', 'open pending', 'modify pending')


def upstox_book():
    """(positions, live sell stops) from Upstox: MTF equity + options/futures. Raises on bad read."""
    pos = api.get('/v2/portfolio/short-term-positions')
    mtf = api.get('/v3/portfolio/mtf-positions')
    od = api.get('/v2/order/retrieve-all')
    for r in (pos, mtf, od):
        if r.get('status') != 'success' or r.get('data') is None:
            raise RuntimeError(f'Upstox read failed: {str(r)[:120]}')
    rows = []
    for p in mtf.get('data') or []:
        q = int(p.get('quantity') or 0)
        if q > 0:
            rows.append(dict(broker='Upstox', book='MTF', sym=p['trading_symbol'].replace('-EQ', ''),
                             key=p.get('instrument_token'), qty=q, ltp=float(p.get('last_price') or 0)))
    for p in pos.get('data') or []:
        q = int(p.get('quantity') or 0)
        if q > 0:
            rows.append(dict(broker='Upstox', book='Option/Future', sym=p['trading_symbol'],
                             key=p.get('instrument_token'), qty=q, ltp=float(p.get('last_price') or 0)))
    stops = [o for o in od.get('data') or []
             if (o.get('transaction_type') or '').upper() == 'SELL'
             and (o.get('status') or '').lower() in LIVE
             and (o.get('order_type') or '').upper() in ('SL', 'SL-M')]
    return rows, stops


def kite_book():
    """(positions, live sell stops) from Kite. Raises on bad read or invalid token."""
    import broker_kite as bk
    if not bk.valid():
        raise RuntimeError('Kite token invalid')
    k = bk.client()
    ps = k.positions().get('net') or []
    hs = k.holdings() or []
    os_ = k.orders() or []
    if ps is None or os_ is None:
        raise RuntimeError('Kite returned no data')
    rows = []
    for p in ps:
        q = int(p.get('quantity') or 0)
        if q > 0 and p.get('exchange') in ('NSE', 'BSE', 'NFO', 'BFO', 'MCX'):
            rows.append(dict(broker='Kite', book=p.get('product') or '?', sym=p['tradingsymbol'],
                             key=p['tradingsymbol'], qty=q, ltp=float(p.get('last_price') or 0)))
    for h in hs:
        q = int((h.get('mtf') or {}).get('quantity') or 0)
        if q > 0:
            rows.append(dict(broker='Kite', book='MTF(holding)', sym=h['tradingsymbol'],
                             key=h['tradingsymbol'], qty=q,
                             ltp=float(h.get('last_price') or h.get('average_price') or 0)))
    stops = [o for o in os_ if o.get('transaction_type') == 'SELL'
             and (o.get('status') or '').upper() in ('TRIGGER PENDING', 'OPEN')
             and (o.get('order_type') or '') in ('SL', 'SL-M')]
    return rows, stops


def main():
    out = {'unprotected': [], 'errors': [], 'covered': 0}
    for name, fn, symf, qtyf, trigf in (
        ('Upstox', upstox_book, lambda o: (o.get('trading_symbol') or '').replace('-EQ', ''),
         lambda o: int(o.get('quantity') or 0), lambda o: float(o.get('trigger_price') or 0)),
        ('Kite', kite_book, lambda o: o.get('tradingsymbol'),
         lambda o: int(o.get('quantity') or 0), lambda o: float(o.get('trigger_price') or 0)),
    ):
        try:
            rows, stops = fn()
        except Exception as e:
            out['errors'].append(f'{name}: {type(e).__name__}: {str(e)[:160]}')
            continue
        for r in rows:
            mine = [o for o in stops if symf(o) == r['sym'] and qtyf(o) > 0]
            covered = sum(qtyf(o) for o in mine)
            r['stops'] = [round(trigf(o), 2) for o in mine]
            r['covered_qty'] = covered
            if covered >= r['qty']:
                out['covered'] += 1
            else:
                r['short_by'] = r['qty'] - covered
                out['unprotected'].append(r)
    if '--json' in sys.argv:
        print(json.dumps(out, indent=1, default=str))
    else:
        for e in out['errors']:
            print('READ FAILED:', e)
        print(f"{out['covered']} position(s) protected, {len(out['unprotected'])} not")
        for r in out['unprotected']:
            print(f"  NO STOP {r['broker']} {r['book']} {r['sym']}: {r['qty']} open"
                  + (f", stops cover {r['covered_qty']}" if r['covered_qty'] else '')
                  + (f" (ltp {r['ltp']})" if r['ltp'] else ''))
    if out['errors']:
        return 2
    return 1 if out['unprotected'] else 0


if __name__ == '__main__':
    sys.exit(main())

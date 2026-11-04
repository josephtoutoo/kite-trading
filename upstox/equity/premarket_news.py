#!/usr/bin/env python3
"""News scoring helper for the pre-market top-3 picker (READ ONLY).

Takes the technical candidates and stores/loads per-symbol news verdicts so a run can
attach news without re-fetching. The routine supplies the verdicts after reading news;
this file only keeps them and does the re-ranking arithmetic, so the ranking is
reproducible rather than eyeballed.

verdict per symbol: {"bias": "positive"|"neutral"|"negative", "impact": 0-3, "note": "..."}
final score = technical score + bias bonus - negative penalty, where
  positive: +4 * impact (impact 1..3)
  neutral : 0
  negative: -6 * impact
News outranks technicals only up to a point: a negative-news name is dropped from the
shortlist regardless of rank; ties are broken by technical score.

CLI: python3 premarket_news.py rank '<json: {candidates:[...], verdicts:{SYM:{...}}}>'
File cache: premarket_news.json {date, verdicts}
"""
import json, os, sys, datetime

D = os.path.dirname(os.path.abspath(__file__))
F = os.path.join(D, 'premarket_news.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def load(date=None):
    try:
        d = json.load(open(F))
    except Exception:
        return {}
    if date and d.get('date') != date:
        return {}
    return d.get('verdicts') or {}


def save(verdicts, date=None):
    date = date or datetime.datetime.now(IST).date().isoformat()
    json.dump({'date': date, 'verdicts': verdicts}, open(F, 'w'), indent=1)
    return F


def score(c, v):
    tech = float(c.get('score') or 0)
    if not v:
        return tech, 'no news read'
    bias = (v.get('bias') or 'neutral').lower()
    impact = max(1, min(int(v.get('impact') or 1), 3))
    if bias == 'positive':
        return tech + 4 * impact, f"news positive (impact {impact})"
    if bias == 'negative':
        return tech - 6 * impact, f"news negative (impact {impact})"
    return tech, 'news neutral'


def rank(candidates, verdicts, n=3):
    rows, dropped = [], []
    for c in candidates:
        v = verdicts.get(c['sym']) or {}
        s, why = score(c, v)
        row = dict(c, news=v or {}, final_score=round(s, 1), news_reason=why)
        if (v.get('bias') or '').lower() == 'negative' and int(v.get('impact') or 1) >= 2:
            dropped.append(row)
        else:
            rows.append(row)
    rows.sort(key=lambda r: (-r['final_score'], -float(r.get('score') or 0)))
    return dict(top=rows[:n], rest=rows[n:], dropped=dropped)


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == 'rank':
        payload = json.loads(a[1])
        print(json.dumps(rank(payload['candidates'], payload.get('verdicts') or {}), indent=1))
    elif a and a[0] == 'save':
        print('saved', save(json.loads(a[1])))
    else:
        print(__doc__)

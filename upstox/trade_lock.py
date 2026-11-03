"""Account-wide trade lock. Entry scripts MUST call can_enter(size) before any BUY.
Lock file: trade_lock.json {date, reason, loss, next_day_reduced, next_day_date}
- On lock date: no new entries.
- Next trading day (next_day_date): max 1 new entry at half size."""
import json,os,datetime
D=os.path.dirname(os.path.abspath(__file__))
LF=os.path.join(D,"trade_lock.json")
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
THRESHOLD=30000
def today(): return datetime.datetime.now(IST).date()
def next_trading_day(d):
    d=d+datetime.timedelta(days=1)
    while d.weekday()>=5: d+=datetime.timedelta(days=1)
    return d
def load(): return json.load(open(LF)) if os.path.exists(LF) else {}
def save(s): json.dump(s,open(LF,"w"),indent=1)
def lock(loss,reason,dry=False):
    d=today(); s=load()
    if s.get("date")==d.isoformat(): return s
    s={"date":d.isoformat(),"reason":reason,"loss":round(loss,2),"next_day_reduced":True,
       "next_day_date":next_trading_day(d).isoformat(),"next_day_entries_used":0}
    if not dry: save(s)
    return s
def can_enter(size=1):
    """-> (allowed, max_qty_factor, reason)"""
    s=load(); d=today().isoformat()
    if s.get("date")==d: return (False,0.0,f"LOCKED today: {s.get('reason')}")
    if s.get("next_day_reduced") and s.get("next_day_date")==d:
        if s.get("next_day_entries_used",0)>=1: return (False,0.0,"reduced day: 1 entry already used")
        return (True,0.5,"reduced day after loss lock: 1 entry at half size")
    return (True,1.0,"ok")
def record_entry():
    """Call after a BUY entry is actually placed."""
    s=load()
    if s.get("next_day_date")==today().isoformat():
        s["next_day_entries_used"]=s.get("next_day_entries_used",0)+1; save(s)
if __name__=="__main__": print(can_enter())

import os,json,requests
def p(t,x): print("==",t); print(json.dumps(x)) if "holdings" not in t else print(len([y for y in x if y["quantity"]]), "held; pnl", round(sum(y["pnl"] for y in x if y["quantity"])), "; top:", sorted([(y.get("tradingsymbol") or y.get("trading_symbol"),round(y["pnl"])) for y in x if y["quantity"]],key=lambda z:z[1])[:3], sorted([(y.get("tradingsymbol") or y.get("trading_symbol"),round(y["pnl"])) for y in x if y["quantity"]],key=lambda z:-z[1])[:3])
K=("tradingsymbol","quantity","average_price","last_price","pnl")
try:
  tok=json.load(open("/workspace/kite/token.json"))["access_token"]
  h={"X-Kite-Version":"3","Authorization":"token %s:%s"%(os.environ["KITE_API_KEY"],tok)}
  for u in ["portfolio/positions","portfolio/holdings"]:
    r=requests.get("https://api.kite.trade/"+u,headers=h).json()
    if r.get("status")=="success":
      d=r["data"]["net"] if "positions" in u else r["data"]
      r=[{k:x[k] for k in K} for x in d]
    p("kite "+u,r)
except Exception as e: print("kite err",e)
try:
  tok=json.load(open("/workspace/upstox/token.json"))["access_token"]
  h={"Accept":"application/json","Authorization":"Bearer "+tok}
  for u in ["portfolio/short-term-positions","portfolio/long-term-holdings"]:
    r=requests.get("https://api.upstox.com/v2/"+u,headers=h).json()
    if r.get("status")=="success":
      r=[{k:x.get(k) for k in ("trading_symbol","quantity","average_price","last_price","pnl")} for x in r["data"]]
    p("upstox "+u,r)
except Exception as e: print("upstox err",e)

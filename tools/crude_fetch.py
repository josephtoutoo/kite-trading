import json,requests,pandas as pd
tok=json.load(open("/workspace/upstox/token.json"))["access_token"]
h={"Accept":"application/json","Authorization":"Bearer "+tok}
pos=requests.get("https://api.upstox.com/v2/portfolio/short-term-positions",headers=h).json()["data"]
c=[x for x in pos if "CRUDE" in x["trading_symbol"]][0]
print({k:c.get(k) for k in ("trading_symbol","instrument_token","quantity","multiplier","buy_price","sell_price","day_buy_price","overnight_quantity","overnight_buy_amount","last_price","pnl","product","close_price")})
import gzip,io
ins=pd.read_json(io.BytesIO(gzip.decompress(requests.get("https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz").content)))
fut=ins[(ins.underlying_symbol=="CRUDEOIL")&(ins.instrument_type=="FUT")].sort_values("expiry").iloc[0]
opt=ins[ins.instrument_key==c["instrument_token"]]
print(fut.trading_symbol,fut.instrument_key, opt[["expiry","lot_size","strike_price"]].to_dict("records"))
for iv in ["30minute","1minute"]:
  r=requests.get(f"https://api.upstox.com/v2/historical-candle/intraday/{fut.instrument_key}/{iv}",headers=h).json()
  print(iv,len(r.get("data",{}).get("candles",[])))
import datetime
r=requests.get(f"https://api.upstox.com/v2/historical-candle/{fut.instrument_key}/30minute/2026-11-02/2026-10-20",headers=h).json()
cd=r.get("data",{}).get("candles",[])
r2=requests.get(f"https://api.upstox.com/v2/historical-candle/intraday/{fut.instrument_key}/30minute",headers=h).json()
cd=cd+r2.get("data",{}).get("candles",[])
df=pd.DataFrame(cd,columns="t o h l c v oi".split()).drop_duplicates("t").sort_values("t").reset_index(drop=True)
df.to_csv("/tmp/crude30.csv",index=False); print(len(df), df.tail(3).to_dict("records"))

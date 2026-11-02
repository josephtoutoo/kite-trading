import pandas as pd,numpy as np
d=pd.read_csv("/tmp/crude30.csv")
def st(df,n=10,m=3):
  hl=(df.h+df.l)/2; tr=pd.concat([df.h-df.l,(df.h-df.c.shift()).abs(),(df.l-df.c.shift()).abs()],axis=1).max(axis=1)
  atr=tr.ewm(alpha=1/n,adjust=False).mean(); up=hl+m*atr; dn=hl-m*atr
  fu=up.copy(); fl=dn.copy(); dirn=[1]*len(df); s=[np.nan]*len(df)
  for i in range(1,len(df)):
    fu[i]=up[i] if up[i]<fu[i-1] or df.c[i-1]>fu[i-1] else fu[i-1]
    fl[i]=dn[i] if dn[i]>fl[i-1] or df.c[i-1]<fl[i-1] else fl[i-1]
    dirn[i]= 1 if df.c[i]>fu[i-1] else (-1 if df.c[i]<fl[i-1] else dirn[i-1])
    s[i]=fl[i] if dirn[i]==1 else fu[i]
  return pd.Series(s),pd.Series(dirn)
def rep(df,name):
  df=df.reset_index(drop=True)
  df["ema20"]=df.c.ewm(span=20,adjust=False).mean()
  m=df.c.ewm(span=12,adjust=False).mean()-df.c.ewm(span=26,adjust=False).mean(); sg=m.ewm(span=9,adjust=False).mean()
  dl=df.c.diff(); rs=dl.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()/(-dl.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
  s,dr=st(df)
  upm=df.h.diff(); dnm=-df.l.diff(); pdm=np.where((upm>dnm)&(upm>0),upm,0); ndm=np.where((dnm>upm)&(dnm>0),dnm,0)
  tr=pd.concat([df.h-df.l,(df.h-df.c.shift()).abs(),(df.l-df.c.shift()).abs()],axis=1).max(axis=1).ewm(alpha=1/14,adjust=False).mean()
  pdi=100*pd.Series(pdm).ewm(alpha=1/14,adjust=False).mean()/tr; ndi=100*pd.Series(ndm).ewm(alpha=1/14,adjust=False).mean()/tr
  adx=(100*(pdi-ndi).abs()/(pdi+ndi)).ewm(alpha=1/14,adjust=False).mean()
  i=len(df)-1
  print(name,"close",df.c[i],"ema20",round(df.ema20[i]),"ST",round(s[i]),"dir",dr[i],"macd",round(m[i],1),"sig",round(sg[i],1),"hist prev",round((m-sg)[i-1],1),"rsi",round(100-100/(1+rs[i]),1),"+DI",round(pdi[i],1),"-DI",round(ndi[i],1),"ADX",round(adx[i],1))
  print(" range hi/lo last 5d", df.h.tail(80).max(), df.l.tail(80).min())
d["t"]=pd.to_datetime(d.t)
rep(d,"30m")
h=d.set_index("t").resample("1h").agg({"o":"first","h":"max","l":"min","c":"last","v":"sum"}).dropna().reset_index(); rep(h,"1h")
dd=d.set_index("t").resample("1D").agg({"o":"first","h":"max","l":"min","c":"last"}).dropna(); print(dd.tail(8))

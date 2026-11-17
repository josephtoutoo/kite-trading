import os, json, requests
from urllib.parse import urlencode
from flask import Flask, request, redirect

def _load_secret_file(path="/workspace/.secrets/upstox.env"):
    """Fill unset env vars from the private secrets file (never prints values)."""
    import os as _os, shlex as _sh
    try:
        for _l in open(path):
            _l=_l.strip()
            if not _l or _l.startswith("#") or "=" not in _l: continue
            _k,_v=_l.split("=",1)
            if True:  # secrets file is the source of truth; overrides stale injected env (fix 17 Nov)
                _p=_sh.split(_v); _os.environ[_k]=_p[0] if _p else ""
    except FileNotFoundError:
        pass
_load_secret_file()

K=os.environ["UPSTOX_API_KEY"]; S=os.environ["UPSTOX_API_SECRET"]; R="http://127.0.0.1:8001/"
app=Flask(__name__)
@app.route("/login")
def lg(): return redirect("https://api.upstox.com/v2/login/authorization/dialog?"+urlencode({"response_type":"code","client_id":K,"redirect_uri":R}))
@app.route("/")
def cb():
    if "code" not in request.args: return "Open /login"
    r=requests.post("https://api.upstox.com/v2/login/authorization/token",headers={"accept":"application/json"},data={"code":request.args["code"],"client_id":K,"client_secret":S,"redirect_uri":R,"grant_type":"authorization_code"}).json()
    if "access_token" not in r: return "Login failed: "+json.dumps(r.get("errors",r))
    open("/workspace/upstox/token.json","w").write(json.dumps({"access_token":r["access_token"]})); os.chmod("/workspace/upstox/token.json",0o600)
    return "Upstox login complete. You can close this tab."
app.run(host="127.0.0.1",port=8001)

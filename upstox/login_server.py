import os, json, requests
from urllib.parse import urlencode
from flask import Flask, request, redirect
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

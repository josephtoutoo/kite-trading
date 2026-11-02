import os, json
from flask import Flask, request
from kiteconnect import KiteConnect
k=KiteConnect(api_key=os.environ["KITE_API_KEY"])
app=Flask(__name__)
@app.route("/callback")
@app.route("/")
def cb():
    d=k.generate_session(request.args["request_token"], api_secret=os.environ["KITE_API_SECRET"])
    open("/workspace/kite/token.json","w").write(json.dumps({"access_token":d["access_token"]}))
    os.chmod("/workspace/kite/token.json",0o600)
    return "Login complete. You can close this tab."
@app.route("/login")
def lg():
    from flask import redirect
    return redirect(k.login_url())
app.run(host="127.0.0.1",port=8000)

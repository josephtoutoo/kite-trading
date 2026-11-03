#!/usr/bin/env python3
"""Headless Zerodha Kite Connect login: password + TOTP -> request_token -> token.json. Never prints secrets."""
import os, sys, json, time, re, base64, requests, pyotp
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect

TOKEN = os.environ.get("KITE_TOKEN_FILE", "/workspace/kite/token.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def totp_code(sec):
    left = 30 - int(time.time()) % 30
    if left < 5: time.sleep(left + 1)
    return pyotp.TOTP(sec).now()

def find_token(url):
    q = parse_qs(urlparse(url or "").query); return (q.get("request_token") or [None])[0]

def login():
    uid = os.environ.get("KITE_USER_ID", "XE9253"); key = os.environ["KITE_API_KEY"]
    pw = os.environ["KITE_PASSWORD"]; sec = os.environ["KITE_TOTP_SECRET"].replace(" ", "").upper()
    if len(sec) < 16 or not re.fullmatch(r"[A-Z2-7]+=*", sec):
        raise RuntimeError("KITE_TOTP_SECRET is not a base32 setup key (looks like a one-time code?)")
    s = requests.Session(); s.headers["User-Agent"] = UA
    url = f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"
    r = s.get(url, timeout=20)
    r = s.post("https://kite.zerodha.com/api/login", data={"user_id": uid, "password": pw}, timeout=20)
    j = r.json()
    if j.get("status") != "success": raise RuntimeError(f"login step: {j.get('message')}")
    d = j["data"]; rid = d["request_id"]
    if d.get("twofa_type") != "totp": raise RuntimeError(f"2FA type is {d.get('twofa_type')} (offered {d.get('twofa_types')}), not totp: enable external TOTP in Kite")
    j = s.post("https://kite.zerodha.com/api/twofa", data={"user_id": uid, "request_id": rid,
               "twofa_value": totp_code(sec), "twofa_type": "totp", "skip_totp": "true"}, timeout=20).json()
    if j.get("status") != "success": raise RuntimeError(f"twofa step: {j.get('message')}")
    tok = None; nxt = url + "&skip_session=true"
    for _ in range(10):
        try: r = s.get(nxt, allow_redirects=False, timeout=20)
        except requests.exceptions.ConnectionError as e:
            tok = find_token(str(e)); break
        loc = r.headers.get("Location")
        tok = find_token(loc) or find_token(r.url)
        if tok or not loc: break
        nxt = requests.compat.urljoin(nxt, loc)
    if not tok: raise RuntimeError(f"no request_token in redirect (last status {r.status_code})")
    sess = KiteConnect(api_key=key).generate_session(tok, api_secret=os.environ["KITE_API_SECRET"])
    with open(TOKEN, "w") as f: json.dump({"access_token": sess["access_token"]}, f)
    os.chmod(TOKEN, 0o600)
    return sess.get("user_id")

if __name__ == "__main__":
    try: print(f"OK: Kite logged in as {login()}")
    except Exception as e: print(f"FAIL: {type(e).__name__}: {str(e)[:200]}"); sys.exit(1)

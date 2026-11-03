#!/usr/bin/env python3
"""Fully automatic Upstox login: mobile -> TOTP -> PIN -> token.json. Never prints secrets."""
import os, sys, time, json, subprocess, requests, pyotp
from playwright.sync_api import sync_playwright

BASE = "/workspace/upstox"; TOKEN = f"{BASE}/token.json"; SHOT = f"{BASE}/auto_login_fail.png"
MOBILE = os.environ["UPSTOX_MOBILE"]; SERVER = "http://127.0.0.1:8001"

def server_up():
    try: requests.get(SERVER + "/", timeout=3); return True
    except Exception: return False

def ensure_server():
    if server_up(): return
    subprocess.Popen(f"nohup python3 {BASE}/login_server.py > {BASE}/server.log 2>&1 &", shell=True)
    for _ in range(20):
        time.sleep(0.5)
        if server_up(): return
    raise RuntimeError("login server did not start")

def totp_code():
    t = pyotp.TOTP(os.environ["UPSTOX_TOTP_SECRET"].replace(" ", "").upper())
    left = 30 - int(time.time()) % 30
    if left < 5: time.sleep(left + 1)
    return t.now()

def sms_only(page):
    txt = page.inner_text("body").lower()
    return "sms" in txt and "totp" not in txt and "pin" not in txt

class Rejected(Exception): pass

def attempt():
    mtime0 = os.path.getmtime(TOKEN) if os.path.exists(TOKEN) else 0
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); ctx = b.new_context(); page = ctx.new_page()
        page.set_default_timeout(30000)
        verify = {}
        def on_resp(r):
            if "otp-totp/verify" in r.url:
                try: verify.update(r.json())
                except Exception: pass
        page.on("response", on_resp)
        try:
            page.goto(SERVER + "/login")
            page.wait_for_url("**login.upstox.com/**")
            page.locator("#mobileNum, input[type=tel], input[name*=mobile i]").first.fill(MOBILE)
            page.get_by_role("button", name="Get OTP").click()
            otp = page.locator("#otpNum, input[placeholder*='OTP' i]").first
            otp.wait_for()
            otp.click(); otp.press_sequentially(totp_code(), delay=120)
            btn = page.locator("#continueBtn")
            page.wait_for_function("() => { const b=document.querySelector('#continueBtn'); return b && !b.disabled; }", timeout=10000)
            btn.click()
            try: open(f"{BASE}/after_otp.html", "w").write(page.content())
            except Exception: pass
            pin = page.locator("#pinCode, input[type=password], input[inputmode=numeric]").first
            for _ in range(20):  # page.wait_for_timeout pumps Playwright events; time.sleep does not
                if verify: break
                page.wait_for_timeout(500)
            if verify and not verify.get("success", True):
                raise Rejected("TOTP rejected by Upstox: " + str(verify.get("error", {}).get("message", ""))[:120])
            try: pin.wait_for(timeout=20000)
            except Exception:
                raise RuntimeError("PIN screen not shown" + (" (SMS-only prompt)" if sms_only(page) else " (TOTP rejected?)"))
            time.sleep(1); open(f"{BASE}/pin_page.html", "w").write(page.content())
            pin.click(); pin.press_sequentially(os.environ["UPSTOX_PIN"], delay=120)
            pbtn = page.locator("#pinContinueBtn, #continueBtn, button:has-text('Continue')").first
            try:
                page.wait_for_function("() => { const b=document.querySelector('#pinContinueBtn, #continueBtn'); return !b || !b.disabled; }", timeout=5000)
                if pbtn.is_visible() and pbtn.is_enabled(): pbtn.click()
            except Exception: pass
            page.wait_for_url("http://127.0.0.1:8001/**", timeout=45000)
            for _ in range(30):
                if os.path.exists(TOKEN) and os.path.getmtime(TOKEN) > mtime0: break
                time.sleep(0.5)
            else:
                raise RuntimeError("token.json not updated: " + page.inner_text("body")[:200])
        except Exception:
            try: page.screenshot(path=SHOT, full_page=True)
            except Exception: pass
            raise
        finally: b.close()
    tok = json.load(open(TOKEN))["access_token"]
    r = requests.get("https://api.upstox.com/v2/user/profile", timeout=15,
                     headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()["data"]["user_id"]

def main():
    t0 = time.time(); last = None
    sec = os.environ.get("UPSTOX_TOTP_SECRET", "").replace(" ", "").upper()
    try: import base64; base64.b32decode(sec + "=" * (-len(sec) % 8)); assert len(sec) >= 16
    except Exception: print("FAIL: UPSTOX_TOTP_SECRET is not a valid base32 setup key"); sys.exit(3)
    try: ensure_server()
    except Exception as e: print(f"FAIL: {e}"); sys.exit(2)
    for i in range(2):  # 1 try + up to 2 retries
        try:
            uid = attempt(); print(f"OK: Upstox logged in as {uid} in {time.time()-t0:.1f}s"); sys.exit(0)
        except Rejected as e:
            print(f"FAIL: {e} — not retrying to avoid account lockout (screenshot {SHOT})"); sys.exit(4)
        except Exception as e:
            last = str(e).split("\n")[0][:200]; print(f"attempt {i+1} failed: {last}", file=sys.stderr)
            time.sleep(130)  # Upstox allows a new OTP request only every 2 min
    print(f"FAIL: {last} (screenshot {SHOT})"); sys.exit(1)

if __name__ == "__main__": main()

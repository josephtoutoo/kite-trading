"""Official Kite Connect redirect callback.

A person opens /login, signs in on Zerodha's page (password and 2FA stay on that page),
and Kite redirects to the URL registered for this api_key with a one-time request_token.
This process exchanges that token and stores the session. It does not collect a password,
TOTP secret, or browser session.

The registered redirect URL must be http://127.0.0.1:8000/ or http://127.0.0.1:8000/callback.
Kite Connect does not take a redirect override on the login URL.
"""
import datetime, json, os

DEFAULT_TOKEN = "/workspace/kite/token.json"


def token_path():
    return os.environ.get("KITE_TOKEN_FILE") or DEFAULT_TOKEN


def session_record(data, api_key):
    """Keep the access token and the public api_key it belongs to. Drop every other secret."""
    if not isinstance(data, dict):
        raise RuntimeError("session exchange returned no data")
    token = data.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("session exchange returned no access_token")
    bound = data.get("api_key") if isinstance(data.get("api_key"), str) else ""
    bound = (bound or api_key or "").strip()
    api_key = (api_key or "").strip()
    if not bound or bound != api_key:
        raise RuntimeError("session api_key does not match KITE_API_KEY")
    rec = {"access_token": token.strip(), "api_key": bound}
    uid = data.get("user_id")
    if isinstance(uid, str) and uid.strip():
        rec["user_id"] = uid.strip()
    lt = data.get("login_time")
    if isinstance(lt, datetime.datetime):
        rec["login_time"] = lt.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(lt, str) and lt.strip():
        rec["login_time"] = lt.strip()
    return rec


def write_token(path, record):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
        f.write("\n")
    os.chmod(path, 0o600)


def exchange_session(request_token, api_key, api_secret):
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=api_key).generate_session(request_token, api_secret=api_secret)


def handle_callback(args, api_key, api_secret, exchange, path):
    """Return (http_status, body). Never echo secrets or exception text."""
    status = args.get("status")
    if status not in (None, "", "success"):
        return 400, "Login was not completed."
    request_token = args.get("request_token")
    request_token = request_token.strip() if isinstance(request_token, str) else ""
    if not request_token:
        return 400, "Open /login and finish Kite Connect sign-in."
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    if not key or not secret:
        return 400, "KITE_API_KEY and KITE_API_SECRET must both be set."
    try:
        data = exchange(request_token, key, secret)
        write_token(path, session_record(data, key))
    except Exception:
        return 400, "Session exchange failed. Start login again with this app's api_key and api_secret."
    return 200, "Login complete. You can close this tab."


def serve():
    from flask import Flask, redirect, request
    app = Flask(__name__)

    @app.route("/callback")
    @app.route("/")
    def cb():
        code, body = handle_callback(
            request.args,
            os.environ.get("KITE_API_KEY"),
            os.environ.get("KITE_API_SECRET"),
            exchange_session,
            token_path(),
        )
        return body, code

    @app.route("/login")
    def lg():
        key = (os.environ.get("KITE_API_KEY") or "").strip()
        if not key:
            return "KITE_API_KEY must be set.", 400
        from kiteconnect import KiteConnect
        return redirect(KiteConnect(api_key=key).login_url())

    app.run(host="127.0.0.1", port=8000)


if __name__ == "__main__":
    serve()

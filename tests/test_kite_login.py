#!/usr/bin/env python3
"""Offline tests for the official Kite Connect callback and token pairing.

No broker, network, password, or TOTP. Fake tokens only.
"""
import datetime, importlib.util, json, os, stat, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import net_block
net_block.install()
sys.path.insert(0, os.path.join(ROOT, "upstox"))
import broker_kite

_spec = importlib.util.spec_from_file_location("kite_login_server", os.path.join(ROOT, "login_server.py"))
login_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(login_server)

FAILS = []
PASSED = [0]


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"FAIL  {name}  {detail}")
    else:
        PASSED[0] += 1
        print(f"ok    {name}")


SECRET = "test-api-secret-do-not-store"
TOKEN = "test-access-token"
OTHER = "test-other-token"
KEY = "test-api-key"
ENCTOKEN = "test-enctoken"
EMAIL = "person@example.com"


def test_callback_persists_paired_session():
    seen = {}

    def exchange(request_token, api_key, api_secret):
        seen["request_token"] = request_token
        seen["api_key"] = api_key
        seen["api_secret"] = api_secret
        return {
            "access_token": TOKEN,
            "api_key": api_key,
            "user_id": "AB1234",
            "login_time": datetime.datetime(2026, 11, 7, 9, 15, 0),
            "api_secret": SECRET,
            "enctoken": ENCTOKEN,
            "refresh_token": "nope",
            "email": EMAIL,
            "public_token": "pub",
        }

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "missing", "kite", "token.json")
        code, body = login_server.handle_callback(
            {"request_token": " rt ", "status": "success"},
            api_key="  " + KEY + "\n",
            api_secret="  " + SECRET + "\n",
            exchange=exchange,
            path=path,
        )
        check("callback 200", code == 200, body)
        check("exchange saw stripped key", seen.get("api_key") == KEY, seen.get("api_key"))
        check("exchange saw stripped secret", seen.get("api_secret") == SECRET)
        check("request token stripped", seen.get("request_token") == "rt")
        saved = json.load(open(path))
        check("saved access token", saved.get("access_token") == TOKEN)
        check("saved api_key", saved.get("api_key") == KEY)
        check("saved user and login time", saved.get("user_id") == "AB1234" and saved.get("login_time") == "2026-11-07 09:15:00", saved)
        leaked = [k for k in ("api_secret", "enctoken", "refresh_token", "email", "public_token") if k in saved]
        check("secret fields omitted", not leaked, leaked)
        blob = open(path).read()
        check("secret values absent from file", SECRET not in blob and ENCTOKEN not in blob and EMAIL not in blob)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check("token file mode 0600", mode == 0o600, oct(mode))
        parent_mode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        check("token dir mode 0700", parent_mode == 0o700, oct(parent_mode))


def test_callback_does_not_exchange_or_write_on_bad_redirect():
    calls = []

    def exchange(*a):
        calls.append(a)
        return {"access_token": TOKEN, "api_key": KEY}

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "token.json")
        cases = [
            {"status": "error", "request_token": "rt"},
            {},
            {"request_token": "   "},
            {"status": "success"},
        ]
        for args in cases:
            code, body = login_server.handle_callback(args, KEY, SECRET, exchange, path)
            check(f"reject {args}", code == 400 and not os.path.exists(path), body)
        code, body = login_server.handle_callback(
            {"request_token": "rt", "status": "success"}, "", SECRET, exchange, path
        )
        check("reject missing key", code == 400 and not os.path.exists(path), body)
        check("exchange not called", calls == [], calls)


def test_callback_hides_exchange_errors_and_mismatch():
    def boom(request_token, api_key, api_secret):
        raise RuntimeError("boom " + SECRET + " " + request_token)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "token.json")
        code, body = login_server.handle_callback(
            {"request_token": "reqtok-99", "status": "success"}, KEY, SECRET, boom, path
        )
        check("exchange error is 400", code == 400 and not os.path.exists(path), body)
        check("error body has no secret", SECRET not in body and "reqtok-99" not in body, body)

        def wrong_key(request_token, api_key, api_secret):
            return {"access_token": TOKEN, "api_key": "other-app"}

        code, body = login_server.handle_callback(
            {"request_token": "rt"}, KEY, SECRET, wrong_key, path
        )
        check("mismatched session api_key not stored", code == 400 and not os.path.exists(path), body)


def test_reader_pairs_key_and_accepts_legacy_file():
    old = os.environ.get("KITE_TOKEN_FILE")
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token.json")
            os.environ["KITE_TOKEN_FILE"] = path
            json.dump({"access_token": "  " + TOKEN + "\n", "api_key": KEY}, open(path, "w"))
            check("matching key loads stripped token", broker_kite.load_access_token("\n" + KEY + " ") == TOKEN)
            try:
                broker_kite.load_access_token("other-app")
                check("mismatch raises", False, "no exception")
            except RuntimeError as e:
                text = str(e)
                check("mismatch before use", "does not match" in text)
                check("mismatch text has no token", TOKEN not in text and KEY not in text, text)
            json.dump({"access_token": OTHER}, open(path, "w"))
            check("legacy file without api_key still loads", broker_kite.load_access_token(KEY) == OTHER)
            check("same default path", login_server.DEFAULT_TOKEN == broker_kite.DEFAULT_TOKEN == "/workspace/kite/token.json")
            os.environ.pop("KITE_TOKEN_FILE", None)
            check("default path when unset", broker_kite.token_path() == login_server.token_path() == "/workspace/kite/token.json")
    finally:
        if old is None:
            os.environ.pop("KITE_TOKEN_FILE", None)
        else:
            os.environ["KITE_TOKEN_FILE"] = old


def test_import_does_not_open_a_port():
    # serve() is what imports Flask and binds 127.0.0.1:8000. Importing must not build that app.
    check("no flask app at import", not hasattr(login_server, "app"))


if __name__ == "__main__":
    test_callback_persists_paired_session()
    test_callback_does_not_exchange_or_write_on_bad_redirect()
    test_callback_hides_exchange_errors_and_mismatch()
    test_reader_pairs_key_and_accepts_legacy_file()
    test_import_does_not_open_a_port()
    print(f"RESULT login passed={PASSED[0]} failed={len(FAILS)} network_attempts={len(net_block.ATTEMPTS)}")
    if FAILS or net_block.ATTEMPTS:
        sys.exit(1)
    print("all passed")

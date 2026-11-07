#!/usr/bin/env python3
"""Local simulation of one Indian Hotels equity order through broker_kite.place.

This does not authenticate, does not accept a broker sandbox order, and does not
submit anything. Sockets and HTTP are fail-closed. Credentials are fake literals.

Test assumptions, not a trading instruction: side BUY, product CNC, order type LIMIT.
The limit price is the sentinel 0. It is not a quote and not a fill.
"""
import glob, json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import net_block
net_block.install()

# Drop broker env before any adapter import. Do not print the previous values.
for _k in list(os.environ):
    if _k.startswith(("KITE_", "UPSTOX_")) or _k.endswith(("_TOKEN", "_SECRET", "_PASSWORD")):
        os.environ.pop(_k, None)

sys.path.insert(0, os.path.join(ROOT, "upstox"))
import broker_kite

FAILS = []
PASSED = [0]
SYMBOL = "INDHOTEL"
QTY = 2
# Sentinel only. The adapter requires a LIMIT price; the repo has no INDHOTEL quote.
PRICE_SENTINEL = 0
TAG = "sim_dummy"


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"FAIL  {name}  {detail}")
    else:
        PASSED[0] += 1
        print(f"ok    {name}")


class KiteConnect:
    """In-process stand-in. place_order records then raises so a leak cannot succeed."""
    instances = []
    mode = "reject"

    def __init__(self, api_key, proxies=None, timeout=None):
        self.api_key = api_key
        self.proxies = proxies
        self.access_token = None
        self.profile_calls = 0
        self.orders = []
        KiteConnect.instances.append(self)

    def set_access_token(self, token):
        self.access_token = token

    def profile(self):
        self.profile_calls += 1
        if self.mode != "accept":
            raise RuntimeError("simulated TokenException: Incorrect api_key or access_token")
        return {"user_id": "SIMULATED"}

    def place_order(self, **body):
        self.orders.append(body)
        raise AssertionError("place_order blocked: this simulation must not submit")


def install_fake_kite():
    import types
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = KiteConnect
    sys.modules["kiteconnect"] = mod


def reset():
    broker_kite._k = None
    broker_kite._valid = None
    broker_kite._why = ""
    broker_kite._reads.clear()
    broker_kite._dump.clear()
    broker_kite._idx.clear()
    KiteConnect.instances.clear()
    KiteConnect.mode = "reject"


def seed_empty_nse_master():
    """The checkout has no instrument file. An empty in-memory master avoids the public dump."""
    broker_kite._dump["NSE"] = []
    broker_kite._idx.pop("NSE", None)


def write_token(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def place(**kw):
    args = dict(exch="NSE", tsym=SYMBOL, side="BUY", qty=QTY, price=PRICE_SENTINEL,
                order_type="LIMIT", product="CNC", tag=TAG, dry=False)
    args.update(kw)
    return broker_kite.place(**args)


def all_orders():
    return [body for inst in KiteConnect.instances for body in inst.orders]


def profile_calls():
    return sum(inst.profile_calls for inst in KiteConnect.instances)


def indhotel_occurrences():
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "tests")]
        for name in filenames:
            if not name.endswith((".py", ".md", ".json", ".txt")):
                continue
            path = os.path.join(dirpath, name)
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if "INDHOTEL" in line:
                    hits.append("%s:%d" % (os.path.relpath(path, ROOT), n))
    return hits


def test_mapping():
    hits = indhotel_occurrences()
    check("INDHOTEL name is in the repo", any(h.startswith("upstox/equity/rotation.py") or h.startswith("upstox/rotation.py") for h in hits), hits)
    masters = glob.glob(os.path.join(ROOT, "upstox", "instruments_NSE_*.json.gz"))
    masters += glob.glob(os.path.join(ROOT, "upstox", "kite_instruments_*.csv"))
    check("no instrument master file is checked in", masters == [], masters)
    check("equity segment is not in the Kite symbol map", "NSE_EQ" not in broker_kite.SEG and "NSE" not in broker_kite.SEG.values(), broker_kite.SEG)
    check("NSE_EQ key does not map", broker_kite.from_upstox("NSE_EQ|INDHOTEL") is None)
    # Prove the adapter would fetch a master, and that the fetch is blocked.
    before = len(net_block.ATTEMPTS)
    reset()
    raised = False
    try:
        broker_kite.dump("NSE")
    except OSError as e:
        raised = "network blocked" in str(e)
    check("missing master does not fall through to the network", raised, net_block.ATTEMPTS[before:])
    check("blocked fetch wrote no instrument csv", glob.glob(os.path.join(broker_kite.D, "kite_instruments_NSE_*.csv")) == [])
    net_block.ATTEMPTS.clear()
    return hits


def test_tokens_fail_closed(path):
    reset()
    seed_empty_nse_master()
    os.environ["KITE_API_KEY"] = "test-api-key"
    os.environ["KITE_TOKEN_FILE"] = path
    os.environ["UPSTOX_PROXY_URL"] = "http://127.0.0.1:9"
    write_token(path, {"access_token": "test-access-token-A", "api_key": "other-app-key"})
    got = place()
    why = broker_kite.why()
    check("mismatched token returns no order id", got is None, got)
    check("mismatch is reported without the token", "does not match" in why and "test-access-token-A" not in why, why)
    check("mismatch does not call profile or place_order", profile_calls() == 0 and all_orders() == [])

    reset()
    seed_empty_nse_master()
    write_token(path, {"access_token": "   ", "api_key": "test-api-key"})
    check("blank token returns no order id", place() is None)
    check("blank token does not submit", all_orders() == [])

    reset()
    seed_empty_nse_master()
    os.remove(path)
    check("missing token file returns no order id", place() is None)
    check("missing token file does not submit", all_orders() == [])

    reset()
    seed_empty_nse_master()
    write_token(path, {"access_token": "test-access-token-B", "api_key": "test-api-key"})
    KiteConnect.mode = "reject"
    got = place()
    check("rejected profile returns no order id", got is None, got)
    check("rejected profile text does not contain the token", "test-access-token-B" not in broker_kite.why(), broker_kite.why())
    check("rejected profile never reaches place_order", profile_calls() == 1 and all_orders() == [])

    reset()
    seed_empty_nse_master()
    os.environ.pop("UPSTOX_PROXY_URL", None)
    write_token(path, {"access_token": "test-access-token-C", "api_key": "test-api-key"})
    KiteConnect.mode = "accept"
    check("missing proxy returns no order id", place() is None)
    check("missing proxy does not construct a submission", profile_calls() == 0 and all_orders() == [])


def test_dry_payload(path):
    """dry=True is the adapter's submission block. Profile would succeed if it were called."""
    reset()
    seed_empty_nse_master()
    os.environ["KITE_API_KEY"] = "test-api-key"
    os.environ["KITE_TOKEN_FILE"] = path
    os.environ["UPSTOX_PROXY_URL"] = "http://127.0.0.1:9"
    write_token(path, {"access_token": "test-access-token-D", "api_key": "test-api-key"})
    KiteConnect.mode = "accept"
    captured = []
    original = broker_kite.log

    def wrapped(msg):
        captured.append(msg)
        return original(msg)

    broker_kite.log = wrapped
    log_path = os.path.join(broker_kite.D, "kite_broker.log")
    before = open(log_path, "rb").read() if os.path.exists(log_path) else None
    try:
        result = place(dry=True)
    finally:
        broker_kite.log = original
        if before is None:
            if os.path.exists(log_path):
                os.remove(log_path)
        else:
            with open(log_path, "wb") as f:
                f.write(before)
    check("dry run returns DRY", result == "DRY", result)
    check("dry run does not call profile or place_order", profile_calls() == 0 and all_orders() == [])
    dry_lines = [m for m in captured if m.startswith("DRY place ")]
    check("one dummy payload and no mirror order", len(dry_lines) == 1, dry_lines)
    body = json.loads(dry_lines[0][len("DRY place "):]) if dry_lines else {}
    check("symbol INDHOTEL", body.get("tradingsymbol") == SYMBOL, body)
    check("exchange NSE", body.get("exchange") == "NSE", body)
    check("quantity 2", body.get("quantity") == QTY, body)
    check("product CNC", body.get("product") == "CNC", body)
    check("side BUY", body.get("transaction_type") == "BUY", body)
    check("limit order", body.get("order_type") == "LIMIT" and body.get("variety") == "regular" and body.get("validity") == "DAY", body)
    check("sentinel price not a quote", body.get("price") == PRICE_SENTINEL, body)
    check("payload has no instrument token or fill", "instrument_token" not in body and "filled_quantity" not in body and "average_price" not in body, body)
    check("tag is the simulation tag", body.get("tag") == TAG, body)
    # Default product is NRML. CNC is present only because this test passed it explicitly.
    reset()
    seed_empty_nse_master()
    captured.clear()
    broker_kite.log = wrapped
    try:
        broker_kite.place("NSE", SYMBOL, "BUY", QTY, PRICE_SENTINEL, dry=True)
    finally:
        broker_kite.log = original
    default_body = json.loads(captured[-1][len("DRY place "):])
    check("omitted product would be NRML, so CNC was explicit", default_body.get("product") == "NRML", default_body)
    check("market orders are refused", broker_kite.place("NSE", SYMBOL, "BUY", QTY, PRICE_SENTINEL, order_type="MARKET", product="CNC", dry=False) is None)
    check("refused market order does not submit", all_orders() == [])
    return body


def main():
    install_fake_kite()
    hits = test_mapping()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "token.json")
        test_tokens_fail_closed(path)
        body = test_dry_payload(path)
    check("no unexpected network attempts", net_block.ATTEMPTS == [], net_block.ATTEMPTS)

    login = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", "test_kite_login.py")],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C.UTF-8",
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True,
    )
    login_line = next((ln for ln in login.stdout.splitlines() if ln.startswith("RESULT login ")), "")
    check("official login tests still pass offline", login.returncode == 0 and "failed=0" in login_line, login_line or login.stderr[-400:])

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    outcome = {
        "label": "LOCAL_ADAPTER_SIMULATION_NOT_SUBMITTED",
        "proves": "adapter payload construction and local fail-closed gates",
        "does_not_prove": [
            "live Kite authentication",
            "broker sandbox acceptance",
            "a fill or a market price",
            "browser loopback reachability",
        ],
        "assumptions": {
            "side": "BUY",
            "product": "CNC",
            "order_type": "LIMIT",
            "price_sentinel": PRICE_SENTINEL,
            "price_meaning": "not a quote and not a fill; the adapter requires a LIMIT price",
        },
        "instrument_mapping": {
            "symbol": SYMBOL,
            "repo_occurrences": hits,
            "instrument_token_in_repo": False,
            "exchange_token_in_repo": False,
            "kite_exchange_field": "NSE",
            "note": "Lines containing INDHOTEL are listed above. No NSE instrument master is in the checkout, so there is no exchange token or official tick. broker_kite.from_upstox maps NSE_FO, BSE_FO, and MCX_FO only.",
        },
        "captured_dummy_payload": body,
        "simulated_outcome": "NOT_SUBMITTED",
        "submission_blocked_by": ["dry=True returns before place_order", "mismatched, blank, and missing tokens", "profile rejection", "missing UPSTOX_PROXY_URL"],
        "upstox_order_calls": 0,
        "unexpected_network_attempts": list(net_block.ATTEMPTS),
        "login_result_line": login_line,
        "order_passed": PASSED[0],
        "order_failed": len(FAILS),
        "head": head,
    }
    out = os.environ.get("SIM_RESULT_PATH", "/opt/cursor/artifacts/indhotel_sim_order_result.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(outcome, f, indent=2)
        f.write("\n")
    print("RESULT order passed=%d failed=%d network_attempts=%d" % (PASSED[0], len(FAILS), len(net_block.ATTEMPTS)))
    print("RESULT artifact %s" % out)
    if FAILS or net_block.ATTEMPTS:
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()

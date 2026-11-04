"""One shared Upstox HTTP client for every script in this folder (5 Nov 2026).

Why: every call used to build a fresh urllib opener, so each request paid a new TLS
handshake, and only some call sites sent the browser User-Agent Cloudflare insists on
(a missing one answers 403 with body code 1010).

What this module owns, once, for everyone:
  * a kept-alive connection pool per (host, proxy) - connections are reused, not rebuilt;
  * the fixed User-Agent ('Mozilla/5.0') and the bearer token on every Upstox request;
  * an explicit timeout on every call (DEFAULT_TIMEOUT unless the caller passes one);
  * the static-IP proxy for order endpoints, and for anyone who asks for it;
  * retry with backoff ONLY for transient GET failures (broken keep-alive / connection
    reset, socket timeout, 429, 5xx, Cloudflare 1010). A POST/PUT/DELETE is NEVER retried:
    placing an order twice is a different trade, not a retry.

Routing (`proxy=`):
  None (default) -> proxy only for api-hft.upstox.com (the order host), reads direct.
                    This is what ind/mtf_sizing/trade_lock already did.
  False          -> never proxy, whatever the host.
  True or a URL  -> always proxy (the scripts that built one proxied opener for all calls).

Call shapes (stdlib only, no framework):
  api.get('/v2/user/get-funds-and-margin')       -> parsed JSON (raises ApiError on HTTP error)
  api.request(url, body, method)                 -> parsed JSON (raises on HTTP error)
  api.call(url, body, method)                    -> parsed JSON or {'status':'error','code':..,'body':..}
Every script keeps its own function names and return types; only the transport changes.
"""
import json, os, sys, time, gzip, base64, random, socket, ssl, http.client, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
UA = 'Mozilla/5.0'
DEFAULT_TIMEOUT = 20.0
RETRIES = 3                 # extra attempts for a transient GET failure (4 tries total);
                            # a Cloudflare-fronted host does occasionally reset a TLS
                            # connection mid-handshake, and one retry was not always enough
BACKOFF = 0.4               # seconds before retry 1; doubled for each retry after that
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
CF_1010 = '1010'
JITTER = 0.25               # +- fraction on each backoff, so routines that fail together do not
                            # retry in lockstep and hammer the same host
ERRLF = os.path.join(D, 'api_errors.log')   # one line per GET that exhausted its retries

_pool = {}                  # (host, proxy) -> http.client.HTTPSConnection
_last_used = {}             # (host, proxy) -> monotonic time of the last completed request
WRITE_IDLE = 30.0           # seconds: a write on a connection idle longer than this starts fresh
_tok = {'stamp': None, 'value': None}
_ctx = None


class ApiError(Exception):
    """An HTTP-level failure (4xx/5xx). Carries the status code and a body excerpt."""

    def __init__(self, code, body, url=''):
        self.code = code
        self.body = body or ''
        self.url = url
        super().__init__(f'HTTP {code}: {(self.body or "")[:160]}')


def _ssl_context():
    global _ctx
    if _ctx is None:
        _ctx = ssl.create_default_context()
    return _ctx


def token():
    """The Upstox access token, re-read whenever token.json changes (auto_login rotates it)."""
    f = os.path.join(D, 'token.json')
    try:
        stamp = os.stat(f).st_mtime_ns
    except OSError:
        stamp = None
    if stamp is not None and stamp == _tok['stamp'] and _tok['value']:
        return _tok['value']
    with open(f) as fh:
        val = json.load(fh)['access_token']
    _tok['stamp'], _tok['value'] = stamp, val
    return val


def proxy_url():
    return os.environ.get('UPSTOX_PROXY_URL') or None


def _resolve_proxy(url, proxy):
    """-> proxy URL to use, or None for a direct connection."""
    if proxy is False:
        return None
    if proxy in (None, ''):
        return proxy_url() if urllib.parse.urlsplit(url).hostname.startswith('api-hft.') else None
    return proxy_url() if proxy is True else proxy


def _conn(host, proxy, timeout):
    key = (host, proxy)
    c = _pool.get(key)
    if c is not None:
        return c, key
    ctx = _ssl_context()
    if proxy:
        px = urllib.parse.urlsplit(proxy)
        c = http.client.HTTPSConnection(px.hostname, px.port or 443, timeout=timeout, context=ctx)
        hdrs = {}
        if px.username:
            cred = '%s:%s' % (urllib.parse.unquote(px.username), urllib.parse.unquote(px.password or ''))
            hdrs['Proxy-Authorization'] = 'Basic ' + base64.b64encode(cred.encode()).decode()
        c.set_tunnel(host, 443, headers=hdrs)
    else:
        c = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ctx)
    _pool[key] = c
    return c, key


def _drop(key):
    _last_used.pop(key, None)
    c = _pool.pop(key, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _attempt(url, body, method, proxy, timeout, extra_headers):
    """One request on a pooled connection -> (status, text). Raises on transport errors."""
    u = urllib.parse.urlsplit(url)
    host = u.hostname
    path = u.path + (('?' + u.query) if u.query else '')
    hdrs = {'Accept': 'application/json', 'User-Agent': UA}
    if host and host.startswith('api') and host.endswith('upstox.com'):
        hdrs['Authorization'] = 'Bearer ' + token()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs['Content-Type'] = 'application/json'
    if extra_headers:
        hdrs.update(extra_headers)
    c, key = _conn(host, proxy, timeout)
    # 5 Nov 2026: an order POST on a connection that has been idle for a while is the one call where a
    # dead keep-alive socket is expensive - the write may have reached the broker before the read side
    # failed, and a POST is never retried. Open a fresh connection for it instead of gambling.
    if method in ('POST', 'PUT', 'DELETE', 'PATCH') and key in _last_used \
            and time.monotonic() - _last_used[key] > WRITE_IDLE:
        _drop(key)
        c, key = _conn(host, proxy, timeout)
    try:
        c.timeout = timeout
        c.request(method, path, body=data, headers=hdrs)
        resp = c.getresponse()
        text = resp.read().decode('utf-8', 'replace')
    except Exception:
        _drop(key)          # stale keep-alive or broken socket: never put it back
        _last_used.pop(key, None)
        raise
    _last_used[key] = time.monotonic()
    if resp.will_close:
        _drop(key)
    return resp.status, text


def _log_attempts(url, tries, err):
    """Append a line whenever a GET burns every retry, so a real transport drop is diagnosable
    after the fact instead of vanishing into a caller's generic error message. Best effort:
    a logging failure must never turn into a request failure."""
    try:
        u = urllib.parse.urlsplit(url)
        now = time.time() + 19800                      # box clock may be UTC; log in IST
        line = '%s IST GET %s%s tries=%d %s: %s\n' % (
            time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now)), u.hostname, u.path, tries,
            type(err).__name__, str(err).replace('\n', ' ')[:160])
        with open(ERRLF, 'a') as fh:
            fh.write(line)
        if os.path.getsize(ERRLF) > 500_000:      # keep the tail, never grow without bound
            with open(ERRLF) as fh:
                tail = fh.readlines()[-500:]
            with open(ERRLF, 'w') as fh:
                fh.writelines(tail)
    except Exception:
        pass


def _sleep(i):
    time.sleep(BACKOFF * (2 ** i) * (1 + random.uniform(-JITTER, JITTER)))


def _transient(status, text):
    return status in TRANSIENT_STATUS or (status == 403 and CF_1010 in (text or '')[:400])


def request(url, body=None, method=None, proxy=None, timeout=DEFAULT_TIMEOUT, headers=None):
    """-> parsed JSON. Retries GETs on transient failures only. Raises ApiError on HTTP error."""
    method = (method or ('POST' if body is not None else 'GET')).upper()
    use_proxy = _resolve_proxy(url, proxy)
    attempts = RETRIES + 1 if method == 'GET' else 1
    last = None
    for i in range(attempts):
        try:
            status, text = _attempt(url, body, method, use_proxy, timeout, headers)
        except (socket.timeout, ConnectionError, http.client.HTTPException, ssl.SSLError, OSError) as e:
            last = e
            if i + 1 < attempts:
                _sleep(i)
                continue
            _log_attempts(url, attempts, e)
            raise
        if 200 <= status < 300:
            return json.loads(text) if text.strip() else {}
        if _transient(status, text) and i + 1 < attempts:
            last = ApiError(status, text, url)
            _sleep(i)
            continue
        raise ApiError(status, text, url)
    if isinstance(last, Exception):
        _log_attempts(url, attempts, last)
        raise last
    raise ApiError(0, 'retries exhausted', url)


def get(path, proxy=None, timeout=DEFAULT_TIMEOUT):
    """GET on api.upstox.com. `path` starts with '/'; a full URL is also accepted."""
    url = path if path.startswith('http') else 'https://api.upstox.com' + path
    return request(url, method='GET', proxy=proxy, timeout=timeout)


def call(url, body=None, method=None, proxy=None, timeout=DEFAULT_TIMEOUT):
    """Never raises: parsed JSON, or {'status':'error','code':..,'body':..} (the shape the
    scripts' own req() helpers already returned, so their error handling is unchanged)."""
    try:
        return request(url, body=body, method=method, proxy=proxy, timeout=timeout)
    except ApiError as e:
        return {'status': 'error', 'code': e.code, 'body': (e.body or '')[:300]}
    except Exception as e:
        return {'status': 'error', 'code': 0, 'body': f'{type(e).__name__}: {str(e)[:200]}'}


# ---------------- cached master-file reads ----------------
# The instruments masters are 2-38 MB of JSON and used to be re-parsed on every lookup
# (mtf_sizing.keymap() alone cost ~300 ms per call). These helpers parse once per process
# and key the cache on (path, mtime_ns, size), so a fresh download or a roll to a new
# dated file invalidates it immediately instead of serving yesterday's map.
_file_cache = {}


def _stamp(path):
    st = os.stat(path)
    return (path, st.st_mtime_ns, st.st_size)


def file_json(path):
    """Parsed JSON for `path`, cached on (path, mtime, size)."""
    key = _stamp(path)
    hit = _file_cache.get(key)
    if hit is not None:
        return hit
    with open(path) as fh:
        val = json.load(fh)
    _file_cache[key] = val
    return val


def gzip_json(path):
    """Parsed JSON from a gzipped file, cached on (path, mtime, size)."""
    key = _stamp(path)
    hit = _file_cache.get(key)
    if hit is not None:
        return hit
    with gzip.open(path) as fh:
        val = json.load(fh)
    _file_cache[key] = val
    return val


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'token':
        t = token()
        print('token present, length', len(t))
    else:
        t0 = time.perf_counter()
        for _ in range(3):
            d = get('/v2/order/retrieve-all')
        print('3 pooled GETs %.0f ms, status=%s orders=%d' % (
            (time.perf_counter() - t0) * 1000, d.get('status'), len(d.get('data') or [])))
        # NOTE: api-hft.upstox.com serves order PLACEMENT/MODIFY only; a read path there
        # returns 404 by design, so it is not a health check. Reads all go to api.upstox.com.
        r = call('https://api.upstox.com/v2/order/retrieve-all')
        print('second host GET:', r.get('status'), len(r.get('data') or []), 'orders')

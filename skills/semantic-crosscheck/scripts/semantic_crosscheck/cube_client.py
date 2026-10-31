"""Minimal Cube REST client using only the standard library.

- HS256 JWT signed with the per-run API secret (kept in memory, passed to the Cube adapter by environment).
- /cubejs-api/v1/load is polled while Cube answers {"error": "Continue wait"}.
- Never sends renewQuery (Cube 1.8 rejects it) and always sets an explicit limit (the default is 10,000 rows).
- Accepts a /load answer only in Cube's load envelope: a JSON object whose "data" is a list of JSON objects (the
  rows). Anything else (not an object, no "data", "data" of another type, a row that is not an object) raises
  CubeResponseError instead of becoming an empty answer: zero rows read from a malformed response would pass V1 and
  V2 vacuously against empty gold rows, pass a V3 pair test of at most PAIR_KEY_CAP keys over no keys whatever the
  source data holds, and count as a V4 kill against non-empty gold rows. A body that is not JSON raises it too, so
  every malformed answer gets one category; meta() keeps the decode error, which the cube stage reports by type.
- Talks to the loopback adapter directly. urllib's default opener applies HTTP_PROXY and the system proxy settings,
  and does not bypass 127.0.0.1 unless NO_PROXY lists it, so the JWT and the query values (bound filter values from
  the database) would go to the proxy; every request therefore uses an opener without proxies.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

DEFAULT_LIMIT = 50000


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(secret: str, ttl: int = 3600) -> str:
    now = int(time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iat": now, "exp": now + ttl}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


class CubeError(Exception):
    pass


class CubeResponseError(CubeError):
    """A /load answer outside Cube's load envelope; compare.error_detail reports it as cube = invalid_response."""


class CubeClient:
    def __init__(self, base_url: str, secret: str, timeout_s: float = 120.0):
        self.base = base_url.rstrip("/")
        self.secret = secret
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", "Authorization": jwt(self.secret)})
        try:
            with self._opener.open(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise CubeError(f"HTTP {e.code}: {body}") from e

    def ready(self) -> bool:
        try:
            with self._opener.open(self.base + "/readyz", timeout=5) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def meta(self) -> dict:
        return self._req("/cubejs-api/v1/meta")

    def load(self, query: dict) -> list[dict]:
        q = dict(query)
        q.pop("renewQuery", None)
        q.setdefault("limit", DEFAULT_LIMIT)
        deadline = time.time() + self.timeout_s
        while True:
            try:
                r = self._req("/cubejs-api/v1/load", {"query": q})
            except (json.JSONDecodeError, UnicodeDecodeError) as e:   # an HTTP 200 whose body is not JSON
                raise CubeResponseError("the load response is not JSON") from e
            if not isinstance(r, dict):
                raise CubeResponseError("the load response is not a JSON object")
            if r.get("error") == "Continue wait":
                if time.time() > deadline:
                    raise CubeError("timed out waiting for Cube")
                time.sleep(0.25)
                continue
            if "error" in r:
                raise CubeError(str(r["error"])[:500])
            data = r.get("data")
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise CubeResponseError("the load response has no data list of row objects")
            if len(data) >= q["limit"]:
                raise CubeError(f"result reached the limit ({q['limit']}); raise the limit or page the query")
            return data

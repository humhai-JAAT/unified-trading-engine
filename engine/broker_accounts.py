"""Multi-broker, multi-account registry — Angel One (fallback) + Groww (primary)
+ Dhan (optional). See Architecture.md's "Multi-broker / multi-account setup".

Each account gets its OWN `AccountRateLimiter` instances (one for quote/ranking
calls, one for candle-history calls, since brokers rate-limit these endpoint
types separately) — never a shared/global limiter. Multiple worker threads may
share one account's limiter (that's the whole point — see engine/rate_limiter.py),
but different accounts never share state with each other.

Credentials come from environment variables / Streamlit secrets, numbered per
account (ANGELONE_1_*, ANGELONE_2_*, GROWW_1_*, GROWW_2_*, DHAN_1_*...). An
account with incomplete credentials is silently skipped (not an error) — same
"fall through untouched" contract the sibling bots use for Angel One, so a
partially-configured deployment degrades gracefully instead of crashing.

IMPORTANT — not live-verified against real accounts (no broker credentials were
available while this project's code was written, 2026-08-02). Groww's exact
request/response shapes here are best-effort from official docs/SDK research,
not a live-tested integration — same caveat the sibling bots' own Angel One
client shipped with initially. Confirm field names against a real account
before trusting this in production; log and fail closed (return None) on any
parse surprise rather than crashing, exactly like the sibling clients do.
"""

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyotp
import pytz
import requests

from common.helpers import PROJECT_ROOT, get_logger
from engine.rate_limiter import AccountRateLimiter

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _jwt_exp_timestamp(token: str) -> float | None:
    """Reads the `exp` claim straight out of a JWT's payload — no signature
    verification (we don't have the signing key, and don't need to; we only
    need to know when OUR OWN just-issued token expires, not authenticate
    someone else's). Returns None on any parse failure so callers can fail
    closed (treat as "needs refresh") instead of crashing.

    Groww's access tokens do NOT expire on a rolling N-hours-from-issuance
    timer — live-tested 2026-08-09, a token minted at 00:55 IST and one that
    would be minted at any other time of day both expire at a FIXED daily
    cutoff (06:00:00 IST). Guessing a fixed TTL (e.g. "4 hours") is wrong —
    decoding the real `exp` is the only reliable way to know when to refresh."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload["exp"])
    except Exception as e:
        logger.warning(f"Could not decode JWT exp claim: {e}")
        return None

def _http_error_detail(e: "requests.exceptions.HTTPError") -> str:
    """requests' raise_for_status() raises BEFORE the response body is ever
    read, so the generic '403 Client Error: Forbidden for url: ...' message
    is all that normally reaches the logs/DB warnings - Angel One's own
    error body (which usually has a real message/errorcode explaining WHY,
    e.g. a specific permission or session error) gets silently discarded.
    2026-08-18: this is exactly what was missing while root-causing a batch
    of candle-fetch 403s that didn't match the account's own successful
    quote-fetch calls moments earlier - pulls the body back out so the next
    occurrence is self-diagnosing from the DB alone."""
    resp = e.response
    if resp is None:
        return str(e)
    try:
        return f"{e} | body: {resp.json()}"
    except Exception:
        return f"{e} | body: {resp.text[:300]}"


ANGELONE_QUOTE_RATE_SECONDS = 1.1     # Angel One quote API: 1 req/sec + buffer
ANGELONE_CANDLE_RATE_SECONDS = 0.4    # Angel One candle API: 3 req/sec + buffer
GROWW_QUOTE_RATE_SECONDS = 0.12       # Groww's OWN official rate-limit table (2026-08-19,
                                       # https://groww.in/trade-api/docs/curl) buckets "Market
                                       # Quote, LTP, OHLC" under "Live Data" = 10 req/sec, NOT
                                       # the ~25 req/sec this constant used to assume (that
                                       # number was never sourced from an official doc). 0.12s
                                       # (~8.3 req/sec) is a safe buffer under the real 10/sec cap.
GROWW_CANDLE_RATE_SECONDS = 0.12      # Historical candle data has NO documented rate limit at
                                       # all (checked https://groww.in/trade-api/docs/curl/backtesting
                                       # — only data-duration limits are listed, no req/sec).
                                       # Conservatively assumed to share the "Live Data" bucket's
                                       # 10 req/sec rather than guessing a higher number again.
ANGELONE_QUOTE_BATCH_SIZE = 50
GROWW_QUOTE_BATCH_SIZE = 50
ANGELONE_SESSION_TTL_SECONDS = 2 * 3600  # JWT valid 2.5h, re-login before that
GROWW_TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60  # refresh this long before the token's real exp claim


@dataclass
class QuoteResult:
    """One row of Stage 1 ranking data for a single symbol."""
    symbol: str
    last_price: float
    pct_change: float


class BrokerAccount:
    """Common interface every account type implements. `account_id` is a short
    human label (e.g. 'angelone_1', 'groww_2') used in logs/dashboard warnings —
    never a secret."""

    account_id: str
    broker: str

    def __init__(self):
        self.quote_limiter = AccountRateLimiter(self._quote_rate_seconds())
        self.candle_limiter = AccountRateLimiter(self._candle_rate_seconds())

    def _quote_rate_seconds(self) -> float:
        raise NotImplementedError

    def _candle_rate_seconds(self) -> float:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def fetch_quotes_batch(self, symbols: list[str]) -> list[QuoteResult]:
        """Returns today's %-change for as many of `symbols` as the broker could
        serve — may be a partial list on a partial failure; callers must not
        assume every requested symbol comes back."""
        raise NotImplementedError

    def fetch_candles(self, symbol: str, interval: str, period_days: int) -> "pd.DataFrame | None":
        """5-min (or other interval) OHLCV history for one symbol. None on any
        failure — never raises, matching the sibling bots' fail-closed contract."""
        raise NotImplementedError


class AngelOneAccount(BrokerAccount):
    """TOTP-based login, same mechanics as the sibling bots' angelone_client.py,
    but instance-scoped (own session cache, own rate limiters) instead of
    module-level globals — required so 2 Angel One accounts never share state."""

    BASE_URL = "https://apiconnect.angelbroking.com"
    INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    _CANDLE_INTERVAL_MAP = {
        "1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE",
        "1h": "ONE_HOUR", "1d": "ONE_DAY",
    }

    def __init__(self, account_id: str, client_code: str, password: str,
                 totp_secret: str, api_key: str):
        self.account_id = account_id
        self.broker = "angelone"
        self._client_code = client_code
        self._password = password
        self._totp_secret = totp_secret
        self._api_key = api_key
        self._jwt_token: str | None = None
        self._logged_in_at = 0.0
        self._instrument_cache_path = PROJECT_ROOT / "data" / f"angelone_instrument_master_{account_id}.csv"
        super().__init__()

    def _quote_rate_seconds(self) -> float:
        return ANGELONE_QUOTE_RATE_SECONDS

    def _candle_rate_seconds(self) -> float:
        return ANGELONE_CANDLE_RATE_SECONDS

    def is_configured(self) -> bool:
        return all([self._client_code, self._password, self._totp_secret, self._api_key])

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
        }
        if self._jwt_token:
            headers["Authorization"] = f"Bearer {self._jwt_token}"
        return headers

    def _login(self) -> bool:
        try:
            totp_code = pyotp.TOTP(self._totp_secret).now()
            resp = requests.post(
                f"{self.BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword",
                headers=self._headers(),
                json={"clientcode": self._client_code, "password": self._password, "totp": totp_code},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("status"):
                logger.warning(f"[{self.account_id}] Angel One login failed: {payload.get('message')}")
                return False
            self._jwt_token = payload["data"]["jwtToken"]
            self._logged_in_at = time.time()
            return True
        except requests.exceptions.HTTPError as e:
            logger.warning(f"[{self.account_id}] Angel One login request failed: {_http_error_detail(e)}")
            return False
        except Exception as e:
            logger.warning(f"[{self.account_id}] Angel One login request failed: {e}")
            return False

    def _ensure_session(self) -> bool:
        if self._jwt_token is None or time.time() - self._logged_in_at > ANGELONE_SESSION_TTL_SECONDS:
            return self._login()
        return True

    def _load_symbol_to_token(self, force_refresh: bool = False) -> dict:
        if not force_refresh and self._instrument_cache_path.exists():
            age_days = (time.time() - self._instrument_cache_path.stat().st_mtime) / 86400
            if age_days < 7:
                df = pd.read_csv(self._instrument_cache_path)
                return dict(zip(df["name"], df["token"]))

        resp = requests.get(self.INSTRUMENT_MASTER_URL, timeout=60)
        resp.raise_for_status()
        full = pd.DataFrame(resp.json())
        eq = full[
            (full["exch_seg"] == "NSE")
            & (full["instrumenttype"] == "")
            & full["symbol"].astype(str).str.endswith("-EQ", na=False)
        ]
        eq = eq[["name", "token"]].dropna().drop_duplicates(subset="name")
        self._instrument_cache_path.parent.mkdir(parents=True, exist_ok=True)
        eq.to_csv(self._instrument_cache_path, index=False)
        return dict(zip(eq["name"], eq["token"]))

    def fetch_quotes_batch(self, symbols: list[str]) -> list[QuoteResult]:
        if not self.is_configured() or not self._ensure_session():
            return []
        try:
            symbol_map = self._load_symbol_to_token()
        except Exception as e:
            logger.warning(f"[{self.account_id}] instrument master fetch failed: {e}")
            return []

        token_to_symbol = {str(tok): sym for sym, tok in symbol_map.items() if sym in symbols}
        tokens = list(token_to_symbol.keys())
        results: list[QuoteResult] = []

        for i in range(0, len(tokens), ANGELONE_QUOTE_BATCH_SIZE):
            batch = tokens[i:i + ANGELONE_QUOTE_BATCH_SIZE]

            def _do_request():
                return requests.post(
                    f"{self.BASE_URL}/rest/secure/angelbroking/market/v1/quote",
                    headers=self._headers(),
                    json={"mode": "FULL", "exchangeTokens": {"NSE": batch}}, timeout=15,
                )

            try:
                resp = self.quote_limiter.call(_do_request)
                resp.raise_for_status()
                payload = resp.json()
                if not payload.get("status"):
                    logger.warning(f"[{self.account_id}] quote batch failed: {payload.get('message')}")
                    continue
                for item in payload["data"]["fetched"]:
                    symbol = token_to_symbol.get(str(item.get("symbolToken", "")))
                    if symbol is None:
                        continue
                    ltp = item.get("ltp")
                    close = item.get("close")
                    if ltp is None or not close:
                        continue
                    pct_change = (ltp - close) / close * 100
                    results.append(QuoteResult(symbol=symbol, last_price=float(ltp), pct_change=float(pct_change)))
            except requests.exceptions.HTTPError as e:
                logger.warning(f"[{self.account_id}] quote batch request failed: {_http_error_detail(e)}")
                continue
            except Exception as e:
                logger.warning(f"[{self.account_id}] quote batch request failed: {e}")
                continue

        return results

    def fetch_candles(self, symbol: str, interval: str, period_days: int) -> "pd.DataFrame | None":
        if not self.is_configured() or not self._ensure_session():
            return None
        angel_interval = self._CANDLE_INTERVAL_MAP.get(interval)
        if angel_interval is None:
            return None
        try:
            symbol_map = self._load_symbol_to_token()
        except Exception as e:
            logger.warning(f"[{self.account_id}] instrument master fetch failed: {e}")
            return None
        token = symbol_map.get(symbol)
        if token is None:
            return None

        now = pd.Timestamp.now(tz=IST)
        from_dt = now - pd.Timedelta(days=period_days)

        def _do_request():
            return requests.post(
                f"{self.BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData",
                headers=self._headers(),
                json={
                    "exchange": "NSE", "symboltoken": str(token), "interval": angel_interval,
                    "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                    "todate": now.strftime("%Y-%m-%d %H:%M"),
                },
                timeout=15,
            )

        try:
            resp = self.candle_limiter.call(_do_request)
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("status"):
                # A 200 OK with status:false was previously discarded silently -
                # Angel One's own message/errorcode here is the same kind of
                # detail _http_error_detail() recovers for the raise_for_status()
                # path below, just reached via a different route (2026-08-18).
                logger.warning(f"[{self.account_id}] candle fetch for {symbol} returned status:false "
                                f"| message: {payload.get('message')} | errorcode: {payload.get('errorcode')}")
                return None
            rows = payload.get("data") or []
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime")
            df.index = df.index.tz_convert(IST) if df.index.tz is not None else df.index.tz_localize(IST)
            return df
        except requests.exceptions.HTTPError as e:
            logger.warning(f"[{self.account_id}] candle fetch failed for {symbol}: {_http_error_detail(e)}")
            return None
        except Exception as e:
            logger.warning(f"[{self.account_id}] candle fetch failed for {symbol}: {e}")
            return None


class GrowwAccount(BrokerAccount):
    """Groww Trade API — primary broker for both stages. Uses the `growwapi`
    Python SDK's documented methods (`get_ohlc`/`get_ltp` for Stage 1 ranking,
    batched up to 50 symbols/call; `get_historical_candles` for Stage 2, 1
    symbol/call — see Architecture.md's Stage 2 note on why this can't batch).

    Fully live-verified 2026-08-09 against a real account with the paid
    Market/Live Data API subscription active — quotes, candles, and the
    `GrowwFeed` websocket (live LTP) all confirmed working. See memory.md's
    2026-08-09 entries for the full bug-fixing history. The growwapi import
    is done lazily so this project doesn't hard-fail at import time for
    accounts that aren't configured/installed yet.

    TWO auth flows supported, mirroring Groww's own two API-key types
    (confirmed via Groww's official docs, 2026-08-10 — see memory.md):
    - TOTP-based (`totp_secret` set): `pyotp`-generated fresh code each call,
      same fully-automated pattern AngelOneAccount already uses. Groww's own
      docs describe this key type as "No Expiry" — no daily manual step.
      **Preferred** — used whenever `totp_secret` is provided.
    - Approval/secret-based (`api_secret` set, `totp_secret` unset): requires
      a human to manually re-approve the app in the Groww console once per
      day — kept only for backward compatibility with an already-configured
      approval-type key."""

    def __init__(self, account_id: str, api_key: str, api_secret: str | None = None,
                 totp_secret: str | None = None):
        self.account_id = account_id
        self.broker = "groww"
        self._api_key = api_key
        self._api_secret = api_secret
        self._totp_secret = totp_secret
        self._client = None
        self._client_expires_at = 0.0  # real `exp` claim from the token, not a guessed TTL
        # 2026-08-18: WORKERS_PER_ACCOUNT=3 (stage1_ranking.py) means up to 3
        # threads can call _get_client() concurrently on every cold cache -
        # without serializing them, they'd all race to call get_access_token()
        # with the SAME TOTP code at once. Live-confirmed: adding a 2nd Groww
        # account doubled the concurrent worker count and both accounts
        # started failing with "400 Bad Request", 6 times in the same second
        # (3 workers x 2 accounts) - looks exactly like Groww's server
        # rejecting simultaneous/duplicate auth requests for one account.
        self._client_lock = threading.Lock()
        self._instrument_cache_path = PROJECT_ROOT / "data" / f"groww_instrument_master_{account_id}.csv"
        self._token_cache_path = PROJECT_ROOT / "data" / f"groww_token_cache_{account_id}.json"
        super().__init__()

    def _quote_rate_seconds(self) -> float:
        return GROWW_QUOTE_RATE_SECONDS

    def _candle_rate_seconds(self) -> float:
        return GROWW_CANDLE_RATE_SECONDS

    def is_configured(self) -> bool:
        return bool(self._api_key and (self._totp_secret or self._api_secret))

    def _get_client(self):
        if self._client is not None and time.time() < self._client_expires_at - GROWW_TOKEN_REFRESH_BUFFER_SECONDS:
            return self._client
        if not self.is_configured():
            return None

        # Serialize everything below - see __init__'s comment on why. Only
        # the first thread through actually hits the network; the rest
        # block here, then hit the fast-path re-check below and return the
        # client that thread just obtained instead of racing their own
        # get_access_token() call against it.
        with self._client_lock:
            if self._client is not None and time.time() < self._client_expires_at - GROWW_TOKEN_REFRESH_BUFFER_SECONDS:
                return self._client

            try:
                from growwapi import GrowwAPI  # lazy import — only needed if configured
            except Exception as e:
                logger.warning(f"[{self.account_id}] Groww client init failed: {e}")
                return None

            # Second-level cache: a token another process persisted to disk (this
            # SAME data/ dir every process on this machine/container shares) may
            # still be valid — reuse it before calling get_access_token() again.
            # Live-hit 2026-08-11: get_access_token() has its OWN rate limit,
            # separate from and stricter than the regular market-data rate limit
            # (GROWW_QUOTE_RATE_SECONDS etc.) — repeated short-lived script
            # invocations during a debugging session (each with an empty
            # in-memory cache) exhausted it and broke the deployed Cloud app's
            # OWN candle fetches too, since they share one account's budget.
            cached = self._load_cached_token()
            if cached is not None:
                token, exp = cached
                if time.time() < exp - GROWW_TOKEN_REFRESH_BUFFER_SECONDS:
                    try:
                        self._client = GrowwAPI(token)
                        self._client_expires_at = exp
                        return self._client
                    except Exception as e:
                        logger.warning(f"[{self.account_id}] Groww client init from cached token failed: {e}")

            try:
                # GrowwAPI takes a single session token, not (api_key, api_secret) directly —
                # exchange the api_key for a token first (live-verified 2026-08-09; the
                # SDK's real constructor signature is `GrowwAPI(token)`). TOTP preferred
                # over approval/secret — see class docstring.
                if self._totp_secret:
                    import pyotp
                    totp_code = pyotp.TOTP(self._totp_secret).now()
                    token = GrowwAPI.get_access_token(self._api_key, totp=totp_code)
                else:
                    token = GrowwAPI.get_access_token(self._api_key, secret=self._api_secret)
                self._client = GrowwAPI(token)
                # Groww's token expiry is a FIXED daily wall-clock cutoff (06:00:00 IST),
                # not N-hours-from-issuance — decode the token's own `exp` rather than
                # guessing a TTL (live-verified 2026-08-09, see _jwt_exp_timestamp's
                # docstring). Falls back to a conservative 1-hour assumption if the
                # token can't be decoded, so a parse surprise degrades, not crashes.
                exp = _jwt_exp_timestamp(token)
                self._client_expires_at = exp if exp is not None else time.time() + 3600
                self._save_cached_token(token, self._client_expires_at)
            except Exception as e:
                logger.warning(f"[{self.account_id}] Groww client init failed: {e}")
                return None
            return self._client

    def _load_cached_token(self) -> "tuple[str, float] | None":
        try:
            if not self._token_cache_path.exists():
                return None
            data = json.loads(self._token_cache_path.read_text())
            return data["token"], float(data["expires_at"])
        except Exception:
            return None

    def _save_cached_token(self, token: str, expires_at: float) -> None:
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_path.write_text(json.dumps({"token": token, "expires_at": expires_at}))
        except Exception as e:
            logger.warning(f"[{self.account_id}] could not persist Groww token cache: {e}")

    def get_client(self):
        """Public accessor for the authenticated `GrowwAPI` instance — used by
        `engine.live_feed` to build a `GrowwFeed` off the SAME session/token
        this account already manages, instead of duplicating the auth flow."""
        return self._get_client()

    def _load_symbol_to_token(self, force_refresh: bool = False) -> dict:
        """`{trading_symbol: exchange_token}` for NSE/CASH equities — needed for
        `GrowwFeed`'s subscribe calls, which take `exchange_token`, not a symbol
        name. Same 7-day file-cache pattern as `AngelOneAccount._load_symbol_to_token`
        (Groww's own full instrument master is ~137k rows — too large to
        refetch every call)."""
        if not force_refresh and self._instrument_cache_path.exists():
            age_days = (time.time() - self._instrument_cache_path.stat().st_mtime) / 86400
            if age_days < 7:
                df = pd.read_csv(self._instrument_cache_path)
                return dict(zip(df["trading_symbol"], df["exchange_token"].astype(str)))

        client = self._get_client()
        if client is None:
            return {}
        full = client.get_all_instruments()
        eq = full[(full["exchange"] == "NSE") & (full["segment"] == "CASH")]
        eq = eq[["trading_symbol", "exchange_token"]].dropna().drop_duplicates(subset="trading_symbol")
        self._instrument_cache_path.parent.mkdir(parents=True, exist_ok=True)
        eq.to_csv(self._instrument_cache_path, index=False)
        return dict(zip(eq["trading_symbol"], eq["exchange_token"].astype(str)))

    def fetch_quotes_batch(self, symbols: list[str]) -> list[QuoteResult]:
        client = self._get_client()
        if client is None:
            return []
        results: list[QuoteResult] = []
        for i in range(0, len(symbols), GROWW_QUOTE_BATCH_SIZE):
            batch = symbols[i:i + GROWW_QUOTE_BATCH_SIZE]

            def _do_request():
                return client.get_ohlc(
                    segment="CASH",
                    exchange_trading_symbols=tuple(f"NSE_{s}" for s in batch),
                )

            try:
                payload = self.quote_limiter.call(_do_request)
                for key, ohlc in (payload or {}).items():
                    symbol = key.replace("NSE_", "", 1)
                    open_price = ohlc.get("open")
                    close_price = ohlc.get("close")
                    if not open_price or close_price is None:
                        continue
                    pct_change = (close_price - open_price) / open_price * 100
                    results.append(QuoteResult(symbol=symbol, last_price=float(close_price),
                                                pct_change=float(pct_change)))
            except Exception as e:
                logger.warning(f"[{self.account_id}] Groww OHLC batch failed: {e}")
                continue
        return results

    def fetch_candles(self, symbol: str, interval: str, period_days: int) -> "pd.DataFrame | None":
        client = self._get_client()
        if client is None:
            return None
        now = pd.Timestamp.now(tz=IST)
        from_dt = now - pd.Timedelta(days=period_days)
        # SDK wants a string like "5minute"/"1hour"/"1day" (see GrowwAPI.CANDLE_INTERVAL_*),
        # not a bare integer — live-verified 2026-08-09, the old int-minutes value produced
        # "Not able to recognize candle_interval, having value 5".
        groww_interval = {"1m": "1minute", "5m": "5minute", "15m": "15minute",
                           "1h": "1hour", "1d": "1day"}.get(interval)
        if groww_interval is None:
            return None

        def _do_request():
            # groww_symbol wants "Exchange-TradingSymbol" (hyphen) — a DIFFERENT format
            # than get_ohlc's exchange_trading_symbols ("NSE_SYMBOL", underscore) above;
            # live-verified 2026-08-09, mixing them up raises a format-validation error.
            return client.get_historical_candles(
                exchange="NSE", segment="CASH", groww_symbol=f"NSE-{symbol}",
                start_time=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=groww_interval,
            )

        try:
            payload = self.candle_limiter.call(_do_request)
            candles = (payload or {}).get("candles")
            if not candles:
                return None
            # Live-verified 2026-08-09: each row is actually 7 fields (an extra
            # trailing field, always None in testing — not documented, dropped
            # here) and Datetime is an ISO string already in IST wall-clock time,
            # not a unix-seconds epoch the old code assumed.
            df = pd.DataFrame(candles, columns=["Datetime", "Open", "High", "Low", "Close", "Volume", "_unused"])
            df = df.drop(columns=["_unused"])
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime")
            df.index = df.index.tz_localize(IST) if df.index.tz is None else df.index.tz_convert(IST)
            return df
        except Exception as e:
            logger.warning(f"[{self.account_id}] Groww candle fetch failed for {symbol}: {e}")
            return None


def _env(prefix: str, suffix: str) -> str | None:
    return os.environ.get(f"{prefix}_{suffix}")


def get_configured_accounts() -> dict[str, list[BrokerAccount]]:
    """Returns {'angelone': [...], 'groww': [...]} — only accounts with complete
    credentials are included. Numbered ANGELONE_1_*/ANGELONE_2_*/GROWW_1_*/
    GROWW_2_*/GROWW_3_* env vars (or Streamlit secrets, which app.py loads into
    os.environ the same way the sibling bots do for DATABASE_URL/ANGELONE_*).
    GROWW_3 added 2026-08-18 while evaluating 2 additional Groww accounts
    alongside the existing production one — still under evaluation, see
    memory.md, not yet confirmed as a permanent 3-account setup."""
    angelone_accounts = []
    for n in (1, 2):
        prefix = f"ANGELONE_{n}"
        acct = AngelOneAccount(
            account_id=f"angelone_{n}",
            client_code=_env(prefix, "CLIENT_CODE"),
            password=_env(prefix, "PASSWORD"),
            totp_secret=_env(prefix, "TOTP_SECRET"),
            api_key=_env(prefix, "API_KEY"),
        )
        if acct.is_configured():
            angelone_accounts.append(acct)

    groww_accounts = []
    for n in (1, 2, 3):
        prefix = f"GROWW_{n}"
        acct = GrowwAccount(
            account_id=f"groww_{n}",
            api_key=_env(prefix, "API_KEY"),
            api_secret=_env(prefix, "API_SECRET"),
            totp_secret=_env(prefix, "TOTP_SECRET"),
        )
        if acct.is_configured():
            groww_accounts.append(acct)

    return {"angelone": angelone_accounts, "groww": groww_accounts}

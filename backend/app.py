"""Legacy local Upstox OAuth/market-data proof of concept (port 8000).

This service is deliberately separate from the assistant API. Keep token
handling server-side and preserve the callback state check when editing OAuth.
It exposes read-only market data; it does not place broker orders.
"""
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv, set_key
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse


# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE, override=True)

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("UPSTOX_API_KEY", "").strip()
API_SECRET = os.getenv("UPSTOX_API_SECRET", "").strip()

REDIRECT_URI = os.getenv(
    "UPSTOX_REDIRECT_URI",
    "http://127.0.0.1:8000/auth/callback",
).strip()

ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

INSTRUMENT_KEY = os.getenv(
    "UPSTOX_INSTRUMENT_KEY",
    "",
).strip()

CANDLE_INTERVAL_MINUTES = int(
    os.getenv("CANDLE_INTERVAL_MINUTES", "5")
)

AUTHORIZE_URL = (
    "https://api.upstox.com/v2/login/authorization/dialog"
)

TOKEN_URL = (
    "https://api.upstox.com/v2/login/authorization/token"
)

UPSTOX_BASE_URL = "https://api.upstox.com"


# ============================================================
# FastAPI application
# ============================================================

app = FastAPI(
    title="MCX Crude AI - Upstox POC",
    description=(
        "Local proof of concept for Upstox authentication "
        "and MCX Crude Oil market-data retrieval."
    ),
    version="0.3.0",
)

# For a single-user local POC only.
# A pending login is invalidated when the application restarts.
pending_oauth_state: str | None = None


# ============================================================
# Upstox API client
# ============================================================

class UpstoxClient:
    """Small read-only HTTP client for the preserved Upstox proof of concept."""

    def __init__(self, access_token: str):
        """Create a private HTTP session carrying the supplied bearer token."""
        if not access_token:
            raise ValueError(
                "No Upstox access token is configured."
            )

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })

    def get_json(
        self,
        path: str,
        params: dict | None = None,
    ) -> dict:
        """
        Execute a GET request and validate the Upstox response.
        """

        url = f"{UPSTOX_BASE_URL}{path}"

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=20,
            )

        except requests.Timeout as exc:
            raise RuntimeError(
                "Upstox request timed out. Try again later."
            ) from exc

        except requests.RequestException as exc:
            raise RuntimeError(
                f"Unable to connect to Upstox: {exc}"
            ) from exc

        if response.status_code == 401:
            raise RuntimeError(
                "Upstox rejected the access token. "
                "Authenticate again at /auth/login."
            )

        if response.status_code == 403:
            raise RuntimeError(
                "Upstox denied the request. "
                "Check application permissions and API access."
            )

        if response.status_code == 429:
            raise RuntimeError(
                "Upstox rate limit reached. "
                "Wait before retrying."
            )

        if not response.ok:
            raise RuntimeError(
                f"Upstox returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()

        except ValueError as exc:
            raise RuntimeError(
                "Upstox returned an invalid JSON response."
            ) from exc

        if payload.get("status") != "success":
            raise RuntimeError(
                f"Upstox API reported an unsuccessful response: "
                f"{payload}"
            )

        return payload

    # --------------------------------------------------------
    # Current market quote
    # --------------------------------------------------------

    def get_quote(
        self,
        instrument_key: str,
    ) -> dict:
        """
        Retrieve the current market quote for one instrument.
        """

        payload = self.get_json(
            "/v3/market-quote/quotes",
            params={
                "instrument_key": instrument_key,
            },
        )

        data = payload.get("data", {})

        if not data:
            raise RuntimeError(
                "Upstox returned no quote for the requested "
                "instrument. Verify the instrument key."
            )

        # The response dictionary key may be formatted
        # differently from the requested instrument key.
        quote_data = next(iter(data.values()))

        return {
            "last_price": quote_data.get("last_price"),
            "instrument_token": quote_data.get(
                "instrument_token"
            ),
            "symbol": quote_data.get("symbol"),
            "timestamp": quote_data.get("timestamp"),
            "last_trade_time": quote_data.get(
                "last_trade_time"
            ),
            "volume": quote_data.get("volume"),
            "average_price": quote_data.get("average_price"),
            "ohlc": quote_data.get("ohlc"),
            "source": "Upstox",
            "retrieved_at": datetime.now(IST).isoformat(),
        }

    # --------------------------------------------------------
    # Current-day intraday candles
    # --------------------------------------------------------

    def get_intraday_candles(
        self,
        instrument_key: str,
        interval: int,
    ) -> list[dict]:
        """
        Retrieve candles for the current trading day.

        Uses Upstox Historical Candle Data V3's intraday
        endpoint, which does not require from/to dates.
        """

        if not 1 <= interval <= 300:
            raise ValueError(
                "Candle interval must be between 1 and 300 minutes."
            )

        encoded_key = quote(
            instrument_key,
            safe="",
        )

        path = (
            f"/v3/historical-candle/intraday/"
            f"{encoded_key}/minutes/{interval}"
        )

        payload = self.get_json(path)

        raw_candles = (
            payload.get("data", {}).get("candles", [])
        )

        return self._parse_candles(raw_candles)

    # --------------------------------------------------------
    # Historical candles
    # --------------------------------------------------------

    def get_candles(
        self,
        instrument_key: str,
        interval: int,
        from_date: date,
        to_date: date,
    ) -> list[dict]:
        """
        Retrieve historical candles for a date range.

        Upstox V3 historical endpoint uses the following order:
        instrument key / minutes / interval / to date / from date
        """

        if not 1 <= interval <= 300:
            raise ValueError(
                "Candle interval must be between 1 and 300 minutes."
            )

        if from_date > to_date:
            raise ValueError(
                "from_date must not be later than to_date."
            )

        encoded_key = quote(
            instrument_key,
            safe="",
        )

        path = (
            f"/v3/historical-candle/"
            f"{encoded_key}/minutes/{interval}/"
            f"{to_date.isoformat()}/"
            f"{from_date.isoformat()}"
        )

        payload = self.get_json(path)

        raw_candles = (
            payload.get("data", {}).get("candles", [])
        )

        return self._parse_candles(raw_candles)

    # --------------------------------------------------------
    # Candle parsing and basic validation
    # --------------------------------------------------------

    @staticmethod
    def _parse_candles(
        raw_candles: list,
    ) -> list[dict]:
        """
        Convert Upstox candle arrays into structured dictionaries.

        Expected format:
        [
            timestamp,
            open,
            high,
            low,
            close,
            volume,
            open_interest (optional)
        ]
        """

        candles = []

        for row in raw_candles:
            if not isinstance(row, (list, tuple)):
                continue

            if len(row) < 6:
                continue

            try:
                candle = {
                    "timestamp": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5]),
                    "open_interest": (
                        row[6]
                        if len(row) > 6
                        else None
                    ),
                }

            except (TypeError, ValueError):
                continue

            # Basic OHLC integrity checks.
            if candle["high"] < candle["low"]:
                raise RuntimeError(
                    "Invalid candle: high is below low."
                )

            if not (
                candle["low"]
                <= candle["open"]
                <= candle["high"]
            ):
                raise RuntimeError(
                    "Invalid candle: open is outside "
                    "the high/low range."
                )

            if not (
                candle["low"]
                <= candle["close"]
                <= candle["high"]
            ):
                raise RuntimeError(
                    "Invalid candle: close is outside "
                    "the high/low range."
                )

            candles.append(candle)

        # Normalise ordering because candle APIs may return
        # the latest candle first.
        candles.sort(
            key=lambda candle: candle["timestamp"]
        )

        # Remove duplicate timestamps if present.
        unique_candles = []
        seen_timestamps = set()

        for candle in candles:
            timestamp = candle["timestamp"]

            if timestamp in seen_timestamps:
                continue

            seen_timestamps.add(timestamp)
            unique_candles.append(candle)

        return unique_candles


# ============================================================
# Shared helpers
# ============================================================

def get_client() -> UpstoxClient:
    """
    Build a client using the latest token held by the application.
    """

    token = ACCESS_TOKEN or os.getenv(
        "UPSTOX_ACCESS_TOKEN",
        "",
    ).strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "No Upstox access token is configured. "
                "Open /auth/login to authenticate."
            ),
        )

    return UpstoxClient(token)


def get_instrument_key() -> str:
    """
    Read the configured instrument key.
    """

    key = INSTRUMENT_KEY or os.getenv(
        "UPSTOX_INSTRUMENT_KEY",
        "",
    ).strip()

    if not key:
        raise HTTPException(
            status_code=400,
            detail=(
                "UPSTOX_INSTRUMENT_KEY is missing from .env."
            ),
        )

    return key


def get_today_ist() -> date:
    """
    Get today's date in Indian Standard Time.
    """

    return datetime.now(IST).date()


def handle_market_data_error(
    exc: Exception,
) -> HTTPException:
    """
    Convert connector errors into HTTP errors.
    """

    return HTTPException(
        status_code=502,
        detail=str(exc),
    )


# ============================================================
# Health endpoint
# ============================================================

@app.get("/")
def root():
    """Expose a small liveness response without returning configuration secrets."""
    return {
        "application": "MCX Crude AI - Upstox POC",
        "version": "0.3.0",
        "status": "running",
        "trading_enabled": False,
        "timestamp": datetime.now(IST).isoformat(),
    }


# ============================================================
# OAuth login
# ============================================================

@app.get("/auth/login")
def auth_login():
    """
    Redirect the user to Upstox OAuth authentication.
    """

    global pending_oauth_state

    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "Configure UPSTOX_API_KEY and "
                "UPSTOX_API_SECRET in .env."
            ),
        )

    pending_oauth_state = secrets.token_urlsafe(32)

    query = urlencode({
        "client_id": API_KEY,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": pending_oauth_state,
    })

    authorization_url = (
        f"{AUTHORIZE_URL}?{query}"
    )

    return RedirectResponse(
        url=authorization_url,
        status_code=302,
    )


# ============================================================
# OAuth callback
# ============================================================

@app.get("/auth/callback")
def auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """
    Exchange the authorisation code for an access token.

    The token is saved locally in backend/.env.
    """

    global ACCESS_TOKEN
    global pending_oauth_state

    if error:
        pending_oauth_state = None

        raise HTTPException(
            status_code=400,
            detail=(
                "Upstox authentication was not completed. "
                "Start again at /auth/login."
            ),
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail=(
                "No authorisation code received. "
                "Start again at /auth/login."
            ),
        )

    # Validate OAuth state to protect against forged callbacks.
    if (
        not pending_oauth_state
        or not state
        or not secrets.compare_digest(
            state,
            pending_oauth_state,
        )
    ):
        pending_oauth_state = None

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid OAuth state. "
                "Restart authentication at /auth/login."
            ),
        )

    # Authorisation codes are single-use.
    pending_oauth_state = None

    form_data = {
        "code": code,
        "client_id": API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(
            TOKEN_URL,
            data=form_data,
            headers={
                "Accept": "application/json",
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
            },
            timeout=20,
        )

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not connect to the Upstox "
                "token endpoint."
            ),
        ) from exc

    if not response.ok:
        # Do not return request payloads or secrets.
        raise HTTPException(
            status_code=502,
            detail=(
                f"Upstox token exchange failed with HTTP "
                f"{response.status_code}. Check credentials, "
                "redirect URI and authorisation code."
            ),
        )

    try:
        token_response = response.json()

    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Upstox returned an invalid token response."
            ),
        ) from exc

    new_token = token_response.get("access_token")

    if not new_token:
        raise HTTPException(
            status_code=502,
            detail=(
                "Upstox token response did not contain "
                "an access token."
            ),
        )

    try:
        # Persist the token in the local environment file.
        set_key(
            str(ENV_FILE),
            "UPSTOX_ACCESS_TOKEN",
            new_token,
        )

    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not save the access token to .env. "
                "Check file permissions."
            ),
        ) from exc

    ACCESS_TOKEN = new_token
    os.environ["UPSTOX_ACCESS_TOKEN"] = new_token

    return {
        "status": "authenticated",
        "message": (
            "Upstox authentication succeeded. "
            "The token was saved locally."
        ),
        "expires_at": token_response.get("expires_at"),
        "next_step": "/api/market/quote",
        "trading_enabled": False,
    }


# ============================================================
# Safe configuration endpoint
# ============================================================

@app.get("/api/market/config")
def market_config():
    """
    Return configuration status without revealing secrets.
    """

    return {
        "api_key_configured": bool(API_KEY),
        "api_secret_configured": bool(API_SECRET),
        "access_token_configured": bool(
            ACCESS_TOKEN
            or os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        ),
        "instrument_key": get_instrument_key(),
        "candle_interval_minutes": (
            CANDLE_INTERVAL_MINUTES
        ),
        "timezone": "Asia/Kolkata",
        "trading_enabled": False,
    }


# ============================================================
# Current market quote endpoint
# ============================================================

@app.get("/api/market/quote")
def market_quote():
    """
    Retrieve the latest available quote from Upstox.
    """

    client = get_client()
    instrument_key = get_instrument_key()

    try:
        quote_data = client.get_quote(
            instrument_key
        )

        return {
            "instrument_key_requested": instrument_key,
            "quote": quote_data,
        }

    except (RuntimeError, ValueError) as exc:
        raise handle_market_data_error(exc) from exc


# ============================================================
# Candle endpoint
# ============================================================

@app.get("/api/market/candles")
def market_candles(
    interval: int = Query(
        default=CANDLE_INTERVAL_MINUTES,
        ge=1,
        le=300,
    ),
    days: int = Query(
        default=1,
        ge=1,
        le=30,
    ),
):
    """
    Retrieve intraday or historical candles.

    days=1:
        Use the current-day intraday endpoint.

    days>1:
        Use the historical candle endpoint for the requested
        number of calendar days, ending today in IST.

    An empty result is returned as an empty list if Upstox
    successfully returns no candles. API failures raise errors.
    """

    client = get_client()
    instrument_key = get_instrument_key()
    today = get_today_ist()

    try:
        if days == 1:
            # FIX: use the intraday endpoint for today's candles.
            candles = client.get_intraday_candles(
                instrument_key=instrument_key,
                interval=interval,
            )

            data_mode = "intraday"

            from_date = today
            to_date = today

        else:
            # Use the historical endpoint for a date range.
            from_date = today - timedelta(
                days=days - 1
            )

            to_date = today

            candles = client.get_candles(
                instrument_key=instrument_key,
                interval=interval,
                from_date=from_date,
                to_date=to_date,
            )

            data_mode = "historical"

        latest_timestamp = (
            candles[-1]["timestamp"]
            if candles
            else None
        )

        return {
            "instrument_key": instrument_key,
            "interval_minutes": interval,
            "requested_days": days,
            "data_mode": data_mode,
            "requested_from": from_date.isoformat(),
            "requested_to": to_date.isoformat(),
            "count": len(candles),
            "latest_candle_timestamp": latest_timestamp,
            "retrieved_at": datetime.now(IST).isoformat(),
            "candles": candles,
        }

    except (RuntimeError, ValueError) as exc:
        raise handle_market_data_error(exc) from exc


# ============================================================
# Application startup
# ============================================================

@app.on_event("startup")
def startup_event():
    """
    Log safe startup information.
    Never log API secrets or access tokens.
    """

    print("=" * 60)
    print("MCX Crude AI - Upstox POC")
    print("=" * 60)
    print(f"API key configured : {bool(API_KEY)}")
    print(f"Access token set   : {bool(ACCESS_TOKEN)}")
    print(f"Instrument key     : {INSTRUMENT_KEY or 'NOT CONFIGURED'}")
    print(f"Candle interval    : {CANDLE_INTERVAL_MINUTES} minutes")
    print("Trading enabled    : False")
    print("=" * 60)

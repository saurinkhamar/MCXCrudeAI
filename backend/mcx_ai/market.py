"""Read-only Upstox quote and candle adapter used by the local assistant.

Keep authentication headers server-side. Convert provider failures into the
stable MarketError categories consumed by the API status endpoints.
"""
from datetime import datetime, timedelta
from urllib.parse import quote
import requests
from zoneinfo import ZoneInfo
from .config import settings

IST=ZoneInfo('Asia/Kolkata'); BASE='https://api.upstox.com'
class MarketError(RuntimeError):
    """Base provider/data error safe to translate into an API state."""

class MarketAuthError(MarketError):
    """Authentication failure distinguished from market/data outages."""

def _get(path, params=None):
    """Perform one authenticated GET and validate the Upstox envelope."""
    if not settings.upstox_access_token: raise MarketAuthError('TOKEN_INVALID: No Upstox access token configured.')
    try: response=requests.get(BASE+path,params=params,headers={'Accept':'application/json','Authorization':f'Bearer {settings.upstox_access_token}'},timeout=20)
    except requests.RequestException as exc: raise MarketError('DATA_UNAVAILABLE: Upstox could not be reached.') from exc
    if response.status_code == 401: raise MarketAuthError('TOKEN_EXPIRED: Upstox rejected the access token.')
    if response.status_code == 403: raise MarketAuthError('TOKEN_INVALID: Upstox denied the request.')
    if not response.ok: raise MarketError(f'DATA_UNAVAILABLE: Upstox returned HTTP {response.status_code}.')
    body=response.json()
    if body.get('status')!='success': raise MarketError('Upstox did not return a successful response.')
    return body
def quote_data(key=None):
    """Fetch a quote and mark it stale when its trade time exceeds 15 minutes."""
    key=key or settings.upstox_instrument_key; data=_get('/v3/market-quote/quotes',{'instrument_key':key}).get('data',{})
    if not data: raise MarketError('DATA_UNAVAILABLE: No quote returned; instrument key may be inactive or invalid.')
    item=next(iter(data.values())); stamp=item.get('last_trade_time') or item.get('timestamp')
    stale=True
    if stamp:
        try: stale=(datetime.now(IST)-datetime.fromtimestamp(float(stamp)/1000,IST))>timedelta(minutes=15)
        except (ValueError,TypeError,OverflowError): stale=False
    return {'instrument_key':key,'symbol':item.get('symbol'),'last_price':item.get('last_price'),'timestamp':stamp,'volume':item.get('volume'),'ohlc':item.get('ohlc'),'stale':stale,'state':'STALE_DATA' if stale else 'READY','source':'Upstox','retrieved_at':datetime.now(IST).isoformat()}
def candles(interval=5,days=1):
    """Fetch and normalize intraday or date-range candles for the configured key."""
    key=quote(settings.upstox_instrument_key,safe='')
    path=f'/v3/historical-candle/intraday/{key}/minutes/{interval}' if days==1 else f'/v3/historical-candle/{key}/minutes/{interval}/{datetime.now(IST).date()}/{(datetime.now(IST)-timedelta(days=days)).date()}'
    rows=_get(path).get('data',{}).get('candles',[]); parsed=[]
    for row in rows:
        if len(row)>=6: parsed.append({'timestamp':row[0],'open':float(row[1]),'high':float(row[2]),'low':float(row[3]),'close':float(row[4]),'volume':int(row[5]),'open_interest':row[6] if len(row)>6 else None})
    return sorted(parsed,key=lambda x:x['timestamp'])
def market_open():
    """Return the app's coarse weekday/time window in the India timezone.

    This is not an exchange holiday calendar; validate upstream data freshness
    as well before producing a signal.
    """
    now=datetime.now(IST); clock=now.time(); return now.weekday()<5 and clock.hour>=9 and clock.hour<24

"""Offline OHLCV importer and deterministic historical replay.

The strategy is intentionally independent of Upstox and Google ADK. Decision
features use only bars known at that time; execution is simulated at a later
bar and transaction costs are estimates. Keep these assumptions explicit.
"""
import csv
import re
from datetime import datetime
from io import StringIO
from .analysis import indicators

def _parse_timestamp(value: str):
    """Parse ISO timestamps and common browser-export dates with GMT offsets."""
    text=value.strip()
    try: return datetime.fromisoformat(text.replace('Z','+00:00'))
    except ValueError: pass
    # Browser exports append a display-only timezone name after the numeric offset.
    text=re.sub(r'\s*\([^)]*\)\s*$','',text)
    try: return datetime.strptime(text,'%a %b %d %Y %H:%M:%S GMT%z')
    except ValueError as exc: raise ValueError('CSV contains an unsupported timestamp format.') from exc

def _parse_number(value: str):
    """Convert a dot-decimal value and discard common thousands separators."""
    return float(value.strip().replace(',',''))

def parse_csv(text: str):
    """Validate a user CSV and return candles sorted by timestamp.

    Required headers are timestamp/open/high/low/close; volume is optional.
    Invalid rows fail the entire import so the backtest never silently skips
    malformed price data.
    """
    reader=csv.DictReader(StringIO(text))
    # Compare normalized names but retain the source column labels for row access.
    normalized={str(name).lstrip('\ufeff').strip().casefold():name for name in (reader.fieldnames or [])}
    aliases={'timestamp':('timestamp','datetime','date','time'),'open':('open',),'high':('high',),'low':('low',),'close':('close',),'volume':('volume',)}
    names={key:next((normalized[name] for name in choices if name in normalized),None) for key,choices in aliases.items()}
    if any(not names[k] for k in ('timestamp','open','high','low','close')): raise ValueError('CSV requires timestamp, open, high, low and close columns.')
    rows=[]
    for row in reader:
        # Ignore blank trailing lines that some spreadsheet exporters append.
        if not any((value or '').strip() for value in row.values() if isinstance(value,str)): continue
        try:
            stamp=_parse_timestamp(row[names['timestamp']])
            candle={'timestamp':stamp.isoformat(),'open':_parse_number(row[names['open']]),'high':_parse_number(row[names['high']]),'low':_parse_number(row[names['low']]),'close':_parse_number(row[names['close']]),'volume':int(_parse_number(row[names['volume']])) if names['volume'] and row.get(names['volume']) else 0}
            if candle['low']>candle['high'] or candle['low']>min(candle['open'],candle['close']) or candle['high']<max(candle['open'],candle['close']): raise ValueError()
            rows.append(candle)
        except (ValueError,TypeError): raise ValueError('CSV contains an invalid timestamp or OHLCV row.')
    rows.sort(key=lambda c:c['timestamp'])
    if len(rows)<31: raise ValueError('At least 31 candles are required.')
    return rows

def run_backtest(candles, *, start=None, end=None, strategy='ema_rsi', risk_inr=2000, max_stop_points=100, multiplier=10, lot_size=1, brokerage_rate=.0003, tax_rate=.0002, slippage_points=.5, force_close='23:00'):
    """Replay one deterministic strategy and return metrics with every trade.

    No-lookahead contract: indicators see rows through ``i`` and entry uses
    row ``i+1`` open. Only one trade is open at a time; exits honor stop-first
    OHLC ambiguity, configured cutoff, and date boundaries. All rate/multiplier
    inputs are assumptions that should be checked against the active contract.
    """
    if strategy!='ema_rsi': raise ValueError('Only deterministic ema_rsi strategy is supported.')
    rows=[c for c in candles if (start is None or c['timestamp'][:10]>=start) and (end is None or c['timestamp'][:10]<=end)]
    trades=[]; equity=peak=0; max_dd=0; i=30
    while i<len(rows)-1:
        hist=rows[:i+1]; values=indicators(hist)
        if not values: i+=1; continue
        side='BUY' if values['ema_fast']>values['ema_slow'] and values['rsi']>=52 else 'SELL' if values['ema_fast']<values['ema_slow'] and values['rsi']<=48 else None
        if not side: i+=1; continue
        entry_bar=rows[i+1]
        if entry_bar['timestamp'][11:16]>=force_close: i+=1; continue
        distance=min(values['atr']*1.5,max_stop_points)
        if distance<=0 or distance>max_stop_points: i+=1; continue
        unit_risk=distance*multiplier+2*slippage_points*multiplier+entry_bar['open']*2*multiplier*(brokerage_rate+tax_rate)
        qty=int(max(0,risk_inr-40)//max(unit_risk,1e-9)); qty=(qty//lot_size)*lot_size
        if qty<lot_size: i+=1; continue
        entry=entry_bar['open']+(slippage_points if side=='BUY' else -slippage_points); stop=entry-distance; target=entry+distance*2
        if side=='SELL': stop=entry+distance; target=entry-distance*2
        exit_price=None; reason='end_of_data'; exit_idx=i+1
        for j in range(i+1,len(rows)):
            bar=rows[j]; exit_idx=j
            if bar['timestamp'][:10]!=entry_bar['timestamp'][:10]:
                exit_price=rows[j-1]['close']; exit_idx=j-1; reason='session_boundary'; break
            if bar['timestamp'][11:16]>=force_close: exit_price=bar['close']; reason='session_cutoff'; break
            if side=='BUY': hit_stop=bar['low']<=stop; hit_target=bar['high']>=target
            else: hit_stop=bar['high']>=stop; hit_target=bar['low']<=target
            if hit_stop or hit_target:
                exit_price=stop if hit_stop else target; reason='stop' if hit_stop else 'target'; break
        if exit_price is None: exit_price=rows[-1]['close']
        exit_price-=slippage_points if side=='BUY' else -slippage_points
        gross=round((exit_price-entry)*(1 if side=='BUY' else -1)*qty*multiplier,2)
        turnover=(entry+exit_price)*qty*multiplier; costs=round(turnover*(brokerage_rate+tax_rate)+40,2); net=round(gross-costs,2)
        equity+=net; peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
        trades.append({'side':side,'entry_time':entry_bar['timestamp'],'exit_time':rows[exit_idx]['timestamp'],'entry':round(entry,4),'exit':round(exit_price,4),'stop':round(stop,4),'target':round(target,4),'quantity':qty,'gross_pnl':gross,'costs':costs,'net_pnl':net,'exit_reason':reason})
        i=max(exit_idx+1,i+2)
    pnls=[t['net_pnl'] for t in trades]; wins=sum(p for p in pnls if p>0); losses=-sum(p for p in pnls if p<0)
    return {'mode':'deterministic_historical_replay','strategy':strategy,'config':{'start':start,'end':end,'risk_inr':risk_inr,'max_stop_points':max_stop_points,'multiplier':multiplier,'lot_size':lot_size,'brokerage_rate':brokerage_rate,'tax_rate':tax_rate,'slippage_points':slippage_points,'force_close':force_close},'trade_count':len(trades),'net_pnl':round(sum(pnls),2),'win_rate':round(100*sum(p>0 for p in pnls)/len(pnls),2) if pnls else 0,'profit_factor':round(wins/losses,3) if losses else None,'max_drawdown':round(max_dd,2),'expectancy':round(sum(pnls)/len(pnls),2) if pnls else 0,'total_costs':round(sum(t['costs'] for t in trades),2),'trades':trades}

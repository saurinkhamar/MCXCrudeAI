"""Feature engineering and the offline deterministic signal fallback.

Keep indicator math side-effect free so live signals and historical replay can
share the same calculations. Any feature added here should also be documented
in the agent prompt/schema and tested against short histories.
"""
from statistics import mean
from .schemas import AgentReport, Side, SignalProposal

def indicators(candles):
    """Calculate the feature snapshot from chronologically ordered OHLCV bars.

    Returns ``None`` until 30 bars exist. Feature names and price units are part
    of the agent input contract; update the ADK prompts when changing them.
    """
    closes=[c['close'] for c in candles]; highs=[c['high'] for c in candles]; lows=[c['low'] for c in candles]
    if len(closes)<30: return None
    def ema_series(values,n):
        """Return the final exponentially weighted value for period ``n``."""
        alpha=2/(n+1); out=values[0]
        for value in values[1:]: out=alpha*value+(1-alpha)*out
        return out
    ema=lambda n: ema_series(closes,n); change=closes[-1]-closes[-15]; gains=[max(0,closes[i]-closes[i-1]) for i in range(-14,0)]; losses=[max(0,closes[i-1]-closes[i]) for i in range(-14,0)]; rsi=100 if not sum(losses) else 100-100/(1+sum(gains)/sum(losses))
    atr=mean([highs[i]-lows[i] for i in range(-14,0)])
    volumes=[max(0,c.get('volume',0)) for c in candles[-20:]]; typical=[(candles[-20+i]['high']+candles[-20+i]['low']+candles[-20+i]['close'])/3 for i in range(20)]; vwap=sum(p*v for p,v in zip(typical,volumes))/sum(volumes) if sum(volumes) else mean(closes[-20:])
    macd=ema_series(closes,12)-ema_series(closes,26); macd_series=[]
    for end in range(max(26,len(closes)-8),len(closes)+1):
        macd_series.append(ema_series(closes[:end],12)-ema_series(closes[:end],26))
    signal=mean(macd_series)
    tr=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(-14,0)]
    return {'price':closes[-1],'ema_fast':ema(9),'ema_slow':ema(21),'rsi':round(rsi,1),'atr':round(atr,2),'change_15':round(change,2),'support':min(lows[-20:]),'resistance':max(highs[-20:]),'vwap':round(vwap,4),'macd':round(macd,4),'macd_signal':round(signal,4),'volume':candles[-1].get('volume',0),'adx_proxy':round(abs(macd)/max(atr,1e-9)*100,2)}
def deterministic_agents(values,fundamental_sources=None):
    """Create a conservative rule-based proposal when ADK is not selected.

    This function never fetches data or enforces trade policy; ``risk.validate``
    remains the final deterministic gate for both this and ADK proposals.
    """
    if not values:
        report=AgentReport(agent='technical',conclusion='Insufficient candle data',data_status='insufficient'); return SignalProposal(side=Side.INSUFFICIENT_DATA,rationale=['At least 30 valid candles are required.'],reports=[report])
    bullish=values['ema_fast']>values['ema_slow'] and values['rsi']>=52; bearish=values['ema_fast']<values['ema_slow'] and values['rsi']<=48
    trend='Bullish trend' if bullish else 'Bearish trend' if bearish else 'No clear trend'
    momentum=f"RSI {values['rsi']}; ATR {values['atr']}"
    if fundamental_sources:
        titles=[item.get('title','Untitled report') for item in fundamental_sources[:4]]
        fundamental=AgentReport(agent='fundamental',conclusion=f'{len(fundamental_sources)} sourced report(s) are cached; deterministic mode does not infer fundamental direction.',rationale=titles,data_status='available')
    else:
        fundamental=AgentReport(agent='fundamental',conclusion='Insufficient data: no relevant current fundamental reports are cached.',data_status='insufficient')
    reports=[fundamental,AgentReport(agent='trend',conclusion=trend,rationale=[f"EMA 9 {values['ema_fast']:.2f}, EMA 21 {values['ema_slow']:.2f}"]),AgentReport(agent='momentum',conclusion=momentum)]
    side=Side.BUY if bullish else Side.SELL if bearish else Side.NO_TRADE
    price=values['price']; stop=price-values['atr']*1.5 if side==Side.BUY else price+values['atr']*1.5; target=price+values['atr']*3 if side==Side.BUY else price-values['atr']*3
    if side==Side.NO_TRADE: return SignalProposal(side=side,rationale=['Indicators are not aligned.'],reports=reports)
    return SignalProposal(side=side,entry=price,stop=round(stop,2),target=round(target,2),confidence=60,rationale=['Rule-based preliminary interpretation; confidence is not calibrated.'],reports=reports)

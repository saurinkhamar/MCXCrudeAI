"""Unit tests for feature calculations, policy checks, and historical replay."""
from mcx_ai.analysis import deterministic_agents, indicators
from mcx_ai.schemas import Side
from mcx_ai.backtest import parse_csv, run_backtest
from mcx_ai.risk import cost
from mcx_ai.schemas import SignalProposal
def test_indicators_need_history():
    """Indicator calculation should wait until its minimum lookback exists."""
    assert indicators([]) is None

def test_insufficient_signal_is_not_tradeable():
    """Missing features must produce an explicit non-tradeable proposal."""
    assert deterministic_agents(None).side == Side.INSUFFICIENT_DATA

def test_bullish_signal_shape():
    """A rising synthetic tape should keep the fallback output in its enum contract."""
    data=[{'close':100+i*.5,'high':101+i*.5,'low':99+i*.5,'volume':100} for i in range(35)]
    result=deterministic_agents(indicators(data)); assert result.side in {Side.BUY,Side.NO_TRADE}

def sample_csv(n=80):
    """Build a small deterministic rising CSV string for isolated replay tests."""
    from datetime import datetime,timedelta
    start=datetime(2025,1,1,9,0)
    lines=['timestamp,open,high,low,close,volume']
    for i in range(n):
        p=100+i*.25; lines.append(f'{(start+timedelta(minutes=5*i)).isoformat()},{p},{p+1},{p-1},{p+.5},100')
    return '\n'.join(lines)

def test_csv_validation_and_sorting():
    """CSV parsing should normalize order and reject missing required headers."""
    rows=parse_csv(sample_csv()); assert len(rows)==80 and rows[0]['open']==100
    import pytest
    with pytest.raises(ValueError): parse_csv('timestamp,open,close\n2025-01-01,1,2')

def test_training_data_template_headers_timestamps_and_grouped_volume():
    """Accept the user's Date/GMT-offset template and ignore derived columns."""
    from datetime import datetime,timezone,timedelta
    start=datetime(2026,10,23,16,10,tzinfo=timezone(timedelta(hours=5,minutes=30)))
    rows=['Date,Open,High,Low,Close,% Change,% Change vs Average,Volume']
    for i in range(35):
        stamp=(start+timedelta(minutes=5*i)).strftime('%a %b %d %Y %H:%M:%S GMT%z')+' (India Standard Time)'
        price=9000+i
        rows.append(f'"{stamp}",{price},{price+2},{price-1},{price+1},0.12,0.11,"{1000+i:,}"')
    parsed=parse_csv('\n'.join(rows))
    assert len(parsed)==35 and parsed[0]['volume']==1000
    assert parsed[0]['timestamp'].endswith('+05:30') and parsed[-1]['close']==9035

def test_backtest_is_reproducible_and_cost_aware():
    """Identical OHLCV and assumptions must yield identical costed results."""
    rows=parse_csv(sample_csv()); a=run_backtest(rows,slippage_points=.5); b=run_backtest(rows,slippage_points=.5)
    assert a==b and a['mode']=='deterministic_historical_replay'
    assert a['total_costs']>=0 and all(t['quantity']>0 for t in a['trades'])

def test_backtest_decision_enters_on_next_bar():
    """The signal candle must not be used as the simulated entry candle."""
    rows=parse_csv(sample_csv(40)); result=run_backtest(rows,slippage_points=0)
    assert result['trades']
    assert result['trades'][0]['entry_time']==rows[31]['timestamp']

def test_risk_sizing_obeys_budget_and_stop_limit():
    """Quantity should be floored to fit the configured crude-point risk budget."""
    rows=parse_csv(sample_csv()); result=run_backtest(rows,risk_inr=1000,max_stop_points=100,multiplier=10)
    assert all(t['quantity']*abs(t['entry']-t['stop'])*10 <= 1000 for t in result['trades'])

def test_backtest_next_bar_entry_and_session_close():
    """Replay should not open a position at or after the configured close time."""
    rows=parse_csv(sample_csv()); result=run_backtest(rows,force_close='09:10')
    assert all(t['entry_time']!=t['exit_time'] or t['exit_reason'] in ('stop','target') for t in result['trades'])
    assert all(t['exit_reason']=='session_cutoff' or t['exit_time'][11:16] <= '09:10' for t in result['trades'])

def test_generated_fixture_closes_trades_intraday_and_is_reproducible():
    """The checked-in synthetic sample should stay repeatable and intraday-only."""
    from pathlib import Path
    fixture=Path(__file__).resolve().parents[2]/'data'/'examples'/'synthetic_crude_5m.csv'
    rows=parse_csv(fixture.read_text(encoding='utf-8'))
    first=run_backtest(rows); second=run_backtest(rows)
    assert first==second and len(rows)==360 and first['trade_count']>0
    assert all(t['entry_time'][:10]==t['exit_time'][:10] for t in first['trades'])

def test_common_signal_schema_and_cost_estimator():
    """Proposal fields and the local paper-cost estimator should remain usable."""
    proposal=SignalProposal(side=Side.BUY,entry=100,stop=95,target=110,confidence=50)
    assert proposal.confidence==50 and proposal.side==Side.BUY
    assert cost(100,1)>0

def test_status_classifies_auth_and_data_failures(monkeypatch):
    """Status should distinguish expired credentials from provider data outages."""
    from mcx_ai import main
    monkeypatch.setattr(main,'market_open',lambda:True)
    monkeypatch.setattr(main,'quote_data',lambda: (_ for _ in ()).throw(main.MarketError('TOKEN_EXPIRED: test')))
    assert main.status()['state']=='TOKEN_EXPIRED'
    monkeypatch.setattr(main,'quote_data',lambda: (_ for _ in ()).throw(main.MarketError('DATA_UNAVAILABLE: test')))
    assert main.status()['state']=='DATA_UNAVAILABLE'

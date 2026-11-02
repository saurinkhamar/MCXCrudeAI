"""FastAPI application and HTTP orchestration for local paper research.

Route handlers coordinate services but should not implement model-controlled
risk policy. Preserve the no-broker-execution boundary when adding endpoints.
"""
import json
import csv
import importlib.util
import threading
import time
from io import StringIO
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .schemas import Approval, PaperEntry, PaperExit, SettingsUpdate, BacktestRequest, ReplayStartRequest, AgentReport, SignalProposal, Side
from .storage import audit, configured, db, init_db, now
from .market import MarketError, candles, market_open, quote_data
from .analysis import deterministic_agents, indicators
from .risk import cost, validate, validate_replay
from .backtest import parse_csv, run_backtest
from .adk_agents import adk_enabled, analyze_with_adk, safe_failure_reason
from .fundamentals import fundamental_status, get_fundamental_sources

app=FastAPI(title='MCX Crude Oil Mini AI Trading Assistant',version='1.0.0',description='Local-only paper trading assistant. It cannot place broker orders.')
IST=ZoneInfo('Asia/Kolkata')
app.add_middleware(CORSMiddleware,allow_origins=['http://localhost:5173','http://127.0.0.1:5173'],allow_methods=['*'],allow_headers=['*'])
_scheduler_stop=threading.Event()
_scheduler_thread=None
def _scheduled_analysis():
    """Create a draft on the configured cadence while the coarse session is open."""
    while not _scheduler_stop.wait(max(1,settings.scheduler_interval_minutes)*60):
        if not market_open(): continue
        try:
            result=generate()
            audit('scheduled_analysis_completed',f"signal={result['id']}; created_at={result['created_at']}")
        except Exception as exc:
            # Signal-generation errors are safe categories; keep cycles visible without logging credentials.
            detail=getattr(exc,'detail',type(exc).__name__)
            audit('scheduled_analysis_failed',str(detail)[:240])
@app.on_event('startup')
def start():
    """Initialize local storage and optionally launch the daemon scheduler."""
    global _scheduler_thread
    init_db()
    if settings.scheduler_enabled and not (_scheduler_thread and _scheduler_thread.is_alive()):
        _scheduler_stop.clear()
        _scheduler_thread=threading.Thread(target=_scheduled_analysis,daemon=True,name='mcx-analysis-scheduler')
        _scheduler_thread.start()
        audit('scheduler_started',f'interval_minutes={settings.scheduler_interval_minutes}; candle_interval_minutes={settings.candle_interval_minutes}')
@app.on_event('shutdown')
def stop():
    """Signal the scheduler thread to exit during server shutdown."""
    _scheduler_stop.set()
def fail(error):
    """Translate a market-provider failure into an API service-unavailable response."""
    raise HTTPException(503,detail=str(error))
@app.get('/api/health')
def health():
    """Return process health without requiring network credentials or market data."""
    return {'status':'ok','mode':'paper-only','broker_execution':False}
@app.get('/api/status')
def status():
    """Classify live quote readiness separately from backend process health."""
    try:
        q=quote_data(); state='STALE_DATA' if q['stale'] else ('READY' if market_open() else 'MARKET_CLOSED'); return {'state':state,'market_open':market_open(),'quote':q,'broker_execution':False}
    except MarketError as exc:
        message=str(exc); state='TOKEN_EXPIRED' if message.startswith('TOKEN_EXPIRED') else 'TOKEN_INVALID' if message.startswith('TOKEN_INVALID') else 'DATA_UNAVAILABLE'
        return {'state':state,'market_open':market_open(),'detail':message,'broker_execution':False}
@app.get('/api/agents/status')
def agent_status():
    """Report safe ADK configuration state without returning any credential."""
    try:
        selected='adk' if adk_enabled() else 'deterministic'; config_error=None
    except Exception as exc:
        selected='unavailable'; config_error=type(exc).__name__
    try: package_installed=importlib.util.find_spec('google.adk') is not None
    except (ImportError,ModuleNotFoundError): package_installed=False
    return {'agent_mode':settings.agent_mode,'analysis_selected':selected,'google_api_key_configured':bool(settings.google_api_key),'adk_package_installed':package_installed,'model':settings.google_model,'output_token_limit':settings.agent_output_tokens,'configuration_error':config_error,'scheduler_enabled':settings.scheduler_enabled,'scheduler_running':bool(_scheduler_thread and _scheduler_thread.is_alive()),'scheduler_interval_minutes':settings.scheduler_interval_minutes,'candle_interval_minutes':settings.candle_interval_minutes}
@app.get('/api/fundamentals/status')
def fundamentals_status_endpoint():
    """Return cached source count, refresh timing, and safe RSS report metadata."""
    return fundamental_status()
@app.get('/api/market/quote')
def quote():
    """Return quote, freshness, session state, and the instrument-validation reminder."""
    try:
        data=quote_data(); data['market_open']=market_open(); data['state']='STALE_DATA' if data['stale'] else 'READY' if data['market_open'] else 'MARKET_CLOSED'; data['validation']='UNVERIFIED: confirm this key maps to active MCX Crude Oil Mini expiry before relying on it.'; return data
    except MarketError as exc:
        if not market_open(): return {'state':'MARKET_CLOSED','market_open':False,'detail':'Live quote is unavailable outside the session; use historical candles or CSV backtest.','instrument_key':settings.upstox_instrument_key}
        fail(exc)
@app.get('/api/market/candles')
def get_candles(interval:int=5,days:int=1):
    """Validate query bounds, then return normalized Upstox OHLCV history."""
    if not 1<=interval<=300 or not 1<=days<=30: raise HTTPException(422,'interval must be 1-300; days must be 1-30')
    try: return {'interval_minutes':interval,'count':len(candles(interval,days)),'candles':candles(interval,days)}
    except MarketError as exc: fail(exc)
@app.get('/api/instrument/validate')
def instrument():
    """Show quote identity while explicitly leaving contract metadata unverified."""
    try:
        q=quote_data(); return {'instrument_key':q['instrument_key'],'symbol':q['symbol'],'last_price':q['last_price'],'verified':False,'message':'Quote retrieved. Verify contract name, lot size, and active expiry against Upstox instrument metadata before enabling analysis.'}
    except MarketError as exc: fail(exc)
@app.post('/api/signals/generate')
def generate():
    """Build one fresh-data proposal, run ADK or deterministic mode, then risk-check it.

    This is the main decision boundary: bad/stale data stops analysis, ADK output
    is schema-validated, and the Python risk engine runs before persistence.
    """
    try:
        bars=candles(settings.candle_interval_minutes,1)
        if not bars: raise MarketError('DATA_UNAVAILABLE: No candles returned.')
        if not market_open(): raise MarketError('MARKET_CLOSED: signal generation requires a current market session; use backtest when closed.')
        try: candle_at=datetime.fromisoformat(bars[-1]['timestamp'].replace('Z','+00:00'))
        except (ValueError,TypeError): raise MarketError('STALE_DATA: Latest candle timestamp is invalid.')
        if candle_at.tzinfo is None: candle_at=candle_at.astimezone()
        if datetime.now(candle_at.tzinfo)-candle_at>timedelta(minutes=max(15,settings.candle_interval_minutes*3)): raise MarketError('STALE_DATA: Latest candle is too old for a current signal.')
        values=indicators(bars)
    except MarketError as exc: fail(exc)
    try:
        # Refresh only on cache expiry; otherwise keep supplying the last fetched reports.
        fundamental_sources=get_fundamental_sources()
        # Use ADK only when explicitly selected or auto-enabled by a configured key.
        # Both paths return the same SignalProposal consumed by the Python risk gate.
        if adk_enabled():
            context={'instrument_key':settings.upstox_instrument_key,'timeframe_minutes':settings.candle_interval_minutes,'as_of':bars[-1]['timestamp'],'features':values,'recent_candles':bars[-80:],'fundamental_sources':fundamental_sources}
            proposal=analyze_with_adk(context)
        else:
            proposal=deterministic_agents(values,fundamental_sources)
    except Exception as exc:
        reason=safe_failure_reason(exc)
        audit('signal_analysis_failed',reason)
        raise HTTPException(503,detail=f'ADK_ANALYSIS_FAILED: {reason} No signal was created.')
    # Treat all model output as untrusted: persist it only after local validation.
    errors=validate(proposal); expires=(datetime.now().astimezone()+timedelta(minutes=int(configured()['signal_ttl_minutes']))).isoformat()
    validation='; '.join(errors) if errors else 'Eligible for human review only.'
    with db() as con:
        cur=con.execute('INSERT INTO signals(created_at,expires_at,side,entry,stop,target,confidence,rationale,reports,status,validation) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(now(),expires,proposal.side.value,proposal.entry,proposal.stop,proposal.target,proposal.confidence,json.dumps(proposal.rationale),json.dumps([r.model_dump() for r in proposal.reports]),'DRAFT',validation)); signal_id=cur.lastrowid
    audit('signal_generated',f'signal={signal_id}; {validation}'); return signal(signal_id)
def signal(signal_id):
    """Load a signal row and decode its persisted rationale and agent reports."""
    with db() as con: row=con.execute('SELECT * FROM signals WHERE id=?',(signal_id,)).fetchone()
    if not row: raise HTTPException(404,'Signal not found.')
    item=dict(row); item['rationale']=json.loads(item['rationale']); item['reports']=json.loads(item['reports']); return item
@app.get('/api/signals')
def signals():
    """Return the most recent signal records for the review dashboard."""
    with db() as con: ids=[r['id'] for r in con.execute('SELECT id FROM signals ORDER BY id DESC LIMIT 50')]
    return [signal(i) for i in ids]
@app.get('/api/signals/{signal_id}')
def one_signal(signal_id:int):
    """Fetch one signal by identifier or return the shared 404 response."""
    return signal(signal_id)
@app.post('/api/signals/{signal_id}/approval')
def approve(signal_id:int,payload:Approval):
    """Record the human's approve/reject decision and enforce signal expiry."""
    item=signal(signal_id)
    if item['status']!='DRAFT': raise HTTPException(409,'Only draft signals can be approved or rejected.')
    if datetime.fromisoformat(item['expires_at'])<datetime.now().astimezone(): status='EXPIRED'
    else: status='APPROVED' if payload.approved else 'REJECTED'
    with db() as con: con.execute('UPDATE signals SET status=? WHERE id=?',(status,signal_id))
    audit('signal_'+status.lower(),f'signal={signal_id}; {payload.note}'); return signal(signal_id)
@app.post('/api/paper/entries')
def paper_entry(payload:PaperEntry):
    """Record a manual paper fill after rechecking approval and Python risk limits."""
    item=signal(payload.signal_id)
    if item['status']!='APPROVED': raise HTTPException(409,'A human-approved signal is required.')
    if item['side'] not in ('BUY','SELL'): raise HTTPException(422,'Signal is not tradeable.')
    errors=validate(type('P',(),{'side':item['side'],'entry':item['entry'],'stop':item['stop'],'target':item['target']})())
    if errors: raise HTTPException(422,{'risk_rejections':errors})
    stop_risk=abs(float(item['entry'])-float(item['stop']))*payload.quantity*int(configured()['contract_multiplier'])
    if stop_risk>float(configured()['risk_per_trade_inr']): raise HTTPException(422,{'risk_rejections':['Position quantity exceeds configured monetary risk per trade.']})
    with db() as con:
        if con.execute("SELECT 1 FROM positions WHERE status='OPEN'").fetchone(): raise HTTPException(409,'Only one open paper position is allowed.')
    entry_cost=cost(payload.entry_price,payload.quantity)
    with db() as con:
        cur=con.execute("INSERT INTO positions(signal_id,opened_at,side,entry_price,quantity,estimated_entry_cost,note,status) VALUES(?,?,?,?,?,?,?, 'OPEN')",(payload.signal_id,now(),item['side'],payload.entry_price,payload.quantity,entry_cost,payload.note)); con.execute("UPDATE signals SET status='OPENED' WHERE id=?",(payload.signal_id,)); position_id=cur.lastrowid
    audit('paper_entry',f'position={position_id}'); return position(position_id)
def position(position_id):
    """Load one paper-position row or return 404 when it does not exist."""
    with db() as con: row=con.execute('SELECT * FROM positions WHERE id=?',(position_id,)).fetchone()
    if not row: raise HTTPException(404,'Position not found.')
    return dict(row)
@app.get('/api/paper/positions')
def positions():
    """List journal positions newest first, including closed trades."""
    with db() as con: return [dict(r) for r in con.execute('SELECT * FROM positions ORDER BY id DESC')]
@app.post('/api/paper/positions/{position_id}/exit')
def paper_exit(position_id:int,payload:PaperExit):
    """Close an open paper position at the operator-supplied fill and book costs."""
    item=position(position_id)
    if item['status']!='OPEN': raise HTTPException(409,'Position is not open.')
    exit_cost=cost(payload.exit_price,item['quantity']); direction=1 if item['side']=='BUY' else -1; gross=round((payload.exit_price-item['entry_price'])*direction*item['quantity']*int(configured()['contract_multiplier']),2); net=round(gross-item['estimated_entry_cost']-exit_cost,2)
    with db() as con: con.execute("UPDATE positions SET closed_at=?,exit_price=?,estimated_exit_cost=?,gross_pnl=?,net_pnl=?,note=?,status='CLOSED' WHERE id=?",(now(),payload.exit_price,exit_cost,gross,net,payload.note,position_id)); con.execute("UPDATE signals SET status='CLOSED' WHERE id=?",(item['signal_id'],))
    audit('paper_exit',f'position={position_id}; net={net}'); return position(position_id)
@app.get('/api/analytics')
def analytics():
    """Aggregate closed paper results; these statistics are descriptive only."""
    with db() as con:
        rows=con.execute("SELECT net_pnl FROM positions WHERE status='CLOSED'").fetchall()
    pnls=[r['net_pnl'] for r in rows]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    return {'closed_trades':len(pnls),'net_pnl':round(sum(pnls),2),'win_rate':round(100*len(wins)/len(pnls),1) if pnls else 0,'profit_factor':round(sum(wins)/abs(sum(losses)),2) if losses else None,'expectancy':round(sum(pnls)/len(pnls),2) if pnls else 0,'note':'Paper results exclude fill uncertainty and are not evidence of future profitability.'}
@app.get('/api/settings')
def get_settings():
    """Return editable risk and signal-lifecycle values from SQLite."""
    return configured()
@app.put('/api/settings')
def update_settings(payload:SettingsUpdate):
    """Persist validated risk controls and audit that policy settings changed."""
    with db() as con:
        for key,value in payload.model_dump().items(): con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
    audit('settings_updated','Risk configuration changed.'); return configured()
@app.get('/api/audit')
def audit_log():
    """Return the latest local audit events for operational review."""
    with db() as con: return [dict(r) for r in con.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 100')]

@app.post('/api/backtests')
def create_backtest(payload:BacktestRequest):
    """Validate, replay, persist, and return one CSV backtest result."""
    try:
        rows=parse_csv(payload.csv_text)
        result=run_backtest(rows,start=payload.start,end=payload.end,strategy=payload.strategy,risk_inr=payload.risk_inr,max_stop_points=payload.max_stop_points,multiplier=payload.multiplier,lot_size=payload.lot_size,brokerage_rate=payload.brokerage_rate,tax_rate=payload.tax_rate,slippage_points=payload.slippage_points,force_close=configured()['exit_cutoff'])
    except ValueError as exc: raise HTTPException(422,str(exc))
    config=json.dumps(result['config'])
    with db() as con:
        cur=con.execute('INSERT INTO backtests(created_at,config,result) VALUES(?,?,?)',(now(),config,json.dumps(result))); run_id=cur.lastrowid
    result.update({'id':run_id,'created_at':now()}); audit('backtest_completed',f'run={run_id}; trades={result["trade_count"]}'); return result

@app.get('/api/backtests')
def list_backtests():
    """List stored backtest configurations and summary/trade results."""
    with db() as con: rows=con.execute('SELECT id,created_at,config,result FROM backtests ORDER BY id DESC LIMIT 50').fetchall()
    return [{'id':r['id'],'created_at':r['created_at'],'config':json.loads(r['config']),'result':json.loads(r['result'])} for r in rows]

@app.get('/api/backtests/{run_id}')
def get_backtest(run_id:int):
    """Load one persisted replay result by run identifier."""
    with db() as con: row=con.execute('SELECT id,created_at,config,result FROM backtests WHERE id=?',(run_id,)).fetchone()
    if not row: raise HTTPException(404,'Backtest not found.')
    return {'id':row['id'],'created_at':row['created_at'],'config':json.loads(row['config']),'result':json.loads(row['result'])}

def _read_replay(replay_id:int):
    """Load the complete local replay record or raise the API's standard 404."""
    with db() as con: row=con.execute('SELECT * FROM replay_sessions WHERE id=?',(replay_id,)).fetchone()
    if not row: raise HTTPException(404,'Replay session not found.')
    return dict(row)

def _replay_response(row:dict):
    """Decode persisted replay JSON and calculate progress for screen rendering."""
    config=json.loads(row['config']); steps=json.loads(row['steps']); total=len(json.loads(row['candles']))-config['start_index']
    return {'id':row['id'],'status':row['status'],'start_at':row['start_at'],'interval_minutes':row['interval_minutes'],'progress':{'current_step':len(steps),'total_steps':total,'percent':round(len(steps)*100/total,1) if total else 0},'config':config,'current_step':steps[-1] if steps else None,'steps':steps}

def _advance_replay(replay_id:int,index:int,*,replace_current:bool=False):
    """Analyze one replay candle using only the prefix available at that timestamp."""
    row=_read_replay(replay_id); config=json.loads(row['config']); bars=json.loads(row['candles']); history=bars[:index+1]
    features=indicators(history)
    if features is None: raise HTTPException(422,'Replay needs at least 30 candles before its selected start time.')
    candle=bars[index]; analysis_mode='deterministic'; analysis_error=None
    try:
        if adk_enabled():
            # Only the historical prefix is sent; bars after this index cannot leak into this decision.
            analysis_mode='adk_historical_experimental'
            proposal=analyze_with_adk({'mode':'historical_replay_experimental','historical_as_of':candle['timestamp'],'timeframe_minutes':row['interval_minutes'],'features':features,'recent_candles':history[-80:],'fundamental_sources':[],'instruction':'Use only supplied candles at or before historical_as_of. This is historical analysis, not live market data.'})
        else:
            proposal=deterministic_agents(features)
    except Exception as exc:
        analysis_mode='adk_historical_experimental' if analysis_mode.startswith('adk') else analysis_mode
        analysis_error=safe_failure_reason(exc)
        proposal=SignalProposal(side=Side.INSUFFICIENT_DATA,confidence=0,rationale=[analysis_error],reports=[AgentReport(agent='orchestrator',conclusion=analysis_error,data_status='insufficient')])
    risk=validate_replay(proposal,candle['timestamp'],max_stop_points=config['max_stop_points'],risk_inr=config['risk_inr'],multiplier=config['multiplier'],lot_size=config['lot_size'],entry_cutoff=config['entry_cutoff']) if analysis_error is None else {'eligible':False,'reasons':['Analysis failed; no risk approval is possible.'],'stop_distance':None,'suggested_quantity':0,'risk_at_stop_inr':0}
    step={'step_number':index-config['start_index']+1,'candle_index':index,'timestamp':candle['timestamp'],'candle':candle,'features':features,'analysis_mode':analysis_mode,'analysis_error':analysis_error,'recommendation':{'side':proposal.side.value,'entry':proposal.entry,'stop':proposal.stop,'target':proposal.target,'confidence':proposal.confidence,'confidence_note':'Model confidence is not a calibrated probability.','rationale':proposal.rationale},'reports':[report.model_dump() for report in proposal.reports],'risk':risk}
    steps=json.loads(row['steps'])
    if replace_current and steps and steps[-1]['candle_index']==index: steps[-1]=step
    else: steps.append(step)
    status_value='COMPLETED' if index>=len(bars)-1 else 'RUNNING'
    with db() as con: con.execute('UPDATE replay_sessions SET current_index=?,status=?,steps=? WHERE id=?',(index,status_value,json.dumps(steps),replay_id))
    audit('replay_step_analyzed',f'replay={replay_id}; step={step["step_number"]}; mode={analysis_mode}')
    return _replay_response(_read_replay(replay_id))

@app.post('/api/replays')
def start_replay(payload:ReplayStartRequest):
    """Create a replay and analyze the first available candle at/after start_at."""
    try: bars=parse_csv(payload.csv_text)
    except ValueError as exc: raise HTTPException(422,str(exc))
    requested=payload.start_at
    if requested.tzinfo is None: requested=requested.replace(tzinfo=IST)
    requested=requested.astimezone(IST)
    start_index=None
    for i,bar in enumerate(bars):
        stamp=datetime.fromisoformat(bar['timestamp'].replace('Z','+00:00'))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=IST)
        if stamp.astimezone(IST)>=requested: start_index=i; break
    if start_index is None: raise HTTPException(422,'No candle exists at or after the selected start time.')
    if start_index<29: raise HTTPException(422,'Select a start time with at least 29 earlier candles for feature warm-up.')
    config={'start_index':start_index,'requested_start_at':payload.start_at.isoformat(),'risk_inr':payload.risk_inr,'max_stop_points':payload.max_stop_points,'multiplier':payload.multiplier,'lot_size':payload.lot_size,'entry_cutoff':configured()['entry_cutoff']}
    with db() as con:
        cur=con.execute('INSERT INTO replay_sessions(created_at,start_at,interval_minutes,current_index,status,config,candles,steps) VALUES(?,?,?,?,?,?,?,?)',(now(),bars[start_index]['timestamp'],payload.interval_minutes,start_index-1,'RUNNING',json.dumps(config),json.dumps(bars),json.dumps([]))); replay_id=cur.lastrowid
    return _advance_replay(replay_id,start_index)

@app.post('/api/replays/{replay_id}/next')
def next_replay_candle(replay_id:int):
    """Advance exactly one imported candle and rerun the analysis cycle."""
    row=_read_replay(replay_id)
    if row['status']=='COMPLETED': raise HTTPException(409,'Replay is already complete.')
    next_index=row['current_index']+1
    if next_index>=len(json.loads(row['candles'])): raise HTTPException(409,'No candles remain in this replay.')
    return _advance_replay(replay_id,next_index)

@app.post('/api/replays/{replay_id}/retry')
def retry_replay_candle(replay_id:int):
    """Retry a failed current candle without adding a duplicate history step."""
    row=_read_replay(replay_id); steps=json.loads(row['steps'])
    if not steps or not steps[-1].get('analysis_error'):
        raise HTTPException(409,'The current replay candle has no analysis failure to retry.')
    return _advance_replay(replay_id,row['current_index'],replace_current=True)

@app.get('/api/replays/{replay_id}')
def get_replay(replay_id:int):
    """Return current replay progress and the accumulated per-candle review log."""
    return _replay_response(_read_replay(replay_id))

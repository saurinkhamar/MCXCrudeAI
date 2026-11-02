/** Local dashboard for live status, paper review, full-history backtests, and stepped replay.
 * Credentials stay on the backend; replay requests contain historical CSV rows only.
 */
import {useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

const API='http://127.0.0.1:8001/api';

/** Centralize local API errors so view actions show the backend's safe message. */
async function api(path:string,options?:RequestInit){
 const response=await fetch(API+path,{headers:{'Content-Type':'application/json'},...options});
 const body=await response.json();
 if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail)||response.statusText);
 return body;
}

function App(){
 // Keep transient view state here; SQLite remains the source of truth for persisted data.
 const [quote,setQuote]=useState<any>();
 const [signals,setSignals]=useState<any[]>([]);
 const [positions,setPositions]=useState<any[]>([]);
 const [analytics,setAnalytics]=useState<any>();
 const [agentStatus,setAgentStatus]=useState<any>();
 const [fundamentals,setFundamentals]=useState<any>();
 const [error,setError]=useState('');
 const [backtest,setBacktest]=useState<any>();
 const [csvFile,setCsvFile]=useState<File>();
 const [startAt,setStartAt]=useState('');
 const [intervalMinutes,setIntervalMinutes]=useState(5);
 const [riskInr,setRiskInr]=useState(2000);
 const [maxStopPoints,setMaxStopPoints]=useState(100);
 const [multiplier,setMultiplier]=useState(10);
 const [lotSize,setLotSize]=useState(1);
 const [replay,setReplay]=useState<any>();
 const [busy,setBusy]=useState(false);
 const [activeScreen,setActiveScreen]=useState<'live'|'backtest'>('live');

 // Refresh dashboard panels together without exposing API credentials to the browser.
 const refresh=async()=>{
  const results=await Promise.allSettled([api('/market/quote'),api('/signals'),api('/paper/positions'),api('/analytics'),api('/agents/status'),api('/fundamentals/status')]);
  const [market,signalRows,positionRows,summary,agents,fundamentalStatus]=results;
  if(market.status==='fulfilled')setQuote(market.value);
  else{
   const message=market.reason?.message??'Live market status request failed.';
   const state=message.includes('TOKEN_EXPIRED')?'TOKEN_EXPIRED':message.includes('TOKEN_INVALID')?'TOKEN_INVALID':message.includes('MARKET_CLOSED')?'MARKET_CLOSED':'DATA_UNAVAILABLE';
   setQuote({state,detail:message});
  }
  if(signalRows.status==='fulfilled')setSignals(signalRows.value);
  if(positionRows.status==='fulfilled')setPositions(positionRows.value);
  if(summary.status==='fulfilled')setAnalytics(summary.value);
  if(agents.status==='fulfilled')setAgentStatus(agents.value);
  if(fundamentalStatus.status==='fulfilled')setFundamentals(fundamentalStatus.value);
  const failed=results.find(result=>result.status==='rejected') as PromiseRejectedResult|undefined;
  setError(failed?.reason?.message??'');
 };

 // Poll API-backed panels and clean up the timer on unmount.
 useEffect(()=>{refresh();const timer=setInterval(refresh,30000);return()=>clearInterval(timer)},[]);

 // A generated signal is only a draft; approval remains a separate human action.
 const generate=async()=>{
  setBusy(true);
  try{await api('/signals/generate',{method:'POST'});await refresh()}
  catch(exception:any){setError(exception.message)}
  finally{setBusy(false)}
 };

 // Persist the operator's explicit approval or rejection.
 const approval=async(id:number,approved:boolean)=>{
  try{await api(`/signals/${id}/approval`,{method:'POST',body:JSON.stringify({approved})});await refresh()}
  catch(exception:any){setError(exception.message)}
 };

 // Run the existing one-shot deterministic backtest on the selected CSV.
 const runBacktest=async(file?:File)=>{
  if(!file)return;
  setBusy(true);
  try{const csv_text=await file.text();setBacktest(await api('/backtests',{method:'POST',body:JSON.stringify({csv_text})}));setError('')}
  catch(exception:any){setError(exception.message)}
  finally{setBusy(false)}
 };

 // Start historical review at the selected time; backend picks the first candle at or after it.
 const startReplay=async()=>{
  if(!csvFile||!startAt){setError('Choose a CSV and replay start date/time first.');return}
  setBusy(true);
  try{
   const csv_text=await csvFile.text();
   const session=await api('/replays',{method:'POST',body:JSON.stringify({csv_text,start_at:startAt,interval_minutes:intervalMinutes,risk_inr:riskInr,max_stop_points:maxStopPoints,multiplier,lot_size:lotSize})});
   setReplay(session);setError('');
  }catch(exception:any){setError(exception.message)}
  finally{setBusy(false)}
 };

 // Load exactly one later CSV row and rerun analysis on the newly available history prefix.
 const nextCandle=async()=>{
  if(!replay)return;
  setBusy(true);
  try{setReplay(await api(`/replays/${replay.id}/next`,{method:'POST'}));setError('')}
  catch(exception:any){setError(exception.message)}
  finally{setBusy(false)}
 };

 // Retry a transient provider/schema failure on the same historical candle.
 const retryCandle=async()=>{
  if(!replay)return;
  setBusy(true);
  try{setReplay(await api(`/replays/${replay.id}/retry`,{method:'POST'}));setError('')}
  catch(exception:any){setError(exception.message)}
  finally{setBusy(false)}
 };

 const step=replay?.current_step;
 // A no-trade signal has no directional score; zero is shown as unscored, not as a failed analysis.
 const confidenceText=(side:string,value:number|null|undefined)=>(side==='NO_TRADE'||side==='INSUFFICIENT_DATA')&&Number(value??0)===0?'Not scored (0%)':`${value??0}% model score (uncalibrated)`;
 // Render API timestamps in the operator's local timezone while retaining exact source in a title.
 const displayTime=(value:string)=>{const parsed=new Date(value);return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString()};
 // Share the expanded signal body between normal cards and collapsed no-trade summaries.
 const signalContent=(signal:any)=><><p>Entry {signal.entry??'—'} · Stop {signal.stop??'—'} · Target {signal.target??'—'} · Confidence {confidenceText(signal.side,signal.confidence)}</p>{signal.side==='NO_TRADE'&&Number(signal.confidence??0)===0&&<p className="info-note">No direction was selected, so no directional confidence score was assigned. This is not by itself a system failure.</p>}<p>{signal.validation}</p><p>{signal.rationale?.join(' ')}</p>{signal.status==='DRAFT'&&signal.side!=='NO_TRADE'&&signal.side!=='INSUFFICIENT_DATA'&&<><button onClick={()=>approval(signal.id,true)}>Approve</button><button className="quiet" onClick={()=>approval(signal.id,false)}>Reject</button></>}<details><summary>Analyst reports</summary>{signal.reports?.map((report:any)=><p key={report.agent}><b>{report.agent}:</b> {report.conclusion} <small>({report.data_status})</small></p>)}</details></>;
 const marketState=quote?.state??(quote?.market_open?'READY':'DATA_UNAVAILABLE');
 const marketLabel:Record<string,string>={READY:'Live data ready',STALE_DATA:'Data is stale',MARKET_CLOSED:'Market closed',TOKEN_EXPIRED:'Access token expired',TOKEN_INVALID:'Access token invalid',DATA_UNAVAILABLE:'Live data unavailable'};
 return <main>
  <header className="app-header"><div><h1>MCX Crude AI <small>paper trading only</small></h1><p>Local research dashboard · No broker order execution</p></div><nav className="screen-nav" aria-label="Application screens"><button className={activeScreen==='live'?'selected':''} onClick={()=>setActiveScreen('live')}>Realtime</button><button className={activeScreen==='backtest'?'selected':''} onClick={()=>setActiveScreen('backtest')}>Backtest</button></nav></header>
  {error&&<p className="error">{error}</p>}
  {activeScreen==='live'&&<>
  <section className="warn">Instrument key and contract metadata remain unverified. Validate expiry, lot size and tick size before using quantities.</section>
  <div className="realtime-layout">
   <div className="realtime-main">
   <section className="signal-list"><div className="section-heading"><div><h2>Realtime signals</h2><p>Newest signal reviews with generation time and analyst reports.</p></div><button disabled={busy||marketState!=='READY'} onClick={generate}>{busy?'Generating review…':'Generate review signal'}</button></div>{signals.length===0?<p>No signals yet. Use Generate review signal when live data is ready.</p>:signals.map(signal=>signal.side==='NO_TRADE'?<details className="signal signal-collapsed" key={signal.id}><summary className="signal-summary"><span className="signal-id">#{signal.id}</span><b>No Trade</b><time className="signal-time" dateTime={signal.created_at} title={signal.created_at}>{displayTime(signal.created_at)}</time><span className="expand-chevron" aria-hidden="true">⌄</span></summary><div className="signal-content">{signalContent(signal)}</div></details>:<article className="signal" key={signal.id}><div className="card-heading"><b>#{signal.id} {signal.side}</b><span>{signal.status}</span></div><p className="signal-time">Generated: <time dateTime={signal.created_at} title={signal.created_at}>{displayTime(signal.created_at)}</time></p>{signalContent(signal)}</article>)}</section>
   <section><h2>Paper positions</h2>{positions.length===0?<p>No paper positions.</p>:positions.map(position=><article key={position.id}><b>#{position.id} {position.side}</b> · {position.status} · Entry {position.entry_price} · Net P&amp;L {position.net_pnl??'open'}</article>)}</section>
   </div>
   <aside className="realtime-sidebar" aria-label="Realtime status tiles">
   <article className="market-card"><div className="card-heading"><h2>Live market</h2><span className={`market-badge ${marketState.toLowerCase()}`}>{marketLabel[marketState]??marketState}</span></div>{quote?<><b>{quote.symbol||quote.instrument_key}</b><p className="price">₹ {quote.last_price??'—'}</p><p>Updated: {quote.retrieved_at?displayTime(quote.retrieved_at):quote.timestamp?displayTime(quote.timestamp):'timestamp not supplied'}</p>{quote.detail&&<p>{quote.detail}</p>}</>:<p>Waiting for market status…</p>}<button className="quiet" onClick={refresh}>Refresh market status</button></article>
    <article><h2>Signal analysis</h2><p>Selected mode: <b>{agentStatus?.analysis_selected??'checking…'}</b> · Model {agentStatus?.model??'—'}</p><p>Automatic cycle: <b>{agentStatus?.scheduler_running?'Running':'Disabled'}</b>{agentStatus?.scheduler_running?` · every ${agentStatus.scheduler_interval_minutes} min · ${agentStatus.candle_interval_minutes}-min candles`:''}</p><p>Fundamental knowledge: <b>{fundamentals?.source_count??'—'} cached reports</b> · refresh every {fundamentals?.refresh_minutes??60} min{fundamentals?.last_fetched_at?` · fetched ${displayTime(fundamentals.last_fetched_at)}`:''}</p>{fundamentals?.last_error&&<p className="info-note">Latest source refresh had an error; previously fetched reports are retained.</p>}{fundamentals?.source_count>0&&<details><summary>Review sourced reports</summary>{fundamentals.sources.map((item:any,index:number)=><p key={`${item.url}-${index}`}><b>{item.title}</b><br/><small>{item.source} · published {item.published_at?displayTime(item.published_at):'date unavailable'} · <a href={item.url} target="_blank" rel="noreferrer">Open source</a></small><br/>{item.summary}</p>)}</details>}<p>Agent calls require a valid Google API key and the configured model to be available.</p>{agentStatus?.configuration_error&&<p className="error">Configuration issue: {agentStatus.configuration_error}</p>}{marketState!=='READY'&&<small className="block-note">Signal review is disabled until live data is ready.</small>}</article>
   <article><h2>Paper performance</h2><p>Net P&amp;L: ₹ {analytics?.net_pnl??'—'}</p><p>Trades: {analytics?.closed_trades??'—'} · Win rate: {analytics?.win_rate??'—'}%</p><p>{analytics?.note}</p></article>
   </aside>
  </div>
  </>}

  {activeScreen==='backtest'&&<>
  <section><h2>Historical data</h2><p>Choose a CSV once for a deterministic full-run backtest or step-by-step historical replay. This screen is separate from current market signals.</p><input type="file" accept=".csv,text/csv" disabled={busy} onChange={event=>{setCsvFile(event.target.files?.[0]);setBacktest(undefined);setReplay(undefined)}}/>{csvFile&&<p>Selected file: {csvFile.name}</p>}</section>
  <section>
   <h2>CSV backtest</h2><p>Run the full deterministic replay over all imported bars. Supports timestamp or Date, case-insensitive OHLC headers, optional Volume, and extra columns such as % Change.</p>
   <button disabled={busy||!csvFile} onClick={()=>runBacktest(csvFile)}>Run full backtest</button>
   {busy&&!replay&&<p>Running backtest…</p>}
   {backtest&&<article><b>{backtest.trade_count} trades · Net ₹{backtest.net_pnl} · Win rate {backtest.win_rate}%</b><p>Profit factor {backtest.profit_factor??'—'} · Max drawdown ₹{backtest.max_drawdown} · Expectancy ₹{backtest.expectancy} · Costs ₹{backtest.total_costs}</p><details><summary>Trade list and run configuration</summary><pre>{JSON.stringify({config:backtest.config,trades:backtest.trades},null,2)}</pre></details></article>}
  </section>

  <section className="replay-panel">
   <h2>Step-by-step candle replay</h2>
   <p>Select a historical start time with at least 29 earlier candles available for indicator warm-up. The first available candle at or after that time is analyzed. Each click on <b>Next candle</b> loads one later CSV candle and repeats the ADK or deterministic analysis using only candles up to that timestamp. This is experimental historical agent analysis, not live ADK market context. ADK mode makes four model calls per candle and may use billable quota; choose deterministic mode in backend/.env for local-only replay.</p>
   <div className="replay-controls">
    <label>Start date and time<input type="datetime-local" value={startAt} onChange={event=>setStartAt(event.target.value)}/></label>
    <label>Interval (minutes)<input type="number" min="1" max="300" value={intervalMinutes} onChange={event=>setIntervalMinutes(Number(event.target.value))}/></label>
    <label>Risk budget (₹)<input type="number" min="1" value={riskInr} onChange={event=>setRiskInr(Number(event.target.value))}/></label>
    <label>Max stop (price points)<input type="number" min="0.01" value={maxStopPoints} onChange={event=>setMaxStopPoints(Number(event.target.value))}/></label>
    <label>Contract multiplier<input type="number" min="1" value={multiplier} onChange={event=>setMultiplier(Number(event.target.value))}/></label>
    <label>Lot size<input type="number" min="1" value={lotSize} onChange={event=>setLotSize(Number(event.target.value))}/></label>
    <button disabled={busy||!csvFile||!startAt} onClick={startReplay}>Start replay</button>
    <button className="quiet" disabled={busy||!replay||replay.status==='COMPLETED'} onClick={nextCandle}>Next candle</button>
    {step?.analysis_error&&<button className="retry" disabled={busy} onClick={retryCandle}>Retry this candle</button>}
   </div>
   {replay&&<>
    <div className="progress-heading"><b>Replay #{replay.id} · {replay.status}</b><span>{replay.progress.current_step} / {replay.progress.total_steps} candles · {replay.progress.percent}%</span></div>
    <progress max="100" value={replay.progress.percent}/>
   </>}
   {step&&<>
    <article className="replay-step">
     <div className="step-heading"><h3>Step {step.step_number} · {step.timestamp}</h3><span>{step.analysis_mode}</span></div>
     <p>O {step.candle.open} · H {step.candle.high} · L {step.candle.low} · C {step.candle.close} · Volume {step.candle.volume}</p>
     <div className="grid"><div><b>Suggestion: {step.recommendation.side}</b><p>Confidence: {confidenceText(step.recommendation.side,step.recommendation.confidence)} <small>{step.recommendation.confidence_note}</small></p><p>Entry {step.recommendation.entry??'—'} · Stop {step.recommendation.stop??'—'} · Target {step.recommendation.target??'—'}</p><p>{step.recommendation.rationale?.join(' ')}</p></div>
      <div><b>Historical risk check: {step.risk.eligible?'Eligible for review':'No trade / blocked'}</b><p>{step.risk.reasons.join(' ')||`Stop ${step.risk.stop_distance} pts · Suggested quantity ${step.risk.suggested_quantity} · Stop risk ₹${step.risk.risk_at_stop_inr}`}</p><p>RSI {step.features.rsi} · ATR {step.features.atr} · EMA 9 {step.features.ema_fast.toFixed(2)} · EMA 21 {step.features.ema_slow.toFixed(2)}</p></div>
     </div>
     {step.analysis_error&&<p className="error">{step.analysis_error}</p>}
     <details open><summary>Agent reports ({step.reports.length})</summary>{step.reports.map((report:any)=><p key={report.agent}><b>{report.agent}:</b> {report.conclusion} <small>({report.data_status})</small>{report.rationale?.length?` · ${report.rationale.join(' ')}`:''}</p>)}</details>
    </article>
    <details><summary>Replay history ({replay.steps.length} analyzed candles)</summary><ol className="replay-history">{replay.steps.map((item:any)=><li key={item.step_number}>{item.timestamp} — {item.recommendation.side}, confidence {item.recommendation.confidence}% ({item.analysis_mode})</li>)}</ol></details>
   </>}
  </section>
  </>}
 </main>;
}

createRoot(document.getElementById('root')!).render(<App/>);

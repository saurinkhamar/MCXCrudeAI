"""Regression tests for ADK graph structure, report handoff, and API integration."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from mcx_ai import main
from mcx_ai.adk_agents import _contract_instruction, _parse_output, _run_async, build_agents, safe_failure_reason
from mcx_ai.config import settings
from mcx_ai.schemas import AgentReport, SignalProposal, Side
from mcx_ai.schemas import BacktestRequest, ReplayStartRequest
from mcx_ai.backtest import parse_csv

def test_adk_agent_graph_has_parallel_reports_then_orchestrator(monkeypatch):
    """Protect the explicit specialist output-key to Orchestrator contract."""
    monkeypatch.setattr(settings,'google_api_key','test-placeholder')
    root=build_agents()
    assert root.name=='mcx_signal_orchestration'
    analyst_team,orchestrator=root.sub_agents
    assert analyst_team.name=='analyst_team'
    assert {agent.output_key for agent in analyst_team.sub_agents}=={'fundamental_report','trend_report','momentum_report'}
    assert orchestrator.output_key=='signal_decision'
    assert all(agent.output_schema is None for agent in analyst_team.sub_agents)
    assert orchestrator.output_schema is None
    assert all('"required"' in agent.instruction and 'AgentReport' in agent.instruction for agent in analyst_team.sub_agents)
    assert 'SignalProposal' in orchestrator.instruction and '"required"' in orchestrator.instruction
    assert all(agent.generate_content_config.response_mime_type=='application/json' for agent in (*analyst_team.sub_agents,orchestrator))
    assert all(key in orchestrator.instruction for key in ('fundamental_report','trend_report','momentum_report'))

def test_adk_runner_report_handoff_and_decision_validation(monkeypatch):
    """Exercise parsing of session-state outputs without making a live model call."""
    import google.adk.runners as runners
    import google.adk.sessions as sessions
    monkeypatch.setattr(settings,'google_api_key','test-placeholder')
    reports={key:AgentReport(agent=name,conclusion=f'{name} checked',data_status='insufficient' if name=='fundamental' else 'available').model_dump_json() for key,name in [('fundamental_report','fundamental'),('trend_report','trend'),('momentum_report','momentum')]}
    reports['momentum_report']='not valid JSON'
    reports['signal_decision']=SignalProposal(side=Side.BUY,entry=100,stop=95,target=110,confidence=61,rationale=['Mocked workflow output.']).model_dump_json()
    class FakeSessionService:
        """Minimal in-memory session service stand-in for ADK's session API."""
        async def create_session(self,**kwargs):
            """Accept session creation without external persistence."""
            return None
        async def get_session(self,**kwargs):
            """Expose the prepared output-key values to the pipeline parser."""
            return type('Session',(),{'state':reports})()
    class FakeRunner:
        """Runner stand-in that prevents all network/model calls in this test."""
        def __init__(self,**kwargs):
            """Match the ADK runner constructor interface used by the service."""
            pass
        async def run_async(self,**kwargs):
            """Provide an empty async event stream; test state contains final outputs."""
            if False: yield None
        async def close(self):
            """Match ADK cleanup without holding external resources."""
            pass
    monkeypatch.setattr(sessions,'InMemorySessionService',FakeSessionService)
    monkeypatch.setattr(runners,'Runner',FakeRunner)
    proposal=asyncio.run(_run_async({'features':{'rsi':55},'fundamental_sources':[]}))
    assert proposal.side==Side.BUY and proposal.entry==100
    assert [r.agent for r in proposal.reports]==['fundamental','trend','momentum']
    assert proposal.reports[0].data_status=='insufficient'
    assert proposal.reports[2].data_status=='insufficient'

def test_adk_parser_accepts_fenced_json_and_rejects_invalid_proposal():
    """Validate tolerant JSON extraction without weakening the shared schema."""
    raw='```json\n{"agent":"trend","conclusion":"Range-bound","rationale":[],"data_status":"available"}\n```'
    assert _parse_output(AgentReport,raw,'trend report').conclusion=='Range-bound'
    try: _parse_output(SignalProposal,'{"side":"BUY","confidence":120}','orchestrator proposal')
    except RuntimeError as exc: assert 'ADK_OUTPUT_INVALID: orchestrator proposal' in str(exc)
    else: raise AssertionError('Out-of-contract proposal unexpectedly validated.')

def test_orchestrator_parser_normalizes_nontrade_and_ignores_echoed_reports():
    """Keep a harmless no-trade formatting variation from failing the cycle."""
    proposal=_parse_output(SignalProposal,{'side':'NO TRADE','entry':'—','stop':'—','target':'—','confidence':'0%','rationale':'No aligned setup.','reports':[{'malformed':'echo'}]},'orchestrator proposal')
    assert proposal.side==Side.NO_TRADE
    assert proposal.confidence==0 and proposal.entry is None and proposal.stop is None and proposal.target is None
    assert proposal.rationale==['No aligned setup.'] and proposal.reports==[]

def test_signal_generation_uses_adk_proposal(monkeypatch,tmp_path):
    """Ensure signal generation sends timestamped candles/features through ADK mode."""
    now=datetime.now().astimezone().replace(second=0,microsecond=0)
    bars=[{'timestamp':(now-timedelta(minutes=5*(35-i))).isoformat(),'open':100+i*.1,'high':101+i*.1,'low':99+i*.1,'close':100+i*.1,'volume':100} for i in range(35)]
    monkeypatch.setattr(settings,'db_path',str(tmp_path/'signals.db'))
    monkeypatch.setattr(main,'candles',lambda *args:bars)
    monkeypatch.setattr(main,'market_open',lambda:True)
    monkeypatch.setattr(main,'adk_enabled',lambda:True)
    sourced=[{'title':'EIA crude stocks update','summary':'Sourced test item','source':'eia.gov','published_at':'2026-11-02T04:00:00+00:00','url':'https://eia.gov/test'}]
    monkeypatch.setattr(main,'get_fundamental_sources',lambda:sourced)
    captured={}
    proposal=SignalProposal(side=Side.BUY,entry=103.4,stop=98,target=114,confidence=63,reports=[AgentReport(agent='trend',conclusion='aligned')])
    def analyze(context):
        """Capture the invocation payload while substituting a deterministic response."""
        captured.update(context)
        return proposal
    monkeypatch.setattr(main,'analyze_with_adk',analyze)
    monkeypatch.setattr(main,'validate',lambda proposal:[])
    main.init_db()
    result=main.generate()
    assert captured['features'] and captured['recent_candles']==bars
    assert captured['fundamental_sources']==sourced
    assert result['side']=='BUY' and result['reports'][0]['agent']=='trend'

def test_sample_csv_backtest_api_persists_result(monkeypatch,tmp_path):
    """Exercise the API service path from generated fixture through SQLite storage."""
    fixture=Path(__file__).resolve().parents[2]/'data'/'examples'/'synthetic_crude_5m.csv'
    csv_text=fixture.read_text(encoding='utf-8')
    assert len(parse_csv(csv_text))==360
    monkeypatch.setattr(settings,'db_path',str(tmp_path/'backtests.db'))
    main.init_db()
    created=main.create_backtest(BacktestRequest(csv_text=csv_text))
    saved=main.list_backtests()
    assert created['trade_count']>0 and created['mode']=='deterministic_historical_replay'
    assert saved[0]['id']==created['id'] and saved[0]['result']['trade_count']==created['trade_count']

def test_replay_advances_one_candle_and_adk_never_sees_future_rows(monkeypatch,tmp_path):
    """Each manual step must analyze its own candle with a truncated history prefix."""
    fixture=Path(__file__).resolve().parents[2]/'data'/'examples'/'synthetic_crude_5m.csv'
    csv_text=fixture.read_text(encoding='utf-8'); bars=parse_csv(csv_text); start_index=50
    monkeypatch.setattr(settings,'db_path',str(tmp_path/'replays.db'))
    monkeypatch.setattr(main,'adk_enabled',lambda:True)
    contexts=[]
    def analyze(context):
        """Capture each ADK input prefix and return a fixed valid test suggestion."""
        contexts.append(context)
        return SignalProposal(side=Side.BUY,entry=6200,stop=6190,target=6220,confidence=72,rationale=['Tested at replay timestamp.'])
    monkeypatch.setattr(main,'analyze_with_adk',analyze)
    main.init_db()
    first=main.start_replay(ReplayStartRequest(csv_text=csv_text,start_at=datetime.fromisoformat(bars[start_index]['timestamp']),interval_minutes=5))
    assert first['current_step']['timestamp']==bars[start_index]['timestamp']
    second=main.next_replay_candle(first['id'])
    assert second['current_step']['timestamp']==bars[start_index+1]['timestamp']
    assert second['progress']['current_step']==2
    assert len(contexts)==2
    for context in contexts:
        assert context['recent_candles'][-1]['timestamp']==context['historical_as_of']
        assert all(c['timestamp']<=context['historical_as_of'] for c in context['recent_candles'])
    assert contexts[0]['historical_as_of']<contexts[1]['historical_as_of']

def test_replay_can_retry_failed_candle_without_duplicate_step(monkeypatch,tmp_path):
    """A transient ADK failure should be replaceable at the same replay position."""
    fixture=Path(__file__).resolve().parents[2]/'data'/'examples'/'synthetic_crude_5m.csv'
    csv_text=fixture.read_text(encoding='utf-8'); bars=parse_csv(csv_text); start_index=50
    monkeypatch.setattr(settings,'db_path',str(tmp_path/'retry.db'))
    monkeypatch.setattr(main,'adk_enabled',lambda:True)
    calls={'count':0}
    def analyze(context):
        """Fail once to model a transient provider issue, then return a valid result."""
        calls['count']+=1
        if calls['count']==1: raise RuntimeError('temporary provider failure')
        return SignalProposal(side=Side.NO_TRADE,confidence=20,rationale=['Retry succeeded.'])
    monkeypatch.setattr(main,'analyze_with_adk',analyze)
    main.init_db()
    first=main.start_replay(ReplayStartRequest(csv_text=csv_text,start_at=datetime.fromisoformat(bars[start_index]['timestamp'])))
    assert first['progress']['current_step']==1 and first['current_step']['analysis_error']
    retried=main.retry_replay_candle(first['id'])
    assert retried['progress']['current_step']==1
    assert retried['current_step']['timestamp']==bars[start_index]['timestamp']
    assert retried['current_step']['analysis_error'] is None

def test_agent_status_confirms_loaded_key_without_returning_value(monkeypatch):
    """The UI can diagnose ADK selection without receiving the secret itself."""
    monkeypatch.setattr(settings,'google_api_key','test-only-not-a-real-key')
    monkeypatch.setattr(settings,'agent_mode','auto')
    status=main.agent_status()
    assert status['google_api_key_configured'] is True
    assert status['analysis_selected']=='adk'
    assert 'google_api_key' not in status
    assert 'test-only-not-a-real-key' not in str(status)

def test_adk_failure_diagnostic_is_actionable_and_redacts_credentials(monkeypatch):
    """Safe diagnostics should classify provider failures without echoing keys."""
    secret='test-secret-value-that-must-not-leak'
    monkeypatch.setattr(settings,'google_api_key',secret)
    invalid=safe_failure_reason(RuntimeError(f'API key not valid: {secret}'))
    quota=safe_failure_reason(RuntimeError('429 RESOURCE_EXHAUSTED'))
    assert 'GOOGLE_API_KEY' in invalid and secret not in invalid
    assert 'quota' in quota.casefold()

def test_scheduled_cycle_creates_and_audits_one_signal(monkeypatch):
    """Verify the automatic loop invokes generation at its configured cadence."""
    monkeypatch.setattr(settings,'scheduler_interval_minutes',1)
    monkeypatch.setattr(main,'market_open',lambda:True)
    monkeypatch.setattr(main,'generate',lambda:{'id':12,'created_at':'2026-11-02T10:00:00+05:30'})
    events=[]
    monkeypatch.setattr(main,'audit',lambda event,detail:events.append((event,detail)))
    class StopAfterOneCycle:
        """Stop the test loop after it has executed a single scheduled pass."""
        def __init__(self): self.calls=0
        def wait(self,seconds):
            """Check the configured one-minute wait and release the second pass."""
            assert seconds==60
            self.calls+=1
            return self.calls>1
    monkeypatch.setattr(main,'_scheduler_stop',StopAfterOneCycle())
    main._scheduled_analysis()
    assert events==[('scheduled_analysis_completed','signal=12; created_at=2026-11-02T10:00:00+05:30')]

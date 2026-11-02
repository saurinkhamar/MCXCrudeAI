"""Google ADK multi-agent pipeline with explicit shared-state handoff."""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from .config import settings
from .schemas import AgentReport, SignalProposal, Side

PROMPTS=Path(__file__).resolve().parent/'prompts'

def prompt(name, fallback):
    """Load one editable role prompt; use inline safe text if missing."""
    try: return (PROMPTS/f'{name}.md').read_text(encoding='utf-8')
    except OSError: return fallback

def adk_enabled():
    """Resolve explicit ADK/deterministic mode without revealing credentials."""
    mode=settings.agent_mode.lower().strip()
    if mode not in {'auto','adk','deterministic'}:
        raise ValueError('AGENT_MODE must be auto, adk, or deterministic.')
    if mode=='adk' and not settings.google_api_key:
        raise RuntimeError('ADK mode requires GOOGLE_API_KEY in backend/.env.')
    return mode=='adk' or (mode=='auto' and bool(settings.google_api_key))

def safe_failure_reason(exc: Exception) -> str:
    """Map ADK/provider failures to actionable text without returning raw details.

    Provider exceptions can contain request metadata or generated content. Only
    return a fixed diagnostic category; never include raw exception text/headers.
    """
    chain=[]; current=exc
    while current is not None and current not in chain:
        chain.append(current); current=current.__cause__ or current.__context__
    details=' '.join(f'{type(item).__name__} {item}' for item in chain).casefold()
    if any(token in details for token in ('api key not valid','invalid api key','api_key_invalid','unauthenticated','401')):
        return 'Google rejected the API credential. Check GOOGLE_API_KEY and that the Generative Language API is enabled for its project.'
    if any(token in details for token in ('resource_exhausted','quota','rate limit','429')):
        return 'Google API quota or rate limit was reached. Wait or check the project quota and billing.'
    if any(token in details for token in ('model not found','is not found for api version','404 not_found','unsupported model')):
        return f'The configured model is unavailable to this API project ({settings.google_model}). Check GOOGLE_MODEL access.'
    if 'permission_denied' in details or '403' in details:
        return f'Google denied access to the configured model ({settings.google_model}). Check project/API permissions.'
    if any(token in details for token in ('timed out','timeout','deadline exceeded')):
        return 'The ADK request timed out. Retry the candle or check the network connection.'
    for item in chain:
        message=str(item).casefold()
        if 'adk_output_invalid:' in message:
            failure=message.split('adk_output_invalid:',1)[1].strip().rstrip('.')
            stage,_,reason=failure.partition(';')
            detail=f' Fields: {reason.strip()}' if reason.strip() else ''
            return f'The {stage.strip()} ADK output did not match its schema.{detail} Retry this candle; if repeated, review that role prompt.'
        if isinstance(item,RuntimeError) and ('required report' in str(item).casefold() or 'orchestrator did not provide' in str(item).casefold()):
            return str(item)
    if any(token in details for token in ('validationerror','invalid json','response_schema','schema validation')):
        return 'An agent response did not match the required output schema. Review the role prompt and retry.'
    if 'google-adk is not installed' in details:
        return 'Google ADK is not installed in this backend environment. Install backend/requirements.txt and restart.'
    return f'ADK analysis failed ({type(chain[-1]).__name__}). Check the backend log and agent configuration, then retry.'

def _parse_output(model_type, raw, stage):
    """Parse one model response and enforce the stable contract in Python.

    ADK's provider-side structured-output validation can abort an entire
    parallel workflow when one specialist emits a malformed response. Ask for
    JSON, then validate locally so one bad report can be downgraded to
    INSUFFICIENT_DATA without losing the whole replay step.
    """
    if isinstance(raw,dict): payload=raw
    elif isinstance(raw,str):
        text=raw.strip()
        if text.startswith('```'):
            text=re.sub(r'^```[^\r\n]*\s*','',text,flags=re.IGNORECASE)
            text=re.sub(r'\s*```\s*$','',text).strip()
        try: payload=json.loads(text)
        except json.JSONDecodeError:
            payload=_extract_json_object(text,model_type)
            if payload is None: raise RuntimeError(f'ADK_OUTPUT_INVALID: {stage}; invalid JSON or no matching object (chars={len(text)}).')
        if isinstance(payload,str):
            payload=_extract_json_object(payload,model_type)
            if payload is None: raise RuntimeError(f'ADK_OUTPUT_INVALID: {stage}; JSON string did not contain a matching object.')
        elif isinstance(payload,dict) and not set(model_type.model_fields).intersection(payload):
            nested=_extract_json_object(text,model_type)
            if nested is not None: payload=nested
    else: raise RuntimeError(f'ADK_OUTPUT_INVALID: {stage}; response was empty or not text.')
    if model_type is SignalProposal:
        # Specialist reports have their own validated handoff and are attached
        # after this parse; never let an echoed/corrupted copy reject a signal.
        payload.pop('reports',None)
        side=payload.get('side')
        if isinstance(side,str):
            normalized=side.strip().upper().replace(' ','_').replace('-','_')
            payload['side']={'NO_TRADE':'NO_TRADE','NOTRADE':'NO_TRADE','WAIT':'NO_TRADE','HOLD':'NO_TRADE','INSUFFICIENT':'INSUFFICIENT_DATA'}.get(normalized,normalized)
        if isinstance(payload.get('confidence'),str):
            confidence=payload['confidence'].strip().removesuffix('%').strip()
            try: payload['confidence']=float(confidence)
            except ValueError: pass
        if isinstance(payload.get('rationale'),str): payload['rationale']=[payload['rationale']]
        if isinstance(payload.get('side'),str) and payload.get('side') in {'NO_TRADE','INSUFFICIENT_DATA'}:
            # Prices on non-trade outcomes are display noise and not entry levels.
            payload.update(entry=None,stop=None,target=None)
    try: return model_type.model_validate(payload)
    except Exception as exc:
        # Report only field paths and Pydantic error codes; never echo model text or values.
        issues=[]
        errors=getattr(exc,'errors',None)
        if callable(errors):
            try:
                issues=[f"{'.'.join(str(part) for part in item.get('loc',())) or '<root>'}:{item.get('type','invalid')}" for item in errors()[:6]]
            except Exception: issues=[]
        suffix=f" ({', '.join(issues)})" if issues else ''
        raise RuntimeError(f'ADK_OUTPUT_INVALID: {stage}; fields did not match the contract{suffix}.') from exc

def _contract_instruction(role_prompt, model_type):
    """Give the model the exact local JSON contract while Python remains authoritative."""
    schema=json.dumps(model_type.model_json_schema(),separators=(',',':'))
    return (role_prompt.strip()+'\n\nOUTPUT CONTRACT (JSON Schema):\n'+schema+
            '\nReturn only one JSON object. Include required fields; use null for unknown optional numeric values. '
            'Do not wrap the object in Markdown fences.')

def _extract_json_object(text, model_type):
    """Find the first complete object matching a role contract in model text."""
    decoder=json.JSONDecoder(); expected=set(model_type.model_fields)
    for index,char in enumerate(text):
        if char!='{': continue
        try: value,_end=decoder.raw_decode(text,index)
        except json.JSONDecodeError: continue
        if not isinstance(value,dict): continue
        if expected.intersection(value): return value
        for key in ('proposal','signal_proposal','signal_decision','report','result','output'):
            nested=value.get(key)
            if isinstance(nested,dict) and expected.intersection(nested): return nested
    return None

def build_agents():
    """Build Parallel analysts -> Sequential orchestrator ADK workflow.

    Each analyst has an output_key; ADK stores its JSON report in session
    state. Exact JSON contracts are included in role prompts and validated
    locally because provider-side schema rejection can abort the full graph.
    """
    if not settings.google_api_key:
        return None
    try:
        from google.adk.agents import Agent, ParallelAgent, SequentialAgent
        from google.genai.types import GenerateContentConfig
    except ImportError as exc:
        raise RuntimeError('google-adk is not installed. Install backend requirements first.') from exc

    # ADK/GenAI reads this standard environment variable. Never log or return it.
    os.environ.setdefault('GOOGLE_API_KEY',settings.google_api_key)
    generation=GenerateContentConfig(temperature=0.1,max_output_tokens=max(128,min(4096,settings.agent_output_tokens)),response_mime_type='application/json')
    model=settings.google_model
    shared=('Use only facts in the supplied timestamped market context and calculate no new values. '
            'Never invent prices, news, indicators, contract metadata, or sources. No broker orders. '
            'Return a single JSON object matching the role contract in your prompt.')
    fundamental=Agent(name='fundamental_analyst',model=model,instruction=_contract_instruction(prompt('fundamental',shared+' Without a reliable sourced current fundamental feed, report insufficient data.'),AgentReport),output_key='fundamental_report',generate_content_config=generation)
    trend=Agent(name='technical_trend_analyst',model=model,instruction=_contract_instruction(prompt('trend',shared+' Analyze trend and structure.'),AgentReport),output_key='trend_report',generate_content_config=generation)
    momentum=Agent(name='technical_momentum_analyst',model=model,instruction=_contract_instruction(prompt('momentum',shared+' Analyze momentum and setup.'),AgentReport),output_key='momentum_report',generate_content_config=generation)
    analyst_team=ParallelAgent(name='analyst_team',description='Run the three independent analysts concurrently and persist their reports to shared session state.',sub_agents=[fundamental,trend,momentum])
    orchestrator_instruction=_contract_instruction(prompt('orchestrator',shared+' Reconcile these specialist reports: fundamental={fundamental_report}; trend={trend_report}; momentum={momentum_report}. Return a SignalProposal JSON. Select NO_TRADE or INSUFFICIENT_DATA unless evidence supports a direction. For BUY/SELL provide entry, stop, target and rationale. Confidence is an uncalibrated model score.'),SignalProposal)
    orchestrator=Agent(name='orchestrator',model=model,instruction=orchestrator_instruction,output_key='signal_decision',generate_content_config=generation)
    return SequentialAgent(name='mcx_signal_orchestration',description='Parallel analyst reports followed by locally schema-validated Orchestrator synthesis.',sub_agents=[analyst_team,orchestrator])

async def _run_async(context: dict):
    """Run a fresh in-memory ADK session and read its named output keys.

    Keep sessions isolated per signal. The context should contain only the
    minimum timestamped market data needed by the four analyst roles.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    root=build_agents()
    if root is None: raise RuntimeError('ADK is not configured.')
    app_name='mcx_crude_ai'; user_id='local_operator'; session_id=uuid.uuid4().hex
    session_service=InMemorySessionService()
    await session_service.create_session(app_name=app_name,user_id=user_id,session_id=session_id)
    runner=Runner(agent=root,app_name=app_name,session_service=session_service)
    message=types.Content(role='user',parts=[types.Part.from_text(text=json.dumps(context,separators=(',',':')))])
    try:
        async for _event in runner.run_async(user_id=user_id,session_id=session_id,new_message=message):
            pass
        session=await session_service.get_session(app_name=app_name,user_id=user_id,session_id=session_id)
    finally:
        await runner.close()
    if session is None: raise RuntimeError('ADK session disappeared before results were read.')
    state=session.state
    reports=[]
    for key,agent_name in [('fundamental_report','fundamental'),('trend_report','trend'),('momentum_report','momentum')]:
        raw=state.get(key)
        if not raw:
            parsed=AgentReport(agent=agent_name,conclusion='Agent report was not returned; treat its evidence as insufficient.',data_status='insufficient')
        else:
            try: parsed=_parse_output(AgentReport,raw,f'{agent_name} report')
            except RuntimeError:
                parsed=AgentReport(agent=agent_name,conclusion='Agent response did not match the report contract; treat its evidence as insufficient.',rationale=['The analyst output was omitted from synthesis after local schema validation.'],data_status='insufficient')
        parsed.agent=agent_name
        reports.append(parsed)
    decision=state.get('signal_decision')
    if not decision: raise RuntimeError('ADK Orchestrator did not provide a signal decision.')
    proposal=_parse_output(SignalProposal,decision,'orchestrator proposal')
    proposal.reports=reports
    if proposal.side not in {Side.BUY,Side.SELL,Side.NO_TRADE,Side.INSUFFICIENT_DATA}:
        raise RuntimeError('ADK Orchestrator returned an unsupported direction.')
    if proposal.side in {Side.BUY,Side.SELL} and any(value is None for value in (proposal.entry,proposal.stop,proposal.target)):
        raise RuntimeError('ADK Orchestrator omitted required entry, stop, or target fields.')
    return proposal

def analyze_with_adk(context: dict) -> SignalProposal:
    """Run one isolated ADK workflow invocation and validate the final schema."""
    try: return asyncio.run(_run_async(context))
    except Exception as exc:
        # Translate provider details to a fixed safe diagnosis before crossing the API boundary.
        raise RuntimeError(f'ADK_ANALYSIS_FAILED: {safe_failure_reason(exc)}') from exc

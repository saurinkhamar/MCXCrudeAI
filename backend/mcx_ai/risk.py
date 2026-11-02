"""Deterministic policy checks shared by model and rule-based proposals.

Do not move these checks into prompts/LLM code: they are the local hard gate.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import settings
from .schemas import SignalProposal, Side
from .storage import configured, db
IST=ZoneInfo('Asia/Kolkata')
def cost(price, quantity):
    """Estimate one-side paper transaction friction from local config rates."""
    return round(max(0,price)*quantity*settings.contract_multiplier*(settings.brokerage_rate+settings.transaction_tax_rate)+settings.fixed_transaction_charges_inr,2)
def validate(proposal: SignalProposal):
    """Return all deterministic reasons a proposed signal is not reviewable."""
    cfg=configured(); errors=[]
    if proposal.side not in (Side.BUY,Side.SELL): return ['No executable direction.']
    if None in (proposal.entry,proposal.stop,proposal.target): errors.append('Entry, stop, and target are required.')
    else:
        distance=abs(proposal.entry-proposal.stop)
        if distance>float(cfg['max_stop_points']): errors.append('Stop distance exceeds configured maximum.')
        if (proposal.side==Side.BUY and not proposal.target>proposal.entry) or (proposal.side==Side.SELL and not proposal.target<proposal.entry): errors.append('Target is not favourable.')
        if abs(proposal.target-proposal.entry)<=distance: errors.append('Gross reward/risk must exceed 1:1.')
    current=datetime.now(IST).strftime('%H:%M')
    if current>=cfg['entry_cutoff']: errors.append('Entry cutoff has passed.')
    with db() as con:
        if con.execute("SELECT 1 FROM positions WHERE status='OPEN'").fetchone(): errors.append('A paper position is already open.')
        loss=con.execute("SELECT COALESCE(SUM(CASE WHEN net_pnl < 0 THEN -net_pnl ELSE 0 END),0) FROM positions WHERE date(closed_at)=date('now','localtime')").fetchone()[0]
        if loss>=float(cfg['max_daily_loss_inr']): errors.append('Daily loss limit reached.')
    return errors

def validate_replay(proposal: SignalProposal, timestamp: str, *, max_stop_points: float, risk_inr: float, multiplier: int, lot_size: int, entry_cutoff: str):
    """Evaluate historical proposal geometry at its replay time, not wall time.

    This deliberately does not read live paper positions or today's loss total.
    Replay recommendations are analytical records, not executed paper trades.
    """
    if proposal.side not in (Side.BUY,Side.SELL):
        return {'eligible':False,'reasons':['No directional trade proposed.'],'stop_distance':None,'suggested_quantity':0,'risk_at_stop_inr':0}
    reasons=[]
    if None in (proposal.entry,proposal.stop,proposal.target):
        return {'eligible':False,'reasons':['Entry, stop, and target are required.'],'stop_distance':None,'suggested_quantity':0,'risk_at_stop_inr':0}
    distance=abs(proposal.entry-proposal.stop)
    if distance>max_stop_points: reasons.append('Stop distance exceeds replay stop cap.')
    if (proposal.side==Side.BUY and (proposal.stop>=proposal.entry or proposal.target<=proposal.entry)) or (proposal.side==Side.SELL and (proposal.stop<=proposal.entry or proposal.target>=proposal.entry)):
        reasons.append('Stop or target is on the wrong side of entry.')
    if abs(proposal.target-proposal.entry)<=distance: reasons.append('Gross reward/risk must exceed 1:1.')
    if timestamp[11:16]>=entry_cutoff: reasons.append('Replay entry time is at or beyond the configured cutoff.')
    one_unit_risk=distance*multiplier
    quantity=(int(risk_inr//one_unit_risk)//lot_size)*lot_size if one_unit_risk>0 else 0
    return {'eligible':not reasons,'reasons':reasons,'stop_distance':round(distance,4),'suggested_quantity':quantity,'risk_at_stop_inr':round(quantity*one_unit_risk,2)}

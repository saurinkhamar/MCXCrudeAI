"""Stable API and ADK data contracts.

Keep these models as the shared boundary between browser requests, persisted
signals, and untrusted model output. Add compatible optional fields where
possible; schema changes may require a SQLite migration for stored rows.
"""
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class Side(str, Enum):
    """Proposal direction; only BUY and SELL can reach paper entry."""
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SignalStatus(str, Enum):
    """Persisted signal lifecycle states used by approval and journal flows."""
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    OPENED = "OPENED"
    CLOSED = "CLOSED"


class AgentReport(BaseModel):
    """Common strict-format summary emitted by each ADK analyst role."""
    agent: str
    conclusion: str
    rationale: list[str] = Field(default_factory=list)
    data_status: str = "available"


class SignalProposal(BaseModel):
    """Model/rule proposal; values remain untrusted until Python risk checks."""
    side: Side = Side.NO_TRADE
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    confidence: float = Field(default=0, ge=0, le=100)
    rationale: list[str] = Field(default_factory=list)
    reports: list[AgentReport] = Field(default_factory=list)


class Approval(BaseModel):
    """Human decision payload recorded against a draft signal."""
    approved: bool
    note: str = ""


class PaperEntry(BaseModel):
    """Operator-entered paper fill after a signal has been approved."""
    signal_id: int
    entry_price: float
    quantity: int = Field(gt=0)
    note: str = ""


class PaperExit(BaseModel):
    """Operator-entered paper exit fill and optional journal note."""
    exit_price: float
    note: str = ""


class SettingsUpdate(BaseModel):
    """Editable deterministic risk and signal-lifecycle settings."""
    max_stop_points: float = Field(gt=0)
    risk_per_trade_inr: float = Field(gt=0)
    max_daily_loss_inr: float = Field(gt=0)
    contract_multiplier: int = Field(gt=0)
    entry_cutoff: str
    exit_cutoff: str
    signal_ttl_minutes: int = Field(gt=0)


class BacktestRequest(BaseModel):
    """CSV replay request; all execution assumptions are explicit inputs."""
    csv_text: str = Field(min_length=1)
    start: str | None = None
    end: str | None = None
    strategy: str = "ema_rsi"
    risk_inr: float = Field(default=2000, gt=0)
    max_stop_points: float = Field(default=100, gt=0)
    multiplier: int = Field(default=10, gt=0)
    lot_size: int = Field(default=1, gt=0)
    brokerage_rate: float = Field(default=.0003, ge=0)
    tax_rate: float = Field(default=.0002, ge=0)
    slippage_points: float = Field(default=.5, ge=0)


class ReplayStartRequest(BaseModel):
    """Start a candle-by-candle historical analysis session from a CSV file."""
    csv_text: str = Field(min_length=1, max_length=8_000_000)
    start_at: datetime
    interval_minutes: int = Field(default=5, ge=1, le=300)
    risk_inr: float = Field(default=2000, gt=0)
    max_stop_points: float = Field(default=100, gt=0)
    multiplier: int = Field(default=10, gt=0)
    lot_size: int = Field(default=1, gt=0)


class MarketContext(BaseModel):
    """Typed live quote/status payload for callers that need a fixed contract."""
    instrument_key: str
    symbol: str | None = None
    last_price: float | None = None
    timestamp: str | None = None
    stale: bool
    market_open: bool
    validation: str

"""Typed backend settings loaded from environment and backend/.env.

Never log the Settings instance: it includes broker and model credentials.
Defaults here are local research assumptions and should be validated before use.
"""
from pathlib import Path
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    """Environment-backed configuration shared by API services."""
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_access_token: str = ""
    upstox_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    upstox_instrument_key: str = "MCX_FO|576265"
    candle_interval_minutes: int = 5
    google_api_key: str = ""
    google_model: str = "gemini-3.5-flash-lite"
    agent_mode: str = "auto"  # auto selects ADK only when a Google API key exists
    agent_output_tokens: int = 700
    fundamental_feed_urls: str = "https://www.eia.gov/rss/todayinenergy.xml,https://www.eia.gov/rss/press_rss.xml"
    fundamental_refresh_minutes: int = 60
    fundamental_max_items: int = 12
    db_path: str = str(BASE_DIR.parent / "data" / "mcx_crude_ai.db")
    max_stop_points: float = 100
    risk_per_trade_inr: float = 2000
    max_daily_loss_inr: float = 5000
    contract_multiplier: int = 10
    entry_cutoff: str = "22:30"
    exit_cutoff: str = "23:00"
    signal_ttl_minutes: int = 10
    scheduler_enabled: bool = False
    scheduler_interval_minutes: int = 5
    brokerage_rate: float = 0.0003
    transaction_tax_rate: float = 0.0002
    fixed_transaction_charges_inr: float = 40

settings = Settings()

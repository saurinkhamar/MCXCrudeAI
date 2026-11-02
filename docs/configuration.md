# Configuration reference

Secrets stay in `backend/.env`, which must never be copied into delivery archives or shared. `.env.example` contains placeholders only. Existing `.env` and `.venv` are preserved by setup.

For Google ADK, put the Gemini API key on the `GOOGLE_API_KEY` line in `backend/.env`. That is the correct location for this local backend; do not copy it into frontend configuration. `AGENT_MODE=auto` selects ADK when the key is loaded and otherwise uses deterministic analysis. Restart the backend after editing `.env`, because settings are read when the Python process starts. The dashboard's Analysis configuration card and `GET /api/agents/status` confirm whether a key is loaded without displaying it.

| Setting | Purpose |
|---|---|
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` | Legacy local OAuth login configuration |
| `UPSTOX_ACCESS_TOKEN` | Current Upstox market data token; token status is exposed without its value |
| `UPSTOX_REDIRECT_URI` | OAuth callback (legacy service, port 8000) |
| `UPSTOX_INSTRUMENT_KEY` | Requested contract key; validate contract metadata, expiry, lot and tick size independently |
| `CANDLE_INTERVAL_MINUTES` | Candle interval used for live analysis, default 5 |
| `GOOGLE_API_KEY`, `GOOGLE_MODEL` | Optional separately provisioned Gemini API access and model; not needed for deterministic local analysis |
| `AGENT_MODE` | `auto` uses ADK when a key exists, `adk` requires it, `deterministic` disables model calls |
| `AGENT_OUTPUT_TOKENS` | ADK response budget (clamped by the application to 128–4096 tokens) |
| `FUNDAMENTAL_FEED_URLS` | Comma-separated RSS/Atom feed URLs; defaults to EIA Today in Energy and EIA Press Releases |
| `FUNDAMENTAL_REFRESH_MINUTES` | Refresh period for the persistent fundamentals cache; default 60 minutes |
| `FUNDAMENTAL_MAX_ITEMS` | Maximum recent relevant feed items supplied to the Fundamental Analyst; default 12 |
| `MAX_STOP_POINTS` | Maximum stop distance in crude price points, default 100 |
| `RISK_PER_TRADE_INR`, `MAX_DAILY_LOSS_INR` | Monetary risk budget and daily loss guard defaults |
| `CONTRACT_MULTIPLIER` | P&L multiplier; validate against the active contract |
| `SCHEDULER_ENABLED` | Enable periodic local signal drafts; defaults to false |
| `SCHEDULER_INTERVAL_MINUTES` | Draft cadence in minutes, default 5. If ADK is selected, each cycle can make four billable model calls. |
| `BROKERAGE_RATE`, `TRANSACTION_TAX_RATE`, `FIXED_TRANSACTION_CHARGES_INR` | Estimated paper-trade round-trip cost inputs |

Other editable runtime defaults live in `backend/mcx_ai/config.py` and the SQLite settings table. Agent prompt text is versioned under `backend/mcx_ai/prompts/`; preserve the JSON report contract described in each prompt. Never put API keys in frontend variables or source control.

The current one-minute validation setup uses `SCHEDULER_ENABLED=true`, `SCHEDULER_INTERVAL_MINUTES=1`, and `CANDLE_INTERVAL_MINUTES=1` in the private `backend/.env`. Restart the backend after changing these values. The first scheduled attempt occurs after one interval; successful drafts appear in Realtime with their creation timestamp. Market-closed and stale-data checks still apply. Set the scheduler back to `false` when validation is complete, or set both intervals to `5` to resume five-minute reviews.

Live fundamentals use the public [EIA RSS feeds](https://www.eia.gov/tools/rssfeeds/). The default reader polls EIA's **Today in Energy** and **Press Releases** feeds, filters for crude/petroleum/oil-related entries, and stores source, publication timestamp, summary, and URL in SQLite. The cache refreshes hourly by default and is reused between refreshes and after transient feed failures. If no relevant items are available yet, or all feeds fail before a first successful fetch, the analyst correctly reports insufficient data. Refresh frequency is only a polling/cache setting; EIA may publish new material less often than hourly.

# Trading policy

This application is a local paper-trading research tool, not a broker execution system. It has no order-placement endpoint or credentials for one.

- One paper position may be open at a time.
- A signal begins as a draft, expires after its configured TTL, and needs a recorded human approval before manual paper entry.
- The Python risk gate rejects late entries, excessive stop distance, unfavourable targets, a second position, and daily-loss breaches.
- Costs are an editable conservative estimator, not a broker contract note.
- Positions are intraday by policy. The operator must manually exit before the configured cutoff.
- Signal confidence is an uncalibrated model/rule score, not a probability. Paper results are not a forecast of profit.

The initial instrument key is deliberately displayed as unverified. Validate its contract name, expiry, lot size and tradability through current Upstox metadata before use.

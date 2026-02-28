# Polymarket Bot

Systematic prediction market trading using probability estimation and EV analysis.

## Strategy

1. **Longshot bias** — Retail overpays for low-probability YES positions. We sell them.
2. **Macro event pricing** — Fed/CPI/GDP markets using actual economic models.
3. **Crypto price markets** — Log-normal vol model for BTC/ETH price targets.
4. **Near-resolution arb** — Pricing inefficiency in final days before resolution.

## Usage

```bash
pip install -r requirements.txt

# Scan all markets
cd src && python scanner.py

# Crypto markets only
python scanner.py --category crypto

# Higher EV threshold (more selective)
python scanner.py --min-ev 0.07

# Show top 20
python scanner.py --top 20
```

## Phase 1 — Paper Trading (now)
Run the scanner daily. Log flagged opportunities. Track resolution.
After 50+ resolved markets, measure actual vs predicted calibration.

## Phase 2 — Live Trading (after validation)
- Fund a Polygon wallet with USDC
- Wire up py-clob-client for order execution
- Max 5% bankroll per market, 25% fractional Kelly

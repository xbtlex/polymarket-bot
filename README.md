# Polymarket Autonomous Bot

Fully autonomous prediction market trader with EV-based edge detection,
Kelly criterion sizing, and autonomous execution via Polymarket CLOB API.

## Strategy

| Edge | Description |
|------|-------------|
| Longshot bias | Retail pays 2-3x fair value for low-prob YES. We sell. |
| Macro model | Fed/CPI/recession markets priced with economic model |
| BTC vol model | Log-normal binary option pricing for BTC price markets |
| Near-resolution arb | Pricing inefficiency in final days before close |

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Telegram tokens

cd src

# Paper mode (no money, tracks hypothetical P&L)
python bot.py --paper

# Check status
python bot.py --status

# Calibration report
python bot.py --calibration
```

## Live Mode Setup

1. Create a Polygon wallet
2. Fund with USDC (start small — $200-500)
3. Visit polymarket.com and connect wallet to approve contracts
4. Add to `.env`:
   ```
   POLYMARKET_PRIVATE_KEY=0x...your_private_key
   STARTING_BANKROLL=500
   ```
5. Install live deps:
   ```bash
   pip install py-clob-client web3
   ```
6. Run:
   ```bash
   python bot.py --live --bankroll 500
   ```

## Risk Parameters

Edit `bankroll_manager.py`:
- `MAX_SINGLE_BET_PCT = 5%` — max 5% of bankroll per market
- `MAX_TOTAL_EXPOSURE = 40%` — max 40% deployed at once
- `KELLY_FRACTION = 25%` — fractional Kelly (conservative)
- `MIN_EV = 4%` — minimum edge to bet

## Paper → Live Migration Path

1. Run paper mode for 2-4 weeks
2. Check calibration: `python bot.py --calibration`
3. If ROI > 0 on 30+ resolved bets → go live with $200-500
4. Scale up only after 50+ resolved bets with positive ROI

## Architecture

```
bot.py              ← Main autonomous loop
scanner.py          ← Market discovery + EV ranking
probability_engine.py ← True probability estimation
btc_vol_model.py    ← Log-normal BTC price market pricer
bankroll_manager.py ← Kelly sizing + risk limits
executor.py         ← Polymarket CLOB order execution
position_monitor.py ← Resolution tracking + P&L
paper_tracker.py    ← SQLite calibration database
telegram_alerts.py  ← Alerts for all activity
```

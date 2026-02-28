# -*- coding: utf-8 -*-
"""
Polymarket Autonomous Bot
=========================
Fully autonomous prediction market trader.

Pipeline (runs every SCAN_INTERVAL seconds):
  1. Scan all active markets for mispricings
  2. Estimate true probability (vol model + bias corrections + macro)
  3. Size bets via fractional Kelly bankroll management
  4. Execute approved bets via Polymarket CLOB
  5. Monitor open positions for resolution
  6. Send Telegram alerts for all activity
  7. Track performance and calibration

Modes:
  PAPER  — Logs bets, tracks hypothetical P&L, no real money
  LIVE   — Real execution via Polygon wallet USDC

Usage:
  python bot.py --paper           # Paper mode (default)
  python bot.py --live            # Live mode (requires wallet)
  python bot.py --status          # Show current status
  python bot.py --calibration     # Show calibration report

Setup for live mode:
  1. Fund Polygon wallet with USDC
  2. Set POLYMARKET_PRIVATE_KEY in .env
  3. pip install py-clob-client web3
  4. python bot.py --live

Risk parameters (edit bankroll_manager.py):
  MAX_SINGLE_BET_PCT = 5%   (max per market)
  MAX_TOTAL_EXPOSURE = 40%  (max deployed at once)
  KELLY_FRACTION     = 25%  (fractional Kelly)
  MIN_EV             = 4%   (minimum edge to bet)
"""

import asyncio
import argparse
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from market_fetcher import PolymarketFetcher
from probability_engine import ProbabilityEngine
from btc_vol_model import BTCVolModel
from bankroll_manager import BankrollManager
from executor import PolymarketExecutor
from paper_tracker import PaperTracker, PaperBet
from position_monitor import PositionMonitor
from telegram_alerts import TelegramAlerter
from scanner import PolymarketScanner

# ── Config ────────────────────────────────────────────────────────────────────

SCAN_INTERVAL       = 3600      # Scan every hour (markets don't move that fast)
MONITOR_INTERVAL    = 300       # Check resolutions every 5 minutes
STARTING_BANKROLL   = float(os.getenv("STARTING_BANKROLL", "500"))  # Default $500
MIN_EV              = 0.04      # 4% minimum EV
TOP_BETS_PER_SCAN   = 3        # Max new bets per scan cycle

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Bot ───────────────────────────────────────────────────────────────────────

class PolymarketBot:
    """Fully autonomous Polymarket trading bot."""

    def __init__(self, live_mode: bool = False):
        self.live_mode   = live_mode
        self.mode_label  = "🔴 LIVE" if live_mode else "📄 PAPER"

        self.scanner     = PolymarketScanner()
        self.tracker     = PaperTracker()
        self.alerter     = TelegramAlerter(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        self.executor    = PolymarketExecutor()
        self.bankroll_mgr = BankrollManager(bankroll_usd=STARTING_BANKROLL)
        self.monitor     = PositionMonitor(
            tracker=self.tracker,
            alerter=self.alerter,
            live_mode=live_mode,
            poll_interval=MONITOR_INTERVAL,
        )

        self._running    = False
        self._scan_count = 0
        self._bets_placed = 0
        self._total_pnl  = 0.0

    async def start(self):
        """Start the autonomous bot."""
        logger.info(f"Starting Polymarket Bot [{self.mode_label}]")

        # Validate live setup
        if self.live_mode:
            if not self.executor.is_configured():
                raise RuntimeError(
                    "Live mode requires POLYMARKET_PRIVATE_KEY and py-clob-client installed.\n"
                    "Run: pip install py-clob-client web3\n"
                    "Set: POLYMARKET_PRIVATE_KEY in .env"
                )
            balance = await self.executor.get_balance()
            logger.info(f"Wallet USDC balance: ${balance:.2f}")
            self.bankroll_mgr.update_bankroll(balance)

        # Send startup alert
        await self.alerter.send(
            f"🚀 <b>Polymarket Bot Started</b>\n\n"
            f"Mode: {self.mode_label}\n"
            f"Bankroll: ${self.bankroll_mgr.bankroll:.2f}\n"
            f"Min EV: {MIN_EV:.0%}\n"
            f"Scan interval: {SCAN_INTERVAL//60}min\n\n"
            f"Bot is now autonomous. Scanning for mispricings."
        )

        self._running = True

        # Run monitor in background
        monitor_task = asyncio.create_task(self.monitor.start())

        # Main scan loop
        try:
            while self._running:
                await self.scan_cycle()
                logger.info(f"Next scan in {SCAN_INTERVAL//60} minutes...")
                await asyncio.sleep(SCAN_INTERVAL)
        except asyncio.CancelledError:
            pass
        finally:
            monitor_task.cancel()
            await self.shutdown()

    async def scan_cycle(self):
        """Single scan cycle — find and execute opportunities."""
        self._scan_count += 1
        logger.info(f"=== SCAN CYCLE #{self._scan_count} [{self.mode_label}] ===")

        # Update live bankroll
        if self.live_mode:
            balance = await self.executor.get_balance()
            self.bankroll_mgr.update_bankroll(balance)
            open_bets = self.tracker.get_open_bets()
            exposure  = sum(b.get("size_usd", 0) for b in open_bets)
            self.bankroll_mgr.update_exposure(exposure)

        # Get current BTC price for vol model
        btc_price = await self._get_btc_price()
        btc_model = BTCVolModel(current_btc_price=btc_price, btc_regime="mixed")

        # Scan for opportunities
        opportunities = await self.scanner.scan(limit=100, min_ev=MIN_EV)
        logger.info(f"Found {len(opportunities)} opportunities")

        if not opportunities:
            logger.info("No opportunities this scan. Market looks efficient.")
            return

        # Enhance BTC markets with vol model
        for opp in opportunities:
            m = opp.market
            if any(w in m.question.lower() for w in ["bitcoin", "btc"]) and m.end_date:
                analysis = btc_model.analyze_market(m.question, m.market.yes_price, m.end_date)
                if analysis and abs(analysis["edge"]) > MIN_EV:
                    opp.our_probability = analysis["our_probability"]
                    opp.edge = analysis["edge"]
                    opp.reasoning = analysis["reasoning"]

        # Filter and sort
        opportunities.sort(key=lambda x: max(x.kelly_yes, x.kelly_no), reverse=True)
        top_opps = opportunities[:TOP_BETS_PER_SCAN]

        # Send alert
        await self.alerter.send_opportunity_alert(top_opps, top_n=TOP_BETS_PER_SCAN)

        # Execute bets
        bets_this_cycle = 0
        for opp in top_opps:
            if bets_this_cycle >= TOP_BETS_PER_SCAN:
                break

            m     = opp.market
            side  = opp.recommended_side
            ev    = opp.ev_yes if side == "YES" else opp.ev_no
            kelly = opp.kelly_yes if side == "YES" else opp.kelly_no
            price = m.yes_price if side == "YES" else m.no_price

            # Size the bet
            sizing = self.bankroll_mgr.size_bet(
                ev=ev,
                kelly=kelly,
                confidence=opp.confidence,
                market_liquidity=m.liquidity,
                question=m.question,
            )

            if not sizing.approved:
                logger.info(f"Skipping '{m.question[:50]}': {sizing.rejection_reason}")
                continue

            # Execute or paper log
            if self.live_mode:
                result = await self._execute_live(opp, sizing.bet_size_usd, side)
            else:
                result = await self._execute_paper(opp, sizing.bet_size_usd, side)

            if result:
                bets_this_cycle += 1
                self._bets_placed += 1
                logger.info(
                    f"{'LIVE BET' if self.live_mode else 'PAPER BET'}: "
                    f"{side} on '{m.question[:50]}' "
                    f"@ {price:.1%} | ${sizing.bet_size_usd:.2f} | EV={ev:+.1%}"
                )

        if bets_this_cycle == 0:
            logger.info("No bets placed this cycle — all rejected by bankroll manager")

    async def _execute_paper(self, opp, size_usd: float, side: str) -> bool:
        """Log a paper bet."""
        m     = opp.market
        price = m.yes_price if side == "YES" else m.no_price

        bet = PaperBet(
            market_id=m.market_id,
            question=m.question,
            category=m.category,
            side=side,
            market_price=price,
            our_probability=opp.our_probability,
            ev=opp.ev_yes if side == "YES" else opp.ev_no,
            kelly=opp.kelly_yes if side == "YES" else opp.kelly_no,
            confidence=opp.confidence,
            reasoning=opp.reasoning,
            flagged_at=datetime.now(timezone.utc),
            end_date=m.end_date,
        )
        bet_id = self.tracker.log_bet(bet)

        await self.alerter.send(
            f"📝 <b>PAPER BET PLACED</b>\n\n"
            f"{m.question[:80]}\n\n"
            f"Side: <b>{side}</b> @ {price:.1%}\n"
            f"Size: <b>${size_usd:.2f}</b>\n"
            f"Our prob: {opp.our_probability:.1%} | EV: {(opp.ev_yes if side=='YES' else opp.ev_no):+.1%}\n"
            f"Confidence: {opp.confidence}\n"
            f"<i>{opp.reasoning[:120]}</i>"
        )
        return True

    async def _execute_live(self, opp, size_usd: float, side: str) -> bool:
        """Execute a live bet."""
        m     = opp.market
        price = m.yes_price if side == "YES" else m.no_price

        # Get token ID for the side we want
        # This requires the full market data with token IDs
        # For now we use the market_id — in production get the specific token_id
        token_id = m.market_id  # Simplified — real impl needs YES/NO token IDs

        if side == "YES":
            result = await self.executor.buy_yes(token_id, size_usd, price)
        else:
            result = await self.executor.buy_no(token_id, size_usd, price)

        if result.success:
            # Log to tracker
            bet = PaperBet(
                market_id=m.market_id,
                question=m.question,
                category=m.category,
                side=side,
                market_price=result.filled_price,
                our_probability=opp.our_probability,
                ev=opp.ev_yes if side == "YES" else opp.ev_no,
                kelly=opp.kelly_yes if side == "YES" else opp.kelly_no,
                confidence=opp.confidence,
                reasoning=opp.reasoning,
                flagged_at=datetime.now(timezone.utc),
                end_date=m.end_date,
            )
            self.tracker.log_bet(bet)

            await self.alerter.send(
                f"🎯 <b>LIVE BET EXECUTED</b>\n\n"
                f"{m.question[:80]}\n\n"
                f"Side: <b>{side}</b> @ {result.filled_price:.1%}\n"
                f"Size: <b>${size_usd:.2f}</b>\n"
                f"Order ID: {result.order_id}\n"
                f"Our prob: {opp.our_probability:.1%} | EV: {(opp.ev_yes if side=='YES' else opp.ev_no):+.1%}"
            )
            return True
        else:
            logger.error(f"Execution failed: {result.error}")
            await self.alerter.send(f"⚠️ <b>Execution failed</b>\n{result.error}")
            return False

    async def _get_btc_price(self) -> float:
        """Get current BTC price from CoinGecko."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin", "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    return float(data["bitcoin"]["usd"])
        except Exception:
            return 65_900  # Fallback

    async def shutdown(self):
        """Clean shutdown."""
        self._running = False
        self.monitor.stop()
        await self.scanner.close()

        report = self.tracker.get_calibration_report()
        pnl    = report.get("total_pnl_usd", 0) if "error" not in report else 0

        await self.alerter.send(
            f"🛑 <b>Polymarket Bot Stopped</b>\n\n"
            f"Total scans: {self._scan_count}\n"
            f"Total bets: {self._bets_placed}\n"
            f"Hypothetical P&L: ${pnl:+.2f}"
        )
        logger.info("Bot shut down cleanly")

    def print_status(self):
        """Print current status."""
        bm = self.bankroll_mgr.get_status()
        self.tracker.print_status()
        print(f"\n  Bankroll:    ${bm['bankroll']:.2f}")
        print(f"  Exposure:    ${bm['open_exposure']:.2f} ({bm['exposure_pct']:.1%})")
        print(f"  Remaining:   ${bm['remaining_capacity']:.2f}")
        print(f"  Scan count:  {self._scan_count}")
        print(f"  Bets placed: {self._bets_placed}")


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Polymarket Autonomous Bot")
    parser.add_argument("--live",        action="store_true", help="Live trading mode")
    parser.add_argument("--paper",       action="store_true", help="Paper mode (default)")
    parser.add_argument("--status",      action="store_true", help="Show status and exit")
    parser.add_argument("--calibration", action="store_true", help="Show calibration report")
    parser.add_argument("--bankroll",    type=float, default=None, help="Override starting bankroll")
    args = parser.parse_args()

    if args.bankroll:
        global STARTING_BANKROLL
        STARTING_BANKROLL = args.bankroll

    live_mode = args.live and not args.paper
    bot = PolymarketBot(live_mode=live_mode)

    if args.status:
        bot.print_status()
        return

    if args.calibration:
        report = bot.tracker.get_calibration_report()
        if "error" in report:
            print(f"\n{report['error']}\n")
        else:
            print(f"\nResolved: {report['total_resolved']} | WR: {report['win_rate']:.1%} | "
                  f"P&L: ${report['total_pnl_usd']:+.2f} | ROI: {report['roi_pct']:+.1f}%\n")
        return

    # Handle CTRL+C gracefully
    loop = asyncio.get_event_loop()
    def shutdown_handler():
        bot._running = False
    loop.add_signal_handler(signal.SIGINT, shutdown_handler)
    loop.add_signal_handler(signal.SIGTERM, shutdown_handler)

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())

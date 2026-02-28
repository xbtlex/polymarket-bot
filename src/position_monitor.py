# -*- coding: utf-8 -*-
"""
Position Monitor
================
Monitors open positions and handles resolution.

Polls Polymarket API periodically to check if markets have resolved.
When resolved:
  1. Records outcome in paper tracker (or live DB)
  2. Calculates P&L
  3. Sends Telegram alert
  4. Updates bankroll
"""

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from loguru import logger

from market_fetcher import PolymarketFetcher
from paper_tracker import PaperTracker
from telegram_alerts import TelegramAlerter


@dataclass
class OpenPosition:
    row_id: int
    market_id: str
    question: str
    side: str
    entry_price: float
    size_usd: float
    our_probability: float
    end_date: Optional[str]
    flagged_at: str


class PositionMonitor:
    """Monitors open positions and resolves them when markets close."""

    def __init__(
        self,
        tracker: PaperTracker,
        alerter: TelegramAlerter,
        live_mode: bool = False,
        poll_interval: int = 300,  # Check every 5 minutes
    ):
        self.fetcher       = PolymarketFetcher()
        self.tracker       = tracker
        self.alerter       = alerter
        self.live_mode     = live_mode
        self.poll_interval = poll_interval
        self._running      = False

    async def start(self):
        """Start monitoring loop."""
        self._running = True
        logger.info(f"Position monitor started (polling every {self.poll_interval}s)")
        while self._running:
            try:
                await self.check_resolutions()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False

    async def check_resolutions(self):
        """Check all open positions for resolution."""
        open_bets = self.tracker.get_open_bets()
        if not open_bets:
            return

        logger.debug(f"Checking {len(open_bets)} open positions for resolution...")

        resolved_count = 0
        for bet in open_bets:
            market_id = bet["market_id"]
            try:
                # Fetch current market state
                markets = await self.fetcher.get_active_markets(limit=1)

                # Check if this specific market resolved
                # Try direct lookup by condition ID
                outcome = await self._check_market_resolution(market_id)

                if outcome:
                    # Market resolved — record it
                    self.tracker.resolve_bet(market_id, outcome)
                    resolved_count += 1

                    # Calculate P&L
                    side  = bet["side"]
                    price = bet["entry_price"]
                    won   = (side == outcome)

                    if self.live_mode:
                        pnl = (1.0 / price - 1.0) * bet.get("size_usd", 100) if won else -bet.get("size_usd", 100)
                    else:
                        pnl = (1.0 / price - 1.0) * 100 if won else -100

                    result_emoji = "✅" if won else "❌"
                    msg = (
                        f"{result_emoji} <b>POLYMARKET RESOLVED</b>\n\n"
                        f"{bet['question'][:80]}\n\n"
                        f"Our bet: <b>{side}</b> @ {price:.1%}\n"
                        f"Outcome: <b>{outcome}</b>\n"
                        f"Result: <b>{'WIN' if won else 'LOSS'}</b>\n"
                        f"P&L: <b>${pnl:+.2f}</b>"
                    )
                    await self.alerter.send(msg)
                    logger.info(f"Resolved {market_id}: {outcome} → {'WIN' if won else 'LOSS'} ${pnl:+.2f}")

            except Exception as e:
                logger.debug(f"Error checking {market_id}: {e}")

        if resolved_count:
            logger.info(f"Resolved {resolved_count} positions")

            # Send updated calibration if we have enough data
            report = self.tracker.get_calibration_report()
            if "error" not in report and report["total_resolved"] % 10 == 0:
                await self.alerter.send_calibration_report(report)

    async def _check_market_resolution(self, market_id: str) -> Optional[str]:
        """
        Check if a specific market has resolved.
        Returns "YES", "NO", or None if still open.
        """
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://gamma-api.polymarket.com/markets/{market_id}",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

                    if data.get("closed") or data.get("resolved"):
                        outcome = data.get("resolution") or data.get("outcome")
                        if outcome in ["YES", "NO", "Yes", "No"]:
                            return outcome.upper()
        except Exception:
            pass
        return None

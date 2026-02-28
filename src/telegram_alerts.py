# -*- coding: utf-8 -*-
"""
Telegram Alerts
===============
Sends Polymarket opportunity alerts to Telegram.
Uses the same bot token as the trading bot.

Config: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
"""

import aiohttp
import os
from datetime import datetime
from typing import List, Optional
from loguru import logger
from market_fetcher import MarketMispricing


class TelegramAlerter:

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID", "")
        self.base_url  = f"https://api.telegram.org/bot{self.bot_token}"

    async def send(self, message: str):
        """Send a Telegram message."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            return

        async with aiohttp.ClientSession() as session:
            try:
                await session.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                )
            except Exception as e:
                logger.error(f"Telegram send failed: {e}")

    async def send_opportunity_alert(self, opportunities: List[MarketMispricing], top_n: int = 5):
        """Send formatted opportunity alert."""
        if not opportunities:
            return

        now = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
        lines = [
            f"🎯 <b>POLYMARKET OPPORTUNITIES — {now}</b>",
            f"Found <b>{len(opportunities)}</b> mispricings\n",
        ]

        for i, opp in enumerate(opportunities[:top_n], 1):
            m = opp.market
            conf_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(opp.confidence, "⚪")
            side_emoji = "✅" if opp.recommended_side == "YES" else "❌"

            days_left = ""
            if m.end_date:
                from probability_engine import ProbabilityEngine
                d = ProbabilityEngine()._days_to_end(m.end_date)
                if d:
                    days_left = f" | {d:.0f}d"

            lines.append(
                f"{conf_emoji} <b>#{i} {opp.confidence} CONFIDENCE</b>{days_left}\n"
                f"{m.question[:80]}\n"
                f"Market: YES={m.yes_price:.0%} | Our est: {opp.our_probability:.0%}\n"
                f"EV: {max(opp.ev_yes, opp.ev_no):+.1%} | Kelly: {max(opp.kelly_yes, opp.kelly_no):.1%}\n"
                f"{side_emoji} <b>BET {opp.recommended_side}</b>\n"
                f"<i>{opp.reasoning[:100]}</i>\n"
            )

        lines.append("⚠️ Paper mode — track don't bet yet")
        await self.send("\n".join(lines))

    async def send_daily_summary(self, open_count: int, resolved_count: int, total_pnl: float):
        """Send daily paper trading summary."""
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        msg = (
            f"📊 <b>POLYMARKET DAILY SUMMARY</b>\n\n"
            f"Open positions: {open_count}\n"
            f"Resolved total: {resolved_count}\n"
            f"{pnl_emoji} Hypothetical P&L: <b>${total_pnl:+.2f}</b>\n\n"
            f"Run /polymarket for full report"
        )
        await self.send(msg)

    async def send_calibration_report(self, report: dict):
        """Send calibration report."""
        if "error" in report:
            await self.send(f"📊 <b>Polymarket Calibration</b>\n{report['error']}")
            return

        ready = "✅ READY FOR LIVE" if report["ready_for_live"] else "❌ Need more data"
        msg = (
            f"📊 <b>POLYMARKET CALIBRATION REPORT</b>\n\n"
            f"Resolved bets: {report['total_resolved']}\n"
            f"Win rate: {report['win_rate']:.1%}\n"
            f"Total P&L: ${report['total_pnl_usd']:+.2f} (hypothetical $100/bet)\n"
            f"ROI: {report['roi_pct']:+.1f}%\n"
            f"High confidence WR: {report['high_confidence_wr']:.1%}\n\n"
            f"{ready}"
        )
        await self.send(msg)

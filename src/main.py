# -*- coding: utf-8 -*-
"""
Polymarket Bot — Main Runner
============================
Runs the full pipeline:
1. Fetch live markets
2. Estimate true probabilities (BTC vol model + bias corrections)
3. Filter for positive EV
4. Log to paper tracker
5. Send Telegram alerts

Usage:
    python main.py              # Full scan + alert
    python main.py --status     # Show paper tracker status
    python main.py --calibrate  # Show calibration report
    python main.py --demo       # Demo BTC vol model

Schedule: Run this daily or via cron/task scheduler
"""

import asyncio
import argparse
import os
from datetime import datetime, timezone
from loguru import logger
from pathlib import Path

from market_fetcher import PolymarketFetcher, MarketMispricing
from probability_engine import ProbabilityEngine
from btc_vol_model import BTCVolModel
from paper_tracker import PaperTracker, PaperBet
from telegram_alerts import TelegramAlerter
from scanner import PolymarketScanner

# ── Config ────────────────────────────────────────────────────────────────────

MIN_EV              = 0.03     # 3% minimum EV
MIN_CONFIDENCE      = "MEDIUM" # MEDIUM or HIGH only
BET_SIZE_USD        = 100.0    # Hypothetical $100 per paper bet
TELEGRAM_BOT_TOKEN  = os.getenv("POLYMARKET_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("POLYMARKET_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")

# ── Main Pipeline ─────────────────────────────────────────────────────────────

async def run_scan(send_telegram: bool = True):
    """Full scan pipeline."""
    logger.info("Starting Polymarket scan pipeline...")

    scanner  = PolymarketScanner()
    tracker  = PaperTracker()
    alerter  = TelegramAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    btc_model = BTCVolModel(current_btc_price=65_900, btc_regime="mixed")

    try:
        # 1. Scan for opportunities
        opportunities = await scanner.scan(limit=100, min_ev=MIN_EV)

        # 2. Enhance BTC price markets with vol model
        enhanced = []
        for opp in opportunities:
            m = opp.market
            if any(w in m.question.lower() for w in ["bitcoin", "btc"]) and m.end_date:
                analysis = btc_model.analyze_market(m.question, m.market.yes_price, m.end_date)
                if analysis and abs(analysis["edge"]) > MIN_EV:
                    # Override with more accurate vol model estimate
                    opp.our_probability = analysis["our_probability"]
                    opp.edge = analysis["edge"]
                    opp.reasoning = analysis["reasoning"]
                    logger.info(f"BTC vol model override: {m.question[:50]} → {analysis['our_probability']:.1%}")
            enhanced.append(opp)

        # Re-filter after enhancement
        final_opps = [o for o in enhanced if abs(o.ev_yes if o.recommended_side == "YES" else o.ev_no) >= MIN_EV]
        final_opps.sort(key=lambda x: max(x.kelly_yes, x.kelly_no), reverse=True)

        # 3. Print report
        scanner.print_report(final_opps, top_n=10)

        # 4. Log to paper tracker
        logged = 0
        for opp in final_opps:
            if opp.confidence not in ["HIGH", "MEDIUM"]:
                continue
            bet = PaperBet(
                market_id=opp.market.market_id,
                question=opp.market.question,
                category=opp.market.category,
                side=opp.recommended_side,
                market_price=opp.market.yes_price if opp.recommended_side == "YES" else opp.market.no_price,
                our_probability=opp.our_probability,
                ev=opp.ev_yes if opp.recommended_side == "YES" else opp.ev_no,
                kelly=opp.kelly_yes if opp.recommended_side == "YES" else opp.kelly_no,
                confidence=opp.confidence,
                reasoning=opp.reasoning,
                flagged_at=datetime.now(timezone.utc),
                end_date=opp.market.end_date,
            )
            tracker.log_bet(bet)
            logged += 1

        logger.info(f"Logged {logged} new paper bets")

        # 5. Send Telegram alert
        if send_telegram and final_opps:
            high_conf = [o for o in final_opps if o.confidence == "HIGH"]
            all_opps  = high_conf if high_conf else final_opps
            await alerter.send_opportunity_alert(all_opps[:5])
            logger.info(f"Sent Telegram alert ({len(all_opps[:5])} opportunities)")

        # 6. Print paper tracker status
        tracker.print_status()

    finally:
        await scanner.close()


async def show_status():
    """Show paper tracker status only."""
    tracker = PaperTracker()
    tracker.print_status()


async def show_calibration():
    """Show full calibration report."""
    tracker = PaperTracker()
    report  = tracker.get_calibration_report()
    alerter = TelegramAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if "error" in report:
        print(f"\n{report['error']}\n")
        return

    print("\n" + "=" * 55)
    print("  CALIBRATION REPORT")
    print("=" * 55)
    print(f"  Resolved bets:      {report['total_resolved']}")
    print(f"  Win rate:           {report['win_rate']:.1%}")
    print(f"  Total P&L:          ${report['total_pnl_usd']:+.2f} (hypothetical $100/bet)")
    print(f"  ROI:                {report['roi_pct']:+.1f}%")
    print(f"  High confidence WR: {report['high_confidence_wr']:.1%}")
    print()
    print("  CALIBRATION BY BUCKET")
    print("  (Good calibration = actual ≈ estimated)")
    for bucket, data in report["calibration_by_bucket"].items():
        bar_actual = "█" * int(data["actual_frequency"] * 20)
        bar_est    = "░" * int(float(bucket.strip("%")) / 100 * 20)
        print(f"  {bucket:>5} est | actual={data['actual_frequency']:.0%} (n={data['n']}) "
              f"| err={data['calibration_error']:.0%}")
    print()
    print(f"  Ready for live: {'✅ YES' if report['ready_for_live'] else '❌ Need 50+ resolved bets'}")
    print("=" * 55 + "\n")

    await alerter.send_calibration_report(report)


def demo_vol_model():
    """Demo the BTC vol model."""
    from btc_vol_model import demonstrate
    demonstrate()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Bot")
    parser.add_argument("--status",    action="store_true", help="Show paper tracker status")
    parser.add_argument("--calibrate", action="store_true", help="Show calibration report")
    parser.add_argument("--demo",      action="store_true", help="Demo BTC vol model")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram alerts")
    args = parser.parse_args()

    if args.demo:
        demo_vol_model()
    elif args.status:
        asyncio.run(show_status())
    elif args.calibrate:
        asyncio.run(show_calibration())
    else:
        asyncio.run(run_scan(send_telegram=not args.no_telegram))


if __name__ == "__main__":
    main()

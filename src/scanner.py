# -*- coding: utf-8 -*-
"""
Polymarket Scanner
==================
Main scanner that:
1. Fetches active markets
2. Runs probability estimation on each
3. Filters for positive EV opportunities
4. Ranks by Kelly fraction (highest conviction first)
5. Outputs report

Usage:
    python scanner.py                 # Scan all markets
    python scanner.py --category crypto  # Crypto only
    python scanner.py --min-ev 0.05   # Min 5% EV only
"""

import asyncio
import argparse
from datetime import datetime
from typing import List
from loguru import logger

from market_fetcher import PolymarketFetcher, PolymarketMarket, MarketMispricing
from probability_engine import ProbabilityEngine


MIN_EV_THRESHOLD     = 0.03    # Min 3% EV to flag
MIN_KELLY_THRESHOLD  = 0.01    # Min 1% Kelly to flag
MIN_LIQUIDITY        = 5_000   # Min $5k liquidity
MIN_VOLUME_24H       = 10_000  # Min $10k 24h volume
MAX_SPREAD           = 0.05    # Max 5% spread (market maker take)


class PolymarketScanner:

    def __init__(self):
        self.fetcher = PolymarketFetcher()
        self.engine  = ProbabilityEngine()

    async def scan(
        self,
        category: str = None,
        min_ev: float = MIN_EV_THRESHOLD,
        limit: int = 100,
    ) -> List[MarketMispricing]:
        """Scan markets and return ranked mispricing opportunities."""

        logger.info(f"Scanning Polymarket... (limit={limit}, min_ev={min_ev:.0%})")
        markets = await self.fetcher.get_active_markets(limit=limit, category=category)
        logger.info(f"Fetched {len(markets)} markets")

        opportunities = []

        for market in markets:
            # Filter out illiquid/wide-spread markets
            if market.liquidity < MIN_LIQUIDITY:
                continue
            if market.volume_24h < MIN_VOLUME_24H:
                continue
            if market.spread > MAX_SPREAD:
                continue
            if market.resolved:
                continue

            # Estimate true probability
            estimate = self.engine.estimate(
                question=market.question,
                yes_price=market.yes_price,
                end_date=market.end_date,
            )

            # Skip if low confidence
            if estimate.confidence < 0.40:
                continue

            # Calculate EV and Kelly
            ev_yes, ev_no, kelly_yes, kelly_no = self.engine.calculate_ev_and_kelly(
                our_prob=estimate.our_probability,
                yes_price=market.yes_price,
                no_price=market.no_price,
            )

            # Determine recommended side
            best_ev   = max(ev_yes, ev_no)
            best_kelly = kelly_yes if ev_yes > ev_no else kelly_no

            if best_ev < min_ev:
                continue
            if best_kelly < MIN_KELLY_THRESHOLD:
                continue

            recommended_side = "YES" if ev_yes > ev_no else "NO"
            edge = estimate.our_probability - market.yes_price

            # Confidence label
            if estimate.confidence >= 0.65:
                confidence_label = "HIGH"
            elif estimate.confidence >= 0.50:
                confidence_label = "MEDIUM"
            else:
                confidence_label = "LOW"

            opportunities.append(MarketMispricing(
                market=market,
                our_probability=estimate.our_probability,
                market_probability=market.yes_price,
                edge=edge,
                ev_yes=ev_yes,
                ev_no=ev_no,
                kelly_yes=kelly_yes,
                kelly_no=kelly_no,
                recommended_side=recommended_side,
                confidence=confidence_label,
                reasoning=estimate.reasoning,
            ))

        # Sort by Kelly fraction (highest conviction first)
        opportunities.sort(key=lambda x: max(x.kelly_yes, x.kelly_no), reverse=True)
        return opportunities

    def print_report(self, opportunities: List[MarketMispricing], top_n: int = 10):
        """Print formatted opportunity report."""
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        print()
        print("=" * 70)
        print(f"  POLYMARKET OPPORTUNITY SCANNER — {now}")
        print("=" * 70)
        print(f"  Found {len(opportunities)} opportunities above threshold")
        print()

        if not opportunities:
            print("  No high-confidence opportunities found right now.")
            print("  Market is reasonably efficient today. Check back later.")
            print()
            return

        for i, opp in enumerate(opportunities[:top_n], 1):
            m = opp.market
            days_left = ""
            if m.end_date:
                from probability_engine import ProbabilityEngine
                d = ProbabilityEngine()._days_to_end(m.end_date)
                if d:
                    days_left = f" | {d:.0f}d left"

            print(f"  #{i} [{opp.confidence}] {m.category.upper()}")
            print(f"  {m.question[:65]}{'...' if len(m.question) > 65 else ''}")
            print(f"  Market: YES={m.yes_price:.2%} | NO={m.no_price:.2%} | "
                  f"Vol24h=${m.volume_24h/1e3:.0f}k{days_left}")
            print(f"  Our estimate: {opp.our_probability:.2%} | "
                  f"Edge: {opp.edge:+.2%}")
            print(f"  EV(YES)={opp.ev_yes:+.2%} | EV(NO)={opp.ev_no:+.2%} | "
                  f"Kelly={max(opp.kelly_yes, opp.kelly_no):.1%} → BET {opp.recommended_side}")
            print(f"  Reasoning: {opp.reasoning}")
            print()

        print("=" * 70)
        print("  BANKROLL MANAGEMENT")
        print("  - Never exceed 25% Kelly (use 10-15% for safety)")
        print("  - Max 5% of bankroll per single market")
        print("  - Diversify across uncorrelated markets")
        print("  - Track all bets. Judge on 50+ resolved markets minimum.")
        print("=" * 70)
        print()

    async def close(self):
        await self.fetcher.close()


async def main():
    parser = argparse.ArgumentParser(description="Polymarket opportunity scanner")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--min-ev", type=float, default=MIN_EV_THRESHOLD, help="Min EV threshold")
    parser.add_argument("--limit", type=int, default=100, help="Max markets to scan")
    parser.add_argument("--top", type=int, default=10, help="Top N to display")
    args = parser.parse_args()

    scanner = PolymarketScanner()
    try:
        opportunities = await scanner.scan(
            category=args.category,
            min_ev=args.min_ev,
            limit=args.limit,
        )
        scanner.print_report(opportunities, top_n=args.top)
    finally:
        await scanner.close()


if __name__ == "__main__":
    asyncio.run(main())

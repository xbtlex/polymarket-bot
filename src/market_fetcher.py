# -*- coding: utf-8 -*-
"""
Polymarket Market Fetcher
=========================
Pulls live market data from Polymarket's public APIs.

APIs used:
- Gamma API: market metadata, categories, volume, liquidity
- CLOB API: real-time order books, best prices, spreads

No API key required for read-only access.
"""

import aiohttp
import asyncio
import ssl
import certifi
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
from loguru import logger

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# SSL context — Polymarket's gamma-api has a hostname mismatch on some Windows setups
# Using False disables cert verification for this specific API only
SSL_CONTEXT = False  # nosec — Polymarket public read-only API, no sensitive data sent


@dataclass
class PolymarketMarket:
    """Single prediction market."""
    market_id: str
    question: str
    category: str
    yes_price: float           # Current YES price (0-1)
    no_price: float            # Current NO price (0-1)
    volume_24h: float          # 24h trading volume USD
    total_volume: float        # All-time volume USD
    liquidity: float           # Current liquidity USD
    end_date: Optional[datetime]
    resolved: bool
    outcome: Optional[str]     # "YES" / "NO" if resolved

    # CLOB token IDs — required for live order execution
    yes_token_id: str = ""     # Token ID for YES shares on CLOB
    no_token_id: str  = ""     # Token ID for NO shares on CLOB

    # Derived
    implied_prob: float = 0.0  # Market's implied probability of YES
    spread: float = 0.0        # YES + NO - 1.0 (market maker take)

    def __post_init__(self):
        self.implied_prob = self.yes_price
        self.spread = (self.yes_price + self.no_price) - 1.0


@dataclass
class MarketMispricing:
    """Detected mispricing opportunity."""
    market: PolymarketMarket
    our_probability: float     # Our estimated true probability
    market_probability: float  # Market's implied probability
    edge: float                # our_prob - market_prob
    ev_yes: float              # Expected value of YES bet
    ev_no: float               # Expected value of NO bet
    kelly_yes: float           # Kelly fraction for YES
    kelly_no: float            # Kelly fraction for NO
    recommended_side: str      # "YES" / "NO" / "PASS"
    confidence: str            # "HIGH" / "MEDIUM" / "LOW"
    reasoning: str             # Why we think it's mispriced


class PolymarketFetcher:
    """Fetches live market data from Polymarket APIs."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            connector = aiohttp.TCPConnector(ssl=SSL_CONTEXT)
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                connector=connector,
            )
        return self.session

    async def get_active_markets(
        self,
        limit: int = 50,
        min_volume: float = 10_000,
        category: Optional[str] = None,
    ) -> List[PolymarketMarket]:
        """Fetch active markets sorted by volume."""
        session = await self._get_session()
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        if category:
            params["category"] = category

        try:
            async with session.get(f"{GAMMA_BASE}/markets", params=params) as resp:
                data = await resp.json()

            markets = []
            for m in data:
                try:
                    tokens = m.get("tokens", [])
                    yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), None)
                    no_token  = next((t for t in tokens if t.get("outcome") == "No"), None)

                    yes_price = float(yes_token.get("price", 0.5)) if yes_token else 0.5
                    no_price  = float(no_token.get("price", 0.5)) if no_token else 0.5
                    vol_24h   = float(m.get("volume24hr", 0))

                    if vol_24h < min_volume:
                        continue

                    end_date = None
                    if m.get("endDate"):
                        try:
                            end_date = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                        except Exception:
                            pass

                    markets.append(PolymarketMarket(
                        market_id=m.get("conditionId", m.get("id", "")),
                        question=m.get("question", ""),
                        category=m.get("category", ""),
                        yes_price=yes_price,
                        no_price=no_price,
                        volume_24h=vol_24h,
                        total_volume=float(m.get("volume", 0)),
                        liquidity=float(m.get("liquidity", 0)),
                        end_date=end_date,
                        resolved=m.get("closed", False),
                        outcome=m.get("resolution"),
                        yes_token_id=yes_token.get("token_id", "") if yes_token else "",
                        no_token_id=no_token.get("token_id", "")  if no_token  else "",
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing market: {e}")
                    continue

            return markets

        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    async def get_market_by_keyword(self, keyword: str) -> List[PolymarketMarket]:
        """Search markets by keyword."""
        session = await self._get_session()
        params = {"q": keyword, "active": "true", "limit": 20}
        try:
            async with session.get(f"{GAMMA_BASE}/markets", params=params) as resp:
                data = await resp.json()
            all_markets = await self.get_active_markets(limit=200)
            keyword_lower = keyword.lower()
            return [m for m in all_markets if keyword_lower in m.question.lower()]
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

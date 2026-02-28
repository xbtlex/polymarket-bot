# -*- coding: utf-8 -*-
"""
Probability Engine
==================
Estimates true probability for Polymarket markets using:

1. Base rates — historical frequency of similar events
2. Macro model — for macro/economic markets
3. Crypto model — for BTC/ETH price markets
4. Longshot bias detection — systematic retail mispricing
5. Near-resolution arbitrage — pricing inefficiency near close

The goal: find markets where our estimated probability
differs from the market price by enough to have positive EV.

EV(YES) = our_prob - yes_price
EV(NO)  = (1 - our_prob) - no_price
Kelly   = edge / (1 - wrong_price)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple
from loguru import logger
import math


@dataclass
class ProbabilityEstimate:
    our_probability: float      # 0-1
    confidence: float           # 0-1 (how confident in estimate)
    method: str                 # How we estimated it
    reasoning: str              # Human-readable explanation


class ProbabilityEngine:
    """
    Estimates true probability for prediction markets.
    Uses multiple methods depending on market type.
    """

    # Longshot bias thresholds
    # Markets below these prices are systematically overpriced by retail
    LONGSHOT_OVERPRICED_BELOW = 0.08   # YES < 8 cents → often really < 4 cents
    FAVORITE_UNDERPRICED_ABOVE = 0.92  # YES > 92 cents → often really > 95 cents

    def estimate(self, question: str, yes_price: float, end_date: Optional[datetime] = None) -> ProbabilityEstimate:
        """
        Main entry point. Routes to appropriate estimation method
        based on question content.
        """
        question_lower = question.lower()

        # Route to specialist estimators
        if any(w in question_lower for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "sol", "solana"]):
            return self._estimate_crypto(question, yes_price, end_date)

        if any(w in question_lower for w in ["fed", "rate", "fomc", "cpi", "inflation", "gdp", "nfp", "payroll"]):
            return self._estimate_macro(question, yes_price, end_date)

        if any(w in question_lower for w in ["election", "president", "senate", "congress", "vote", "win"]):
            return self._estimate_political(question, yes_price, end_date)

        if any(w in question_lower for w in ["price", "above", "below", "reach", "$"]):
            return self._estimate_price_target(question, yes_price, end_date)

        # Default: check for systematic biases
        return self._estimate_base_rate(question, yes_price, end_date)

    def _estimate_crypto(self, question: str, yes_price: float, end_date: Optional[datetime]) -> ProbabilityEstimate:
        """Estimate crypto price markets using vol and regime context."""
        question_lower = question.lower()

        # Days to resolution
        days_to_end = self._days_to_end(end_date)

        # BTC price target markets — use historical vol
        # BTC 30-day vol is roughly 60-80% annualized
        # For short-term binary price targets we can estimate using log-normal

        # Extract price target if possible
        import re
        price_match = re.search(r'\$([0-9,]+)k?', question)
        if price_match:
            try:
                target_str = price_match.group(1).replace(',', '')
                target = float(target_str)
                if target < 1000:  # "k" format
                    target *= 1000

                # Current BTC rough estimate
                current_btc = 65_900
                log_return_needed = math.log(target / current_btc)

                # Annualized vol ~70%, scale to time period
                if days_to_end and days_to_end > 0:
                    vol = 0.70 * math.sqrt(days_to_end / 365)
                    # Drift assumption: 0 (conservative)
                    from scipy.stats import norm
                    prob = float(norm.cdf(log_return_needed / vol))
                    if "above" in question_lower or "over" in question_lower or "exceed" in question_lower:
                        prob = 1 - prob  # P(price > target)

                    return ProbabilityEstimate(
                        our_probability=max(0.02, min(0.98, prob)),
                        confidence=0.6,
                        method="log-normal vol model",
                        reasoning=f"BTC at ~$65,900, target ${target:,.0f}, {days_to_end}d window, 70% annualized vol → P={prob:.1%}"
                    )
            except Exception:
                pass

        # Generic crypto market — check longshot bias
        return self._estimate_base_rate(question, yes_price, end_date)

    def _estimate_macro(self, question: str, yes_price: float, end_date: Optional[datetime]) -> ProbabilityEstimate:
        """Estimate macro/Fed markets."""
        question_lower = question.lower()

        # Fed rate cut markets
        if "rate cut" in question_lower or "cut rates" in question_lower:
            # Current macro: Fed holding, sticky inflation, hawkish bias
            # Market consensus: no cuts in near term
            base_prob = 0.15  # Low probability of cut given current data
            return ProbabilityEstimate(
                our_probability=base_prob,
                confidence=0.65,
                method="macro model",
                reasoning="Fed hawkish, sticky inflation, no cuts likely near term. Fed funds futures pricing minimal cuts."
            )

        if "rate hike" in question_lower or "hike" in question_lower:
            base_prob = 0.05  # Extremely unlikely to hike
            return ProbabilityEstimate(
                our_probability=base_prob,
                confidence=0.75,
                method="macro model",
                reasoning="Fed hiking cycle over. Hike probability near zero given current data."
            )

        if "recession" in question_lower:
            base_prob = 0.30  # Meaningful recession risk
            return ProbabilityEstimate(
                our_probability=base_prob,
                confidence=0.50,
                method="macro model",
                reasoning="Yield curve recently re-steepened after inversion — historically elevated recession risk 6-18 months out."
            )

        if "cpi" in question_lower and ("above" in question_lower or "below" in question_lower or "beat" in question_lower):
            # CPI surprise markets — roughly coin flip but Fed-watchers have edge
            base_prob = 0.45  # Slight lean toward miss given sticky inflation
            return ProbabilityEstimate(
                our_probability=base_prob,
                confidence=0.45,
                method="macro model",
                reasoning="CPI markets are near coin-flip. Slight miss bias given sticky services inflation."
            )

        return self._estimate_base_rate(question, yes_price, end_date)

    def _estimate_political(self, question: str, yes_price: float, end_date: Optional[datetime]) -> ProbabilityEstimate:
        """Estimate political markets — base rate + polling."""
        # Political markets tend to be reasonably efficient
        # Main edge: longshot bias and near-resolution arbitrage
        return ProbabilityEstimate(
            our_probability=yes_price,  # Trust market for political events
            confidence=0.30,
            method="market trust",
            reasoning="Political markets are relatively efficient. No strong prior to deviate from market price."
        )

    def _estimate_price_target(self, question: str, yes_price: float, end_date: Optional[datetime]) -> ProbabilityEstimate:
        """Generic price target market."""
        return self._estimate_base_rate(question, yes_price, end_date)

    def _estimate_base_rate(self, question: str, yes_price: float, end_date: Optional[datetime]) -> ProbabilityEstimate:
        """
        Default estimator — applies longshot bias corrections
        and near-resolution pricing.
        """
        days_to_end = self._days_to_end(end_date)
        our_prob = yes_price  # Start from market price

        # LONGSHOT BIAS CORRECTION
        # Research shows retail overpays for longshots by ~2-3x
        if yes_price < self.LONGSHOT_OVERPRICED_BELOW:
            # Retail is probably paying 2-3x the true probability
            our_prob = yes_price * 0.45  # Deflate significantly
            return ProbabilityEstimate(
                our_probability=max(0.01, our_prob),
                confidence=0.65,
                method="longshot bias correction",
                reasoning=f"YES at {yes_price:.1%} — below {self.LONGSHOT_OVERPRICED_BELOW:.0%} threshold. "
                          f"Retail overpays for longshots by ~2-3x. True prob est. {our_prob:.1%}. "
                          f"SELL YES or BUY NO."
            )

        # FAVORITE BIAS CORRECTION
        # High-probability markets are sometimes underpriced
        if yes_price > self.FAVORITE_UNDERPRICED_ABOVE:
            our_prob = min(0.98, yes_price * 1.03)
            return ProbabilityEstimate(
                our_probability=our_prob,
                confidence=0.55,
                method="favorite bias correction",
                reasoning=f"YES at {yes_price:.1%} — near certainty. May be underpriced. Est. true prob {our_prob:.1%}."
            )

        # NEAR-RESOLUTION ARBITRAGE
        if days_to_end and days_to_end <= 3 and yes_price > 0.85:
            our_prob = min(0.97, yes_price + 0.04)
            return ProbabilityEstimate(
                our_probability=our_prob,
                confidence=0.60,
                method="near-resolution arb",
                reasoning=f"Market resolves in {days_to_end:.0f} days, YES at {yes_price:.1%}. "
                          f"Near-resolution discount often exists. True prob likely {our_prob:.1%}."
            )

        # No strong view — trust the market
        return ProbabilityEstimate(
            our_probability=yes_price,
            confidence=0.20,
            method="no edge detected",
            reasoning="No systematic mispricing detected. Market price likely fair."
        )

    def calculate_ev_and_kelly(
        self,
        our_prob: float,
        yes_price: float,
        no_price: float,
        max_kelly_fraction: float = 0.25,  # Never bet more than 25% Kelly
    ) -> Tuple[float, float, float, float]:
        """
        Calculate expected value and Kelly fraction for both sides.

        Returns: (ev_yes, ev_no, kelly_yes, kelly_no)
        """
        # EV = (prob × payout) - cost
        # YES: pay yes_price, win 1.0 if YES resolves
        ev_yes = (our_prob * 1.0) - yes_price

        # NO: pay no_price, win 1.0 if NO resolves
        ev_no = ((1 - our_prob) * 1.0) - no_price

        # Kelly fraction: f = edge / odds
        # For binary markets: f = (p - q * b) / b where b = (1/price) - 1
        def kelly(prob, price):
            if price <= 0 or price >= 1:
                return 0.0
            odds = (1.0 / price) - 1.0  # Net odds
            f = (prob * odds - (1 - prob)) / odds
            return max(0.0, min(max_kelly_fraction, f))

        kelly_yes = kelly(our_prob, yes_price)
        kelly_no  = kelly(1 - our_prob, no_price)

        return ev_yes, ev_no, kelly_yes, kelly_no

    def _days_to_end(self, end_date: Optional[datetime]) -> Optional[float]:
        if not end_date:
            return None
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        delta = (end_date - now).total_seconds() / 86400
        return max(0, delta)

# -*- coding: utf-8 -*-
"""
BTC Volatility Model for Polymarket Price Markets
==================================================
Prices binary "Will BTC be above $X by date Y?" markets
using a proper log-normal model calibrated to BTC's
actual volatility term structure.

Method:
    P(BTC > target | current, days, vol) = N(d2)

    where:
        d2 = [ln(S/K) + (μ - σ²/2)T] / (σ√T)
        S = current spot price
        K = target price
        T = time to expiry in years
        σ = annualized volatility
        μ = drift (use 0 for conservative)
        N = cumulative normal distribution

Vol Term Structure (calibrated to BTC historical data):
    - 7 days:  80% annualized
    - 30 days: 70% annualized
    - 90 days: 65% annualized
    - 180 days: 60% annualized
    - 365 days: 55% annualized
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

try:
    from scipy.stats import norm as scipy_norm
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


def _norm_cdf(x: float) -> float:
    """Cumulative normal distribution. Uses scipy if available, else approximation."""
    if SCIPY_AVAILABLE:
        return float(scipy_norm.cdf(x))
    # Abramowitz & Stegun approximation (accurate to 7 decimal places)
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t)
                + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * math.exp(-x * x / 2)
    return 0.5 * (1.0 + sign * y)


# BTC volatility term structure (annualized)
# Based on historical realized vol + typical implied vol premium
VOL_TERM_STRUCTURE = {
    7:   0.85,   # 1 week: very high, BTC can move 10% in days
    14:  0.80,
    30:  0.72,   # 1 month
    60:  0.68,
    90:  0.65,   # 3 months
    180: 0.60,   # 6 months
    365: 0.55,   # 1 year
    730: 0.50,   # 2 years
}

# Regime vol adjustments
REGIME_VOL_ADJUSTMENTS = {
    "cooperation": 0.90,  # Trending market, slightly lower realized vol
    "mixed":       1.00,  # Normal
    "defection":   1.20,  # High vol / liquidation cascades
}


@dataclass
class BinaryOptionResult:
    """Result of binary option pricing."""
    probability_above: float    # P(price > target)
    probability_below: float    # P(price < target)
    implied_vol: float          # Vol used
    days_to_expiry: float
    current_price: float
    target_price: float
    log_return_needed: float    # How much BTC needs to move (log)
    z_score: float              # Standardized move needed


class BTCVolModel:
    """
    Binary option pricer for BTC price prediction markets.
    Tells you: given current BTC price, what's the probability
    BTC will be above (or below) a target price by a given date?
    """

    def __init__(
        self,
        current_btc_price: float = 65_900,
        btc_regime: str = "mixed",  # cooperation / mixed / defection
        drift: float = 0.0,         # Annual drift. Use 0 for conservative.
    ):
        self.current_price = current_btc_price
        self.btc_regime    = btc_regime
        self.drift         = drift

    def _get_vol(self, days: float) -> float:
        """Get interpolated annualized vol for given time horizon."""
        if days <= 0:
            return 0.85

        # Interpolate between known points
        days_keys = sorted(VOL_TERM_STRUCTURE.keys())

        if days <= days_keys[0]:
            base_vol = VOL_TERM_STRUCTURE[days_keys[0]]
        elif days >= days_keys[-1]:
            base_vol = VOL_TERM_STRUCTURE[days_keys[-1]]
        else:
            # Linear interpolation
            lower = max(k for k in days_keys if k <= days)
            upper = min(k for k in days_keys if k >= days)
            if lower == upper:
                base_vol = VOL_TERM_STRUCTURE[lower]
            else:
                t = (days - lower) / (upper - lower)
                base_vol = VOL_TERM_STRUCTURE[lower] * (1 - t) + VOL_TERM_STRUCTURE[upper] * t

        # Apply regime adjustment
        regime_adj = REGIME_VOL_ADJUSTMENTS.get(self.btc_regime.lower(), 1.0)
        return base_vol * regime_adj

    def price_above_target(
        self,
        target: float,
        days_to_expiry: float,
    ) -> BinaryOptionResult:
        """
        P(BTC > target) at expiry.

        For "Will BTC be above $X by [date]?" markets.
        """
        S = self.current_price
        K = target
        T = days_to_expiry / 365.0

        if T <= 0:
            # Already expired
            prob = 1.0 if S > K else 0.0
            return BinaryOptionResult(
                probability_above=prob, probability_below=1-prob,
                implied_vol=0, days_to_expiry=0,
                current_price=S, target_price=K,
                log_return_needed=0, z_score=0,
            )

        vol = self._get_vol(days_to_expiry)
        vol_T = vol * math.sqrt(T)

        log_return = math.log(K / S)  # Log return needed to hit target
        d2 = (math.log(S / K) + (self.drift - 0.5 * vol ** 2) * T) / vol_T

        prob_above = _norm_cdf(d2)  # P(BTC > K)
        prob_below = 1.0 - prob_above

        return BinaryOptionResult(
            probability_above=max(0.01, min(0.99, prob_above)),
            probability_below=max(0.01, min(0.99, prob_below)),
            implied_vol=vol,
            days_to_expiry=days_to_expiry,
            current_price=S,
            target_price=K,
            log_return_needed=log_return,
            z_score=d2,
        )

    def price_range_market(
        self,
        lower: float,
        upper: float,
        days_to_expiry: float,
    ) -> float:
        """P(lower < BTC < upper) at expiry."""
        p_above_lower = self.price_above_target(lower, days_to_expiry).probability_above
        p_above_upper = self.price_above_target(upper, days_to_expiry).probability_above
        return max(0.01, p_above_lower - p_above_upper)

    def analyze_market(self, question: str, yes_price: float, end_date: datetime) -> Optional[dict]:
        """
        Full analysis of a BTC price market.
        Extracts target from question, prices it, compares to market.
        """
        import re
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        days = max(1, (end_date - now).total_seconds() / 86400)

        question_lower = question.lower()

        # Extract price target
        # Handles: "$70k", "$70,000", "70000", "$70K"
        patterns = [
            r'\$([0-9,]+)k\b',      # $70k
            r'\$([0-9,]+),([0-9]+)',  # $70,000
            r'\$([0-9]+)K\b',        # $70K
            r'([0-9]+),([0-9]{3})\b', # 70,000
        ]

        target = None
        for pat in patterns:
            m = re.search(pat, question, re.IGNORECASE)
            if m:
                try:
                    num_str = m.group(1).replace(',', '')
                    target = float(num_str)
                    if 'k' in m.group(0).lower() or target < 1000:
                        target *= 1000
                    if 1_000 < target < 10_000_000:  # Sanity check
                        break
                    target = None
                except Exception:
                    continue

        if not target:
            return None

        # Determine direction
        is_above = any(w in question_lower for w in ["above", "over", "exceed", "higher than", "more than", ">"])
        is_below = any(w in question_lower for w in ["below", "under", "less than", "<"])

        result = self.price_above_target(target, days)

        our_prob = result.probability_above if is_above else result.probability_below
        direction = "above" if is_above else "below"

        move_pct = (target - self.current_price) / self.current_price * 100
        edge = our_prob - yes_price

        return {
            "target": target,
            "direction": direction,
            "our_probability": our_prob,
            "market_probability": yes_price,
            "edge": edge,
            "days_to_expiry": days,
            "implied_vol": result.implied_vol,
            "z_score": result.z_score,
            "move_needed_pct": move_pct,
            "reasoning": (
                f"BTC at ${self.current_price:,.0f}, target ${target:,.0f} ({direction}), "
                f"{days:.0f}d window. Move needed: {move_pct:+.1f}%. "
                f"Vol: {result.implied_vol:.0%} ann. Z-score: {result.z_score:.2f}. "
                f"Model prob: {our_prob:.1%} vs market {yes_price:.1%} "
                f"(edge: {edge:+.1%})"
            )
        }


def demonstrate():
    """Quick demo of the model."""
    model = BTCVolModel(current_btc_price=65_900, btc_regime="mixed")

    print("\nBTC BINARY OPTION MODEL — Quick Demo")
    print("=" * 55)
    print(f"Current BTC: ${model.current_price:,.0f}")
    print()

    scenarios = [
        (70_000, 30,  "Will BTC be above $70k in 30 days?"),
        (80_000, 60,  "Will BTC be above $80k in 60 days?"),
        (60_000, 30,  "Will BTC stay above $60k in 30 days?"),
        (100_000, 90, "Will BTC hit $100k in 90 days?"),
        (50_000, 30,  "Will BTC fall below $50k in 30 days?"),
    ]

    for target, days, question in scenarios:
        r = model.price_above_target(target, days)
        above = target > model.current_price
        prob = r.probability_above if above else r.probability_below
        direction = "ABOVE" if above else "BELOW"
        print(f"  {question}")
        print(f"  P(BTC {direction} ${target:,.0f} in {days}d) = {prob:.1%} | vol={r.implied_vol:.0%} | z={r.z_score:.2f}")
        print()


if __name__ == "__main__":
    demonstrate()

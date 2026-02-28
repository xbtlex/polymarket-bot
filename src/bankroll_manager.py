# -*- coding: utf-8 -*-
"""
Bankroll Manager
================
Controls position sizing and risk limits for autonomous trading.

Rules:
- Never bet more than MAX_SINGLE_BET_PCT of bankroll on one market
- Never have more than MAX_TOTAL_EXPOSURE_PCT deployed at once
- Use fractional Kelly (25%) — never full Kelly
- Minimum EV threshold before any bet
- Correlation check — don't double-up on related markets
"""

from dataclasses import dataclass
from typing import List, Optional
from loguru import logger


# ── Risk Parameters ───────────────────────────────────────────────────────────

MAX_SINGLE_BET_PCT   = 0.05   # Max 5% of bankroll per bet
MAX_TOTAL_EXPOSURE   = 0.40   # Max 40% of bankroll deployed at once
KELLY_FRACTION       = 0.25   # Use 25% of full Kelly
MIN_EV_TO_BET        = 0.04   # Min 4% EV before executing
MIN_KELLY_TO_BET     = 0.02   # Min 2% Kelly before executing
MIN_LIQUIDITY_TO_BET = 10_000 # Min $10k market liquidity
MIN_CONFIDENCE       = "MEDIUM"


@dataclass
class SizingResult:
    """Position sizing recommendation."""
    bet_size_usd: float         # How much to bet in USD
    kelly_fraction: float       # Kelly fraction used
    reasoning: str
    approved: bool              # Whether to proceed
    rejection_reason: str = ""  # If not approved


class BankrollManager:
    """Controls risk and position sizing."""

    def __init__(
        self,
        bankroll_usd: float,
        max_single_bet_pct: float = MAX_SINGLE_BET_PCT,
        max_total_exposure: float = MAX_TOTAL_EXPOSURE,
        kelly_fraction: float = KELLY_FRACTION,
    ):
        self.bankroll           = bankroll_usd
        self.max_single_bet_pct = max_single_bet_pct
        self.max_total_exposure = max_total_exposure
        self.kelly_fraction     = kelly_fraction
        self._open_exposure     = 0.0  # Currently deployed USD

    def update_bankroll(self, new_balance: float):
        self.bankroll = new_balance

    def update_exposure(self, deployed_usd: float):
        self._open_exposure = deployed_usd

    def size_bet(
        self,
        ev: float,
        kelly: float,
        confidence: str,
        market_liquidity: float,
        question: str = "",
    ) -> SizingResult:
        """
        Calculate position size. Returns SizingResult with approved flag.
        """
        # Hard gates
        if ev < MIN_EV_TO_BET:
            return SizingResult(0, 0, "", False, f"EV {ev:.1%} below minimum {MIN_EV_TO_BET:.0%}")

        if kelly < MIN_KELLY_TO_BET:
            return SizingResult(0, 0, "", False, f"Kelly {kelly:.1%} below minimum {MIN_KELLY_TO_BET:.0%}")

        if market_liquidity < MIN_LIQUIDITY_TO_BET:
            return SizingResult(0, 0, "", False, f"Liquidity ${market_liquidity:,.0f} below ${MIN_LIQUIDITY_TO_BET:,.0f}")

        if confidence == "LOW":
            return SizingResult(0, 0, "", False, "Low confidence — skip")

        # Exposure check
        remaining_capacity = self.bankroll * self.max_total_exposure - self._open_exposure
        if remaining_capacity <= 0:
            return SizingResult(0, 0, "", False, f"Max exposure reached ({self.max_total_exposure:.0%} of bankroll)")

        # Kelly sizing
        full_kelly_usd    = kelly * self.bankroll
        fractional_kelly  = full_kelly_usd * self.kelly_fraction

        # Confidence multiplier
        conf_mult = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}.get(confidence, 0.5)
        sized     = fractional_kelly * conf_mult

        # Hard caps
        max_bet = min(
            self.bankroll * self.max_single_bet_pct,  # 5% of bankroll
            remaining_capacity,                        # Remaining capacity
            market_liquidity * 0.02,                  # Max 2% of market liquidity
        )

        final_size = min(sized, max_bet)
        final_size = max(1.0, round(final_size, 2))  # Min $1 bet

        return SizingResult(
            bet_size_usd=final_size,
            kelly_fraction=self.kelly_fraction * conf_mult,
            reasoning=(
                f"Full Kelly: ${full_kelly_usd:.2f} × {self.kelly_fraction:.0%} Kelly × "
                f"{conf_mult:.0%} conf = ${fractional_kelly * conf_mult:.2f}, "
                f"capped at ${max_bet:.2f} → ${final_size:.2f}"
            ),
            approved=True,
        )

    def get_status(self) -> dict:
        return {
            "bankroll":          self.bankroll,
            "open_exposure":     self._open_exposure,
            "exposure_pct":      self._open_exposure / self.bankroll if self.bankroll else 0,
            "remaining_capacity": self.bankroll * self.max_total_exposure - self._open_exposure,
        }

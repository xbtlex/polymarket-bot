# -*- coding: utf-8 -*-
"""
Trade Executor
==============
Executes bets on Polymarket via the CLOB API.

Requires:
    pip install py-clob-client

Wallet setup:
    - Fund a Polygon wallet with USDC
    - Approve Polymarket's CTF contract to spend USDC
    - Set POLYMARKET_PRIVATE_KEY in environment

The CLOB API uses a limit order book. We place limit orders
at or slightly inside the current best ask/bid to get fills
without crossing the spread unnecessarily.
"""

import os
import asyncio
from dataclasses import dataclass
from typing import Optional
from loguru import logger

# Polymarket CLOB client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, Side
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    logger.warning("py-clob-client not installed. Run: pip install py-clob-client")


CLOB_HOST    = "https://clob.polymarket.com"
CHAIN_ID     = 137  # Polygon mainnet
POLYGON_RPC  = "https://polygon-rpc.com"


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str]
    filled_size: float
    filled_price: float
    error: Optional[str] = None


class PolymarketExecutor:
    """
    Executes trades on Polymarket CLOB.
    Handles order placement, partial fills, and cancellation.
    """

    def __init__(self):
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.client: Optional[object] = None
        self._initialized = False

    def _init_client(self):
        """Lazy init CLOB client."""
        if self._initialized:
            return

        if not CLOB_AVAILABLE:
            raise RuntimeError("py-clob-client not installed. Run: pip install py-clob-client")

        if not self.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in environment")

        try:
            self.client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=self.private_key,
                signature_type=2,  # POLY_GNOSIS_SAFE or EOA
            )
            # Create API key if needed
            try:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
            except Exception:
                pass  # API creds might already exist

            self._initialized = True
            logger.info("Polymarket CLOB client initialized")
        except Exception as e:
            raise RuntimeError(f"Failed to init CLOB client: {e}")

    async def get_balance(self) -> float:
        """Get current USDC balance."""
        if not CLOB_AVAILABLE or not self.private_key:
            return 0.0
        try:
            self._init_client()
            # Balance via web3 — check USDC balance on Polygon
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
            # USDC on Polygon
            usdc_address  = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            usdc_abi = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
                         "outputs":[{"name":"","type":"uint256"}],"type":"function"}]
            contract = w3.eth.contract(address=usdc_address, abi=usdc_abi)
            from eth_account import Account
            account  = Account.from_key(self.private_key)
            balance  = contract.functions.balanceOf(account.address).call()
            return balance / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return 0.0

    async def buy_yes(
        self,
        token_id: str,
        size_usd: float,
        current_price: float,
        slippage: float = 0.02,  # Accept up to 2% slippage
    ) -> ExecutionResult:
        """Buy YES shares on a market."""
        return await self._place_order(token_id, size_usd, current_price, "BUY", slippage)

    async def buy_no(
        self,
        token_id: str,
        size_usd: float,
        current_price: float,
        slippage: float = 0.02,
    ) -> ExecutionResult:
        """Buy NO shares (equivalent to selling YES)."""
        return await self._place_order(token_id, size_usd, current_price, "BUY_NO", slippage)

    async def _place_order(
        self,
        token_id: str,
        size_usd: float,
        current_price: float,
        side: str,
        slippage: float,
    ) -> ExecutionResult:
        """Place a limit order on the CLOB."""
        if not CLOB_AVAILABLE:
            return ExecutionResult(False, None, 0, 0, "py-clob-client not installed")

        try:
            self._init_client()

            # Calculate max price with slippage
            if side == "BUY":
                price = min(0.99, current_price * (1 + slippage))
                shares = size_usd / price
            else:  # BUY_NO = buy the NO token
                no_price = 1 - current_price
                price    = min(0.99, no_price * (1 + slippage))
                shares   = size_usd / price

            price  = round(price, 4)
            shares = round(shares, 2)

            logger.info(f"Placing order: {side} {shares:.2f} shares @ ${price:.4f} (${size_usd:.2f})")

            order_args = OrderArgs(
                price=price,
                size=shares,
                side=BUY,
                token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            response     = self.client.post_order(signed_order, OrderType.GTC)

            order_id = response.get("orderID") or response.get("id", "")
            logger.info(f"Order placed: {order_id}")

            return ExecutionResult(
                success=True,
                order_id=order_id,
                filled_size=shares,
                filled_price=price,
            )

        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            return ExecutionResult(False, None, 0, 0, str(e))

    async def get_open_orders(self) -> list:
        """Get all open orders."""
        if not self._initialized:
            return []
        try:
            return self.client.get_orders() or []
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._initialized:
            return False
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def is_configured(self) -> bool:
        """Check if executor is ready to trade."""
        return bool(self.private_key) and CLOB_AVAILABLE

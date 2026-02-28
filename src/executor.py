# -*- coding: utf-8 -*-
"""
Trade Executor
==============
Executes bets on Polymarket via the CLOB API.

Requires for live trading:
    pip install py-clob-client web3

Wallet setup:
    1. Create a Polygon EOA wallet (MetaMask or generate new)
    2. Fund with USDC on Polygon network
    3. Visit polymarket.com, connect wallet, approve contracts
    4. Set POLYMARKET_PRIVATE_KEY in .env

Order flow:
    1. Get YES/NO token IDs from market data
    2. Check CLOB order book for current best prices
    3. Place limit order at best ask + small slippage buffer
    4. Poll for fill status
    5. If unfilled after timeout → cancel and skip

Notes:
    - Minimum order size on Polymarket CLOB is $1 USDC
    - Shares are priced 0.01–0.99 (cents on the dollar)
    - YES share at 0.60 = $0.60 per share, pays $1 if YES wins
"""

import os
import asyncio
import aiohttp
from dataclasses import dataclass
from typing import Optional
from loguru import logger

CLOB_HOST   = "https://clob.polymarket.com"
CHAIN_ID    = 137   # Polygon mainnet
POLYGON_RPC = "https://polygon-rpc.com"

# USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str]
    filled_shares: float
    filled_price: float
    cost_usd: float
    error: Optional[str] = None


class PolymarketExecutor:
    """
    Executes trades on Polymarket CLOB.

    Each market has TWO token IDs — one for YES shares, one for NO shares.
    We always BUY the token we want (YES token to bet YES, NO token to bet NO).
    Shares pay $1 at resolution if correct, $0 if wrong.
    """

    def __init__(self):
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.client: Optional[object] = None
        self._initialized = False

    def _init_client(self):
        """Lazy-init CLOB client."""
        if self._initialized:
            return

        if not CLOB_AVAILABLE:
            raise RuntimeError(
                "py-clob-client not installed.\n"
                "Run: pip install py-clob-client web3"
            )
        if not self.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in .env")

        try:
            self.client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=self.private_key,
                signature_type=0,   # 0 = EOA (regular private key wallet)
            )
            # Derive API credentials from private key
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._initialized = True
            logger.info("Polymarket CLOB client initialized ✓")
        except Exception as e:
            raise RuntimeError(f"CLOB client init failed: {e}")

    async def get_balance_usdc(self) -> float:
        """Get USDC balance on Polygon."""
        if not self.private_key:
            return 0.0
        try:
            from web3 import Web3
            from eth_account import Account

            w3       = Web3(Web3.HTTPProvider(POLYGON_RPC))
            account  = Account.from_key(self.private_key)
            abi      = [{"inputs":[{"name":"account","type":"address"}],
                         "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
                         "stateMutability":"view","type":"function"}]
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS), abi=abi
            )
            raw = contract.functions.balanceOf(account.address).call()
            return raw / 1e6  # USDC = 6 decimals
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return 0.0

    async def get_wallet_address(self) -> str:
        """Get wallet address from private key."""
        if not self.private_key:
            return ""
        try:
            from eth_account import Account
            return Account.from_key(self.private_key).address
        except Exception:
            return ""

    async def get_clob_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """
        Get current best price from CLOB order book for a token.
        Returns the best ask price (what we'd pay to buy).
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CLOB_HOST}/price",
                    params={"token_id": token_id, "side": side},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    price = data.get("price")
                    return float(price) if price else None
        except Exception as e:
            logger.debug(f"CLOB price fetch failed for {token_id}: {e}")
            return None

    async def execute_bet(
        self,
        token_id: str,          # YES or NO token ID from market data
        side: str,              # "YES" or "NO" (for logging only)
        size_usd: float,        # How much USDC to spend
        expected_price: float,  # Price from Gamma API (used as reference)
        max_slippage: float = 0.03,  # Accept up to 3% price slippage
        fill_timeout: int = 30,      # Seconds to wait for fill
    ) -> ExecutionResult:
        """
        Execute a bet by buying the specified token.

        token_id: the CLOB token ID for YES or NO shares
        size_usd: how much to spend in USDC
        expected_price: our reference price (from scanner)
        """
        if not CLOB_AVAILABLE:
            return ExecutionResult(False, None, 0, 0, 0, "py-clob-client not installed")

        try:
            self._init_client()

            # Step 1: Get live CLOB price (more accurate than Gamma API)
            live_price = await self.get_clob_price(token_id, "BUY")
            if live_price is None:
                live_price = expected_price
                logger.warning(f"Could not get CLOB price, using Gamma price {live_price:.3f}")

            # Step 2: Check slippage vs our scanner price
            price_diff = abs(live_price - expected_price)
            if price_diff > max_slippage:
                return ExecutionResult(
                    False, None, 0, 0, 0,
                    f"Price slipped: expected {expected_price:.3f}, got {live_price:.3f} "
                    f"(diff {price_diff:.1%} > max {max_slippage:.1%})"
                )

            # Step 3: Calculate shares — add small buffer on price to improve fill rate
            buy_price = min(0.99, round(live_price * 1.01, 4))  # 1% above best ask
            shares    = round(size_usd / buy_price, 2)

            if shares < 1.0:
                return ExecutionResult(
                    False, None, 0, 0, 0,
                    f"Order too small: ${size_usd:.2f} / {buy_price:.3f} = {shares:.2f} shares (min 1)"
                )

            logger.info(
                f"Placing order: BUY {shares:.2f} {side} shares @ ${buy_price:.4f} "
                f"= ${shares * buy_price:.2f} USDC | token={token_id[:16]}..."
            )

            # Step 4: Create and post order
            order_args   = OrderArgs(price=buy_price, size=shares, side=BUY, token_id=token_id)
            signed_order = self.client.create_order(order_args)
            response     = self.client.post_order(signed_order, OrderType.GTC)

            order_id = (
                response.get("orderID")
                or response.get("id")
                or response.get("order_id", "")
            )

            if not order_id:
                return ExecutionResult(False, None, 0, 0, 0, f"No order ID in response: {response}")

            logger.info(f"Order placed: {order_id}")

            # Step 5: Wait for fill
            filled = await self._wait_for_fill(order_id, timeout=fill_timeout)
            if not filled:
                # Cancel the unfilled order
                await self.cancel_order(order_id)
                return ExecutionResult(
                    False, order_id, 0, 0, 0,
                    f"Order {order_id} unfilled after {fill_timeout}s — cancelled"
                )

            cost = shares * buy_price
            logger.info(f"Order filled: {shares:.2f} shares @ {buy_price:.4f} = ${cost:.2f}")

            return ExecutionResult(
                success=True,
                order_id=order_id,
                filled_shares=shares,
                filled_price=buy_price,
                cost_usd=cost,
            )

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def _wait_for_fill(self, order_id: str, timeout: int = 30) -> bool:
        """Poll order status until filled or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                orders = self.client.get_orders()
                order  = next((o for o in (orders or []) if o.get("id") == order_id), None)
                if order is None:
                    return True   # Order gone from open book = filled
                status = order.get("status", "").lower()
                if status in ("matched", "filled", "completed"):
                    return True
                if status in ("cancelled", "canceled", "rejected"):
                    return False
            except Exception as e:
                logger.debug(f"Fill check error: {e}")
            await asyncio.sleep(3)
        return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._initialized:
            return False
        try:
            self.client.cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.warning(f"Cancel failed for {order_id}: {e}")
            return False

    async def get_open_orders(self) -> list:
        """Get all open orders."""
        if not self._initialized:
            return []
        try:
            return self.client.get_orders() or []
        except Exception:
            return []

    def is_configured(self) -> bool:
        """True if executor can actually trade."""
        return bool(self.private_key) and CLOB_AVAILABLE

# -*- coding: utf-8 -*-
"""
Setup Check
===========
Run this before going live to verify everything is configured correctly.

    python setup_check.py

Checks:
  ✓ Dependencies installed
  ✓ .env file exists and has required keys
  ✓ Wallet address derived from private key
  ✓ USDC balance on Polygon
  ✓ Polymarket API accessible
  ✓ CLOB client can authenticate
  ✓ Market data has valid token IDs
  ✓ Telegram alerts working
"""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


async def check_all():
    errors = []

    print()
    print("=" * 55)
    print("  POLYMARKET BOT — SETUP CHECK")
    print("=" * 55)
    print()

    # 1. Dependencies
    print("1. Dependencies")
    required = ["aiohttp", "loguru", "dotenv"]
    for pkg in required:
        try:
            __import__(pkg.replace("-", "_"))
            print(f"   {PASS} {pkg}")
        except ImportError:
            print(f"   {FAIL} {pkg} — run: pip install {pkg}")
            errors.append(pkg)

    live_deps = ["py_clob_client", "web3", "eth_account"]
    live_ok = True
    for pkg in live_deps:
        try:
            __import__(pkg)
            print(f"   {PASS} {pkg} (live trading)")
        except ImportError:
            print(f"   {WARN}{pkg} not installed — paper mode only")
            live_ok = False
    print()

    # 2. Environment
    print("2. Environment (.env)")
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        print(f"   {PASS} .env file found")
    else:
        print(f"   {FAIL} .env not found — copy .env.example to .env")
        errors.append(".env missing")

    tg_token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat    = os.getenv("TELEGRAM_CHAT_ID", "")
    pk         = os.getenv("POLYMARKET_PRIVATE_KEY", "")

    print(f"   {'✅' if tg_token else '❌'} TELEGRAM_BOT_TOKEN {'set' if tg_token else 'NOT SET'}")
    print(f"   {'✅' if tg_chat  else '❌'} TELEGRAM_CHAT_ID   {'set' if tg_chat  else 'NOT SET'}")
    print(f"   {'✅' if pk       else '⚠️ '} POLYMARKET_PRIVATE_KEY {'set' if pk else 'NOT SET (paper only)'}")
    print()

    # 3. Wallet
    print("3. Wallet")
    if pk and live_ok:
        try:
            from eth_account import Account
            acct = Account.from_key(pk)
            print(f"   {PASS} Address: {acct.address}")

            # Check USDC balance
            from executor import PolymarketExecutor
            ex  = PolymarketExecutor()
            bal = await ex.get_balance_usdc()
            bal_status = PASS if bal >= 5 else WARN
            print(f"   {bal_status} USDC balance: ${bal:.2f}")
            if bal < 5:
                print(f"       Fund your wallet with USDC on Polygon to trade live")
        except Exception as e:
            print(f"   {FAIL} Wallet error: {e}")
            errors.append("wallet")
    elif not pk:
        print(f"   {WARN}No private key — skipping wallet check")
    else:
        print(f"   {WARN}Live deps not installed — skipping wallet check")
    print()

    # 4. Polymarket API
    print("4. Polymarket API")
    try:
        import aiohttp
        import ssl, certifi
        _ssl = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl)) as s:
            async with s.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 3, "active": "true"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    print(f"   {PASS} Gamma API: {len(data)} markets fetched")
                    # Check token IDs
                    m      = data[0]
                    tokens = m.get("tokens", [])
                    yes_t  = next((t for t in tokens if t.get("outcome") == "Yes"), None)
                    if yes_t and yes_t.get("token_id"):
                        print(f"   {PASS} Token IDs present (e.g. {yes_t['token_id'][:20]}...)")
                    else:
                        print(f"   {WARN}Token IDs missing — check market_fetcher parsing")
                        errors.append("token_ids")
                else:
                    print(f"   {FAIL} Gamma API returned unexpected data")
    except Exception as e:
        print(f"   {FAIL} API error: {e}")
        errors.append("api")
    print()

    # 5. CLOB auth
    print("5. CLOB Authentication")
    if pk and live_ok:
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                signature_type=0,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            print(f"   {PASS} CLOB authenticated")
            print(f"   {PASS} API key: {creds.api_key[:16]}...")
        except Exception as e:
            print(f"   {FAIL} CLOB auth failed: {e}")
            errors.append("clob_auth")
    else:
        print(f"   {WARN}Skipped (paper mode)")
    print()

    # 6. Telegram
    print("6. Telegram")
    if tg_token and tg_chat:
        try:
            from telegram_alerts import TelegramAlerter
            alerter = TelegramAlerter(tg_token, tg_chat)
            await alerter.send("🔧 Polymarket Bot setup check — Telegram working ✅")
            print(f"   {PASS} Test message sent — check your Telegram")
        except Exception as e:
            print(f"   {FAIL} Telegram error: {e}")
    else:
        print(f"   {WARN}Not configured")
    print()

    # 7. Summary
    print("=" * 55)
    if errors:
        print(f"  {FAIL} {len(errors)} issue(s) found: {', '.join(errors)}")
        print("  Fix the above before going live.")
    else:
        mode = "LIVE" if (pk and live_ok) else "PAPER"
        print(f"  {PASS} All checks passed — ready for {mode} mode")
        if mode == "PAPER":
            print()
            print("  To enable live trading:")
            print("  1. pip install py-clob-client web3")
            print("  2. Add POLYMARKET_PRIVATE_KEY to .env")
            print("  3. Fund wallet with USDC on Polygon")
            print("  4. python bot.py --live --bankroll 100")
    print("=" * 55)
    print()


if __name__ == "__main__":
    asyncio.run(check_all())

# -*- coding: utf-8 -*-
"""
Paper Trading Tracker
=====================
Logs flagged opportunities and tracks resolution outcomes.
After 50+ resolved markets we can measure actual calibration
vs our probability estimates — that's how we know if the edge is real.

Storage: SQLite (paper_trades.db)
"""

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from pathlib import Path
from loguru import logger


DB_PATH = Path(__file__).parent.parent / "data" / "paper_trades.db"


@dataclass
class PaperBet:
    """A logged paper bet."""
    market_id: str
    question: str
    category: str
    side: str               # "YES" or "NO"
    market_price: float     # Price when flagged
    our_probability: float  # Our estimate
    ev: float               # Expected value
    kelly: float            # Kelly fraction
    confidence: str         # HIGH / MEDIUM / LOW
    reasoning: str
    flagged_at: datetime
    end_date: Optional[datetime]
    # Filled on resolution
    resolved: bool = False
    outcome: Optional[str] = None   # "YES" or "NO"
    profit_loss: float = 0.0        # Hypothetical P&L (assuming $100 bet)
    resolved_at: Optional[datetime] = None


class PaperTracker:
    """Tracks paper bets and measures calibration."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS paper_bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    question TEXT,
                    category TEXT,
                    side TEXT,
                    market_price REAL,
                    our_probability REAL,
                    ev REAL,
                    kelly REAL,
                    confidence TEXT,
                    reasoning TEXT,
                    flagged_at TEXT,
                    end_date TEXT,
                    resolved INTEGER DEFAULT 0,
                    outcome TEXT,
                    profit_loss REAL DEFAULT 0,
                    resolved_at TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_market_id ON paper_bets(market_id)")
            db.commit()

    def log_bet(self, bet: PaperBet) -> int:
        """Log a new paper bet. Returns row ID."""
        # Don't duplicate
        with sqlite3.connect(self.db_path) as db:
            existing = db.execute(
                "SELECT id FROM paper_bets WHERE market_id=? AND side=? AND resolved=0",
                (bet.market_id, bet.side)
            ).fetchone()
            if existing:
                logger.debug(f"Already tracking {bet.market_id} {bet.side}")
                return existing[0]

            cur = db.execute("""
                INSERT INTO paper_bets
                (market_id, question, category, side, market_price, our_probability,
                 ev, kelly, confidence, reasoning, flagged_at, end_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                bet.market_id, bet.question, bet.category, bet.side,
                bet.market_price, bet.our_probability, bet.ev, bet.kelly,
                bet.confidence, bet.reasoning,
                bet.flagged_at.isoformat(),
                bet.end_date.isoformat() if bet.end_date else None,
            ))
            db.commit()
            logger.info(f"Logged paper bet: {bet.side} on '{bet.question[:50]}' @ {bet.market_price:.2%}")
            return cur.lastrowid

    def resolve_bet(self, market_id: str, outcome: str, bet_size: float = 100.0):
        """Resolve a bet with actual outcome. Calculates P&L."""
        with sqlite3.connect(self.db_path) as db:
            bets = db.execute(
                "SELECT id, side, market_price FROM paper_bets WHERE market_id=? AND resolved=0",
                (market_id,)
            ).fetchall()

            for row_id, side, price in bets:
                won = (side == outcome)
                # P&L on $100 bet
                if won:
                    pnl = (1.0 / price - 1.0) * bet_size  # Net profit
                else:
                    pnl = -bet_size  # Lost the bet

                db.execute("""
                    UPDATE paper_bets
                    SET resolved=1, outcome=?, profit_loss=?, resolved_at=?
                    WHERE id=?
                """, (outcome, pnl, datetime.utcnow().isoformat(), row_id))

            db.commit()
            logger.info(f"Resolved {market_id}: outcome={outcome}, {len(bets)} bet(s) updated")

    def get_calibration_report(self) -> dict:
        """
        Measure how well our probability estimates are calibrated.
        Good calibration: when we say 70%, it resolves YES ~70% of the time.
        """
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("""
                SELECT our_probability, side, outcome, ev, kelly, profit_loss, confidence
                FROM paper_bets WHERE resolved=1
            """).fetchall()

        if not rows:
            return {"error": "No resolved bets yet. Need 50+ for meaningful stats."}

        total = len(rows)
        wins  = sum(1 for r in rows if r[2] == r[1])  # outcome == side
        total_pnl = sum(r[5] for r in rows)
        avg_kelly = sum(r[4] for r in rows) / total

        # Calibration buckets
        buckets = {}
        for r in rows:
            our_prob = r[0]
            bucket = round(our_prob * 10) / 10  # Round to nearest 0.1
            if bucket not in buckets:
                buckets[bucket] = {"n": 0, "wins": 0}
            buckets[bucket]["n"] += 1
            if r[2] == r[1]:
                buckets[bucket]["wins"] += 1

        calibration = {}
        for bucket, data in sorted(buckets.items()):
            actual_freq = data["wins"] / data["n"] if data["n"] > 0 else 0
            calibration[f"{bucket:.0%}"] = {
                "n": data["n"],
                "actual_frequency": actual_freq,
                "calibration_error": abs(actual_freq - bucket),
            }

        # High confidence only
        high_conf = [r for r in rows if r[6] == "HIGH"]
        high_conf_wr = sum(1 for r in high_conf if r[2] == r[1]) / len(high_conf) if high_conf else 0

        return {
            "total_resolved": total,
            "win_rate": wins / total,
            "total_pnl_usd": total_pnl,  # Assuming $100/bet
            "roi_pct": total_pnl / (total * 100) * 100,
            "avg_kelly": avg_kelly,
            "high_confidence_wr": high_conf_wr,
            "calibration_by_bucket": calibration,
            "ready_for_live": total >= 50 and total_pnl > 0,
        }

    def get_open_bets(self) -> List[dict]:
        """Get all unresolved paper bets."""
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("""
                SELECT market_id, question, side, market_price, our_probability,
                       ev, kelly, confidence, flagged_at, end_date
                FROM paper_bets WHERE resolved=0
                ORDER BY flagged_at DESC
            """).fetchall()

        return [
            {
                "market_id": r[0], "question": r[1], "side": r[2],
                "market_price": r[3], "our_prob": r[4], "ev": r[5],
                "kelly": r[6], "confidence": r[7],
                "flagged_at": r[8], "end_date": r[9],
            }
            for r in rows
        ]

    def print_status(self):
        """Print current paper trading status."""
        open_bets = self.get_open_bets()
        report    = self.get_calibration_report()

        print()
        print("=" * 60)
        print("  POLYMARKET PAPER TRACKER STATUS")
        print("=" * 60)
        print(f"  Open positions:  {len(open_bets)}")

        if "error" not in report:
            print(f"  Resolved bets:   {report['total_resolved']}")
            print(f"  Win rate:        {report['win_rate']:.1%}")
            print(f"  Total P&L:       ${report['total_pnl_usd']:+.2f} (hypothetical $100/bet)")
            print(f"  ROI:             {report['roi_pct']:+.1f}%")
            print(f"  High conf WR:    {report['high_confidence_wr']:.1%}")
            print(f"  Ready for live:  {'✅ YES' if report['ready_for_live'] else '❌ Not yet'}")
        else:
            print(f"  {report['error']}")

        if open_bets:
            print()
            print("  OPEN POSITIONS:")
            for b in open_bets[:10]:
                print(f"  [{b['confidence']}] {b['side']} @ {b['market_price']:.2%} — {b['question'][:55]}")

        print("=" * 60)
        print()

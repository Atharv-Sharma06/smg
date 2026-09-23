"""Plain data types shared by the scraper, strategies, executor and monitor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class TradeStatus(str, Enum):
    DRY_RUN = "DRY_RUN"        # validated, not sent
    REJECTED = "REJECTED"      # failed rule validation
    SUBMITTED = "SUBMITTED"    # sent to SMG, confirmation not checked
    CONFIRMED = "CONFIRMED"    # portfolio shows the change
    PENDING = "PENDING"        # SMG lists it as a pending order
    UNCONFIRMED = "UNCONFIRMED"  # sent, but not visible in portfolio or pending orders
    FAILED = "FAILED"          # error while submitting


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Holding:
    ticker: str
    shares: int
    current_price: float
    market_value: float
    purchase_price: float | None = None
    gain_loss: float | None = None
    position: str = "LONG"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PendingOrder:
    ticker: str
    action: str
    shares: int
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Portfolio:
    cash: float
    total_equity: float
    holdings: list[Holding] = field(default_factory=list)
    pending_orders: list[PendingOrder] = field(default_factory=list)
    buying_power: float | None = None
    timestamp: datetime = field(default_factory=_now)

    def holding(self, ticker: str) -> Holding | None:
        ticker = ticker.upper()
        return next((h for h in self.holdings if h.ticker == ticker), None)

    def shares_of(self, ticker: str) -> int:
        h = self.holding(ticker)
        return h.shares if h else 0

    @property
    def invested_value(self) -> float:
        return sum(h.market_value for h in self.holdings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "cash": self.cash,
            "buying_power": self.buying_power,
            "total_equity": self.total_equity,
            "holdings": [h.to_dict() for h in self.holdings],
            "pending_orders": [p.to_dict() for p in self.pending_orders],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Portfolio:
        ts = data.get("timestamp")
        return cls(
            cash=float(data["cash"]),
            total_equity=float(data["total_equity"]),
            holdings=[Holding(**h) for h in data.get("holdings", [])],
            pending_orders=[PendingOrder(**p) for p in data.get("pending_orders", [])],
            buying_power=data.get("buying_power"),
            timestamp=datetime.fromisoformat(ts) if ts else _now(),
        )


@dataclass
class LeaderboardEntry:
    rank: int
    team: str
    school: str = ""
    total_equity: float | None = None
    pct_return: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    """Output of a strategy for one ticker.

    ``score`` is in [-1, 1] (negative = bearish); ``confidence`` is in [0, 1].
    """

    ticker: str
    action: Action
    confidence: float
    score: float
    reason: str
    strategy: str
    price: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class TradeOrder:
    ticker: str
    action: Action
    shares: int
    price: float                    # latest known price, used for validation/sizing
    confidence: float = 0.0
    reason: str = ""
    exchange: str | None = None

    @property
    def notional(self) -> float:
        return self.shares * self.price


@dataclass
class ValidationResult:
    order: TradeOrder
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # human-readable validation log

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class TradeResult:
    order: TradeOrder
    status: TradeStatus
    message: str = ""
    confirmation_id: str = ""
    timestamp: datetime = field(default_factory=_now)

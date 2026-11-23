from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    effective: date
    input_per_million: Decimal
    output_per_million: Decimal
    cached_per_million: Decimal | None
    version: str


# Demo-safe illustrative pricing, deliberately not claimed as current provider pricing.
PRICES = [
    Price(
        "demo",
        "simulated-model",
        date(2024, 1, 1),
        Decimal("1"),
        Decimal("2"),
        Decimal("0.5"),
        "demo-2024-01",
    )
]


def calculate(
    provider: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    at: date,
) -> tuple[Decimal | None, str | None]:
    if input_tokens is None and output_tokens is None and cached_tokens is None:
        return None, None
    matches = [
        p for p in PRICES if p.provider == provider and p.model == model and p.effective <= at
    ]
    if not matches:
        return None, None
    price = max(matches, key=lambda p: p.effective)
    million = Decimal(1_000_000)
    total = Decimal(input_tokens or 0) * price.input_per_million / million
    total += Decimal(output_tokens or 0) * price.output_per_million / million
    if cached_tokens and price.cached_per_million is not None:
        total += Decimal(cached_tokens) * price.cached_per_million / million
    return total.quantize(Decimal("0.00000001")), price.version

"""Small helpers shared across routers."""


def _as_float(value) -> float | None:
    """Postgres NUMERIC arrives as Decimal; mixing it with float raises.
    Nulls stay null rather than becoming a misleading 0."""
    return float(value) if value is not None else None

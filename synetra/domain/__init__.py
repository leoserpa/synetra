"""Camada de Domínio: regras de negócio e indicadores financeiros."""
from .indicators import (
    BRAZIL_AFTER_TAX,
    BRAZIL_TAX_RATE,
    Categoria,
    by_category,
    calculate_all_indicators,
    only_for,
    safe_div,
)

__all__ = [
    "BRAZIL_AFTER_TAX",
    "BRAZIL_TAX_RATE",
    "Categoria",
    "by_category",
    "calculate_all_indicators",
    "only_for",
    "safe_div",
]

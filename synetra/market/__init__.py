"""Camada de Mercado — dados de cotação da B3 via Yahoo Finance.

Este módulo é **independente** da camada contábil (CVM). Pode ser desabilitado
no config.toml sem afetar o pipeline principal.
"""
from synetra.market.price_aggregator import (
    SNAPSHOT_COLS,
    VALUATION_COLS,
    attach_historical_valuation,
    attach_prices_to_history,
    build_snapshot_atual,
)
from synetra.market.yahoo_client import YahooPriceDownloader

__all__ = [
    "SNAPSHOT_COLS",
    "VALUATION_COLS",
    "YahooPriceDownloader",
    "attach_historical_valuation",
    "attach_prices_to_history",
    "build_snapshot_atual",
]

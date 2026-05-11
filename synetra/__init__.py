"""Synetra - Pipeline ETL para Análise Fundamentalista de Dados da CVM."""

__version__ = "1.1.0"

from synetra.config import load_config
from synetra.downloader import CVMDownloader
from synetra.loader import load_parquet_history, process_fre_from_zip, process_year_from_zip
from synetra.transformer import FinancialTransformer

__all__ = [
    "load_config",
    "CVMDownloader",
    "process_year_from_zip",
    "process_fre_from_zip",
    "load_parquet_history",
    "FinancialTransformer",
]

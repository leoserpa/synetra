"""Synetra - Pipeline ETL para Análise Fundamentalista de Dados da CVM."""

__version__ = "1.0.0"

from synetra.config import load_config
from synetra.downloader import CVMDownloader
from synetra.loader import process_year_from_zip, process_fre_from_zip, load_parquet_history
from synetra.transformer import FinancialTransformer
from synetra.utils import FundamentusScraper, establish_matches

__all__ = [
    "load_config",
    "CVMDownloader",
    "process_year_from_zip",
    "process_fre_from_zip",
    "load_parquet_history",
    "FinancialTransformer",
    "FundamentusScraper",
    "establish_matches",
]

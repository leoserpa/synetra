"""Cliente Yahoo Finance para cotações diárias da B3.

Responsabilidades:
    1. Baixar cotações diárias de ações brasileiras via yfinance.
    2. Gerenciar cache em Parquet (evita rebaixar quando possível).
    3. Tratamento resiliente de tickers inexistentes/com falha.

Convenção:
    - Tickers brasileiros precisam do sufixo ``.SA`` no Yahoo.
    - Preços BRUTOS (``auto_adjust=False``) para casar com valores de mercado.

Nota arquitetural:
    Este cliente trabalha apenas com fechamentos diários. O "snapshot atual"
    do pipeline é derivado do último fechamento disponível no histórico
    (ver ``price_aggregator.build_snapshot_atual``), evitando uma segunda
    viagem à API do Yahoo para cotação intraday.
"""
from __future__ import annotations

import math
import pathlib
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

import polars as pl
import yfinance as yf
from loguru import logger

# --- Constantes ---

#: Conversão de dias para segundos (expiração do cache).
_SECONDS_PER_DAY: Final[int] = 86_400

#: Sufixo que o Yahoo Finance exige para tickers brasileiros.
_YAHOO_SA_SUFFIX: Final[str] = ".SA"

#: Data default de início para o histórico (suficiente para análise de décadas).
_DEFAULT_START_DATE: Final[str] = "2010-01-01"

#: Delay entre batches para não sobrecarregar o Yahoo (politeness).
_INTER_BATCH_DELAY_SECONDS: Final[int] = 1

#: Nome do arquivo de cache histórico.
_CACHE_HISTORICAL_FILENAME: Final[str] = "cotacoes_diarias.parquet"

#: Valor do expire = 0 → comportamento "nunca cacheado".
_NEVER_FRESH: Final[int] = 0


# --- Helpers públicos ---


def _is_nan(x: float | int | None) -> bool:
    """Verifica se valor é NaN (funciona para floats numpy e Python).

    Args:
        x: Valor a testar. ``None`` conta como NaN (ausência de dado).

    Returns:
        ``True`` se for ``None`` ou NaN, ``False`` caso contrário.
    """
    if x is None:
        return True
    try:
        return bool(isinstance(x, float) and math.isnan(x))
    except (TypeError, ValueError):
        return False


# --- Cache em Parquet ---


@dataclass(frozen=True)
class _ParquetCache:
    """Gerenciador de cache em Parquet com expiração por idade do arquivo.

    Usa ``mtime`` do arquivo como proxy de "quando foi baixado pela última vez".

    Attributes:
        path: Caminho do Parquet.
        max_age_seconds: Idade máxima em segundos antes de considerar stale.
            Zero significa "nunca fresh" — útil para forçar rebaixamento.
        label: Rótulo humano usado em logs (ex: ``"cotações"``).
    """

    path: pathlib.Path
    max_age_seconds: int
    label: str

    def is_fresh(self) -> bool:
        """Retorna ``True`` se o arquivo existe e está dentro da validade."""
        if self.max_age_seconds == _NEVER_FRESH:
            return False
        if not self.path.exists():
            return False
        age_seconds = time.time() - self.path.stat().st_mtime
        return age_seconds < self.max_age_seconds

    def load(self) -> pl.DataFrame | None:
        """Carrega o DataFrame do cache se estiver fresco e íntegro.

        Returns:
            DataFrame se conseguir ler; ``None`` se stale, inexistente ou
            corrompido (nesse caso loga warning, não propaga exceção).
        """
        if not self.is_fresh():
            return None
        try:
            return pl.read_parquet(self.path)
        except (pl.exceptions.ComputeError, OSError, ValueError) as exc:
            logger.warning(
                "Cache de {} corrompido ({}). Rebaixando.", self.label, exc
            )
            return None

    def save(self, df: pl.DataFrame) -> None:
        """Salva o DataFrame em Parquet com compressão zstd."""
        df.write_parquet(self.path, compression="zstd")
        logger.info(
            "Cache de {} salvo: {} ({} linhas)", self.label, self.path, df.height
        )


# --- Helpers de parsing da resposta do Yahoo ---


def _to_yahoo_tickers(tickers: list[str]) -> list[str]:
    """Adiciona sufixo ``.SA`` aos tickers B3 para uso no Yahoo."""
    return [f"{t}{_YAHOO_SA_SUFFIX}" for t in tickers]


def _optional_float(value: Any) -> float | None:
    """Converte ``value`` em float se possível, ou ``None`` se NaN/ausente."""
    if _is_nan(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_yahoo_ohlcv_row(ticker: str, row: Any) -> dict | None:
    """Converte uma linha de OHLCV do pandas (yfinance) em dict normalizado.

    Args:
        ticker: Nome do ticker (sem o sufixo ``.SA``).
        row: Linha do DataFrame pandas devolvido por ``yfinance.download``.

    Returns:
        Dict pronto para virar linha Polars, ou ``None`` se Close for NaN
        (dia sem pregão — filtramos fora).
    """
    close_raw = row.get("Close")
    if _is_nan(close_raw):
        return None

    close = float(close_raw)
    adj_close = _optional_float(row.get("Adj Close"))

    return {
        "TICKER": ticker,
        "DATA": row["Date"].strftime("%Y-%m-%d"),
        "ABERTURA": _optional_float(row.get("Open")),
        "MAXIMA": _optional_float(row.get("High")),
        "MINIMA": _optional_float(row.get("Low")),
        "FECHAMENTO": close,
        "FECHAMENTO_AJUSTADO": adj_close if adj_close is not None else close,
        "VOLUME": _optional_float(row.get("Volume")) or 0.0,
    }


# --- YahooPriceDownloader ---


class YahooPriceDownloader:
    """Baixa e cacheia cotações diárias da B3 via Yahoo Finance.

    Attributes:
        cache_dir: Pasta onde o Parquet de cotações é salvo.
        cache_file: Caminho completo do cache de histórico (dias).
        cache_max_age_days: Idade máxima do cache em dias.
    """

    def __init__(
        self,
        cache_dir: str = ".synetra_cache",
        cache_max_age_days: int = 7,
    ) -> None:
        self.cache_dir: pathlib.Path = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        # Mantido como atributo público por retrocompatibilidade com testes.
        self.cache_file: pathlib.Path = self.cache_dir / _CACHE_HISTORICAL_FILENAME
        self.cache_max_age_days: int = cache_max_age_days

        self._historical_cache = _ParquetCache(
            path=self.cache_file,
            max_age_seconds=cache_max_age_days * _SECONDS_PER_DAY,
            label="cotações",
        )

    # --- Compatibilidade com testes existentes ---

    def _cache_is_fresh(self) -> bool:
        """Retorna ``True`` se o cache histórico está fresco."""
        return self._historical_cache.is_fresh()

    def _load_cache(self) -> pl.DataFrame | None:
        """Carrega o cache histórico (``None`` se stale/corrompido)."""
        return self._historical_cache.load()

    def _save_cache(self, df: pl.DataFrame) -> None:
        """Salva DataFrame no cache histórico."""
        self._historical_cache.save(df)

    # --- API pública: Download histórico diário ---

    def download(
        self,
        tickers: Iterable[str],
        inicio: str = _DEFAULT_START_DATE,
        use_cache: bool = True,
        batch_size: int = 100,
    ) -> pl.DataFrame:
        """Baixa cotações diárias de múltiplos tickers brasileiros.

        Args:
            tickers: Lista de tickers B3 (sem ``.SA`` — adicionamos automaticamente).
            inicio: Data inicial no formato ``YYYY-MM-DD``.
            use_cache: Se ``True`` e cache existe/fresco, usa cache.
            batch_size: Quantos tickers por chamada (Yahoo limita ~100 por request).

        Returns:
            DataFrame com colunas: ``TICKER``, ``DATA``, ``ABERTURA``, ``MAXIMA``,
            ``MINIMA``, ``FECHAMENTO``, ``FECHAMENTO_AJUSTADO``, ``VOLUME``.
        """
        tickers_list = sorted(set(tickers))
        logger.info("Yahoo Finance: {} tickers solicitados", len(tickers_list))

        if use_cache:
            cached = self._try_use_historical_cache(tickers_list)
            if cached is not None:
                return cached

        registros = self._download_all(tickers_list, inicio, batch_size)
        if not registros:
            logger.warning("Nenhum dado baixado do Yahoo.")
            return pl.DataFrame()

        df = pl.DataFrame(registros)
        self._log_historical_summary(df)

        if use_cache:
            self._save_cache(df)
        return df

    def _try_use_historical_cache(
        self, tickers_list: list[str]
    ) -> pl.DataFrame | None:
        """Retorna DataFrame cacheado se cobre todos os tickers; senão ``None``."""
        cached = self._load_cache()
        if cached is None:
            return None

        cached_tickers = set(cached["TICKER"].unique().to_list())
        faltantes = [t for t in tickers_list if t not in cached_tickers]
        if faltantes:
            logger.info(
                "Cache parcial: {} tickers faltando. Rebaixando tudo.",
                len(faltantes),
            )
            return None

        logger.info(
            "Cache de cotações válido — usando {} linhas cacheadas", cached.height
        )
        return cached.filter(pl.col("TICKER").is_in(tickers_list))

    @staticmethod
    def _log_historical_summary(df: pl.DataFrame) -> None:
        """Loga tamanho e cobertura do DataFrame baixado."""
        n_tickers = df["TICKER"].n_unique()
        n_pregoes = df.height // max(n_tickers, 1)
        logger.info(
            "Yahoo Finance: {} linhas baixadas ({} tickers × {} pregões)",
            df.height, n_tickers, n_pregoes,
        )

    def _download_all(
        self, tickers: list[str], inicio: str, batch_size: int
    ) -> list[dict]:
        """Baixa todos os tickers em batches (Yahoo limita ~100 por request)."""
        registros: list[dict] = []
        total_batches = (len(tickers) + batch_size - 1) // batch_size

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(
                "Baixando batch {}/{} ({} tickers)...",
                batch_num, total_batches, len(batch),
            )
            registros.extend(self._download_batch(batch, inicio))

            if batch_num < total_batches:
                time.sleep(_INTER_BATCH_DELAY_SECONDS)

        return registros

    def _download_batch(self, tickers: list[str], inicio: str) -> list[dict]:
        """Baixa um batch de tickers em uma única chamada ao Yahoo."""
        data = self._safe_yf_download(tickers, inicio)
        if data is None or data.empty:
            return []

        return self._extract_ohlcv_records(data, tickers)

    @staticmethod
    def _safe_yf_download(tickers: list[str], inicio: str) -> Any:
        """Chama ``yf.download`` capturando erros de rede/API comuns."""
        tickers_yahoo = _to_yahoo_tickers(tickers)
        try:
            return yf.download(
                tickers_yahoo,
                start=inicio,
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
            )
        except (ConnectionError, TimeoutError, ValueError, KeyError) as exc:
            logger.error("Falha no batch [{}...]: {}", tickers[:3], exc)
            return None

    @staticmethod
    def _extract_ohlcv_records(data: Any, tickers: list[str]) -> list[dict]:
        """Extrai linhas OHLCV do DataFrame pandas multi-index do yfinance."""
        registros: list[dict] = []
        available = data.columns.get_level_values(0)

        for ticker in tickers:
            ticker_yf = f"{ticker}{_YAHOO_SA_SUFFIX}"
            if ticker_yf not in available:
                continue

            try:
                df_ticker = data[ticker_yf].dropna(how="all").reset_index()
            except (KeyError, AttributeError) as exc:
                logger.debug("Ticker {} inacessível: {}", ticker, exc)
                continue

            for _, row in df_ticker.iterrows():
                parsed = _parse_yahoo_ohlcv_row(ticker, row)
                if parsed is not None:
                    registros.append(parsed)

        return registros

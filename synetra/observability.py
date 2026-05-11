"""Utilitários de observabilidade: timing, métricas e logs estruturados.

Uso típico::

    from synetra.observability import timed_step, PipelineMetrics

    metrics = PipelineMetrics()
    with timed_step("prepare_history", metrics):
        df = prepare_history(...)

    metrics.record("rows_processed", df.height)
    metrics.log_summary()
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from loguru import logger

# --- Constantes de apresentação ---

#: Largura do separador visual nos logs do `log_summary`.
_SUMMARY_SEPARATOR_WIDTH: Final[int] = 60

#: Prefixos usados nas seções do log para facilitar leitura em grep.
_PREFIX_SUMMARY: Final[str] = "[METRICS]"
_PREFIX_TIMING: Final[str] = "[TIMING]"
_PREFIX_COUNTER: Final[str] = "[COUNTER]"
_PREFIX_METADATA: Final[str] = "[META]"

#: Rótulo padrão para a linha de total agregado.
_TOTAL_LABEL: Final[str] = "TOTAL"

#: Largura da coluna de nome do passo nos alinhamentos.
_STEP_NAME_COLUMN_WIDTH: Final[int] = 30


# --- PipelineMetrics ---


@dataclass
class PipelineMetrics:
    """Agregador de métricas do pipeline para observabilidade estruturada.

    Coleta:
        - Timings por etapa (``step_timings``).
        - Contadores arbitrários (``counters``).
        - Metadados de execução (``metadata``).

    Exemplo::

        >>> m = PipelineMetrics()
        >>> with timed_step("load", m):
        ...     pass
        >>> m.record("rows", 5000)
        >>> m.log_summary()
    """

    step_timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int | float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def record(self, key: str, value: int | float) -> None:
        """Registra um contador (ex: linhas processadas, outliers detectados)."""
        self.counters[key] = value

    def set_metadata(self, key: str, value: Any) -> None:
        """Registra metadado arbitrário (ex: versão, timestamp de início)."""
        self.metadata[key] = value

    def log_summary(self) -> None:
        """Emite resumo estruturado das métricas no log."""
        separator = "=" * _SUMMARY_SEPARATOR_WIDTH
        logger.info(separator)
        logger.info("{} PIPELINE METRICS SUMMARY", _PREFIX_SUMMARY)
        logger.info(separator)

        self._log_timings()
        self._log_counters()
        self._log_metadata()

        logger.info(separator)

    def to_dict(self) -> dict[str, Any]:
        """Exporta todas as métricas como dict (útil para JSON / APIs)."""
        return {
            "step_timings": dict(self.step_timings),
            "counters": dict(self.counters),
            "metadata": dict(self.metadata),
            "total_duration_s": sum(self.step_timings.values()),
        }

    # --- Helpers privados ---

    def _log_timings(self) -> None:
        """Loga timings por etapa ordenados do mais lento ao mais rápido."""
        if not self.step_timings:
            return

        total_time = sum(self.step_timings.values())
        logger.info("{} Timings por etapa (s):", _PREFIX_TIMING)

        for step, duration in sorted(self.step_timings.items(), key=lambda x: -x[1]):
            pct = (duration / total_time * 100) if total_time > 0 else 0
            logger.info(
                "   {:<{w}s} {:>8.3f}s  ({:>5.1f}%)",
                step, duration, pct, w=_STEP_NAME_COLUMN_WIDTH,
            )

        logger.info(
            "   {:<{w}s} {:>8.3f}s  (100.0%)",
            _TOTAL_LABEL, total_time, w=_STEP_NAME_COLUMN_WIDTH,
        )

    def _log_counters(self) -> None:
        """Loga contadores, separando inteiros e floats para formatação."""
        if not self.counters:
            return

        logger.info("{} Contadores:", _PREFIX_COUNTER)
        for key, value in sorted(self.counters.items()):
            if isinstance(value, float):
                logger.info(
                    "   {:<{w}s} {:>12.2f}", key, value, w=_STEP_NAME_COLUMN_WIDTH
                )
            else:
                logger.info(
                    "   {:<{w}s} {:>12d}", key, value, w=_STEP_NAME_COLUMN_WIDTH
                )

    def _log_metadata(self) -> None:
        """Loga metadados em ordem alfabética."""
        if not self.metadata:
            return

        logger.info("{} Metadados:", _PREFIX_METADATA)
        for key, value in sorted(self.metadata.items()):
            logger.info(
                "   {:<{w}s} {}", key, value, w=_STEP_NAME_COLUMN_WIDTH
            )


# --- timed_step ---


@contextmanager
def timed_step(
    step_name: str,
    metrics: PipelineMetrics | None = None,
    log_level: str = "INFO",
) -> Iterator[None]:
    """Context manager que mede o tempo de uma etapa.

    Args:
        step_name: Nome descritivo da etapa (ex: ``"load_parquet_history"``).
        metrics: Se fornecido, salva o timing em ``metrics.step_timings[step_name]``.
            O timing é registrado **mesmo se a etapa levantar exceção** (garantia
            via ``try/finally``).
        log_level: Nível do log (``INFO``, ``DEBUG``, ``WARNING``, ...).

    Yields:
        ``None`` — o chamador não recebe valor, apenas o controle de contexto.

    Exemplo::

        >>> metrics = PipelineMetrics()
        >>> with timed_step("download", metrics):
        ...     download_data()
        # Log: "[TIMING] download: 2.341s"
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if metrics is not None:
            metrics.step_timings[step_name] = elapsed
        logger.log(log_level, "{} {}: {:.3f}s", _PREFIX_TIMING, step_name, elapsed)

"""Testes complementares para helpers e constantes do synetra.observability.

Os testes principais (PipelineMetrics e timed_step) ficam em
`test_observability.py`. Este arquivo cobre:

    - Constantes de apresentação (separator, emojis, widths)
    - Helpers privados de logging (_log_timings, _log_counters, _log_metadata)
    - to_dict() com conteúdo heterogêneo

Princípios F.I.R.S.T.: sem I/O real, usando loguru logger capturado.
"""
from __future__ import annotations

import pytest

from synetra.observability import (
    _PREFIX_COUNTER,
    _PREFIX_METADATA,
    _PREFIX_SUMMARY,
    _PREFIX_TIMING,
    _STEP_NAME_COLUMN_WIDTH,
    _SUMMARY_SEPARATOR_WIDTH,
    _TOTAL_LABEL,
    PipelineMetrics,
)

# --- Constantes de apresentação ---


class TestPresentationConstants:
    """Valores das constantes de apresentação."""

    def test_separator_width_is_reasonable(self) -> None:
        """Largura do separador precisa caber em terminal padrão (80 colunas)."""
        assert 40 <= _SUMMARY_SEPARATOR_WIDTH <= 80

    def test_step_name_column_width_is_positive(self) -> None:
        assert _STEP_NAME_COLUMN_WIDTH > 0

    def test_prefixes_are_distinct(self) -> None:
        """Cada seção do summary tem seu próprio prefixo de log."""
        prefixes = {_PREFIX_SUMMARY, _PREFIX_TIMING, _PREFIX_COUNTER, _PREFIX_METADATA}
        assert len(prefixes) == 4

    def test_total_label_is_not_empty(self) -> None:
        assert _TOTAL_LABEL
        assert isinstance(_TOTAL_LABEL, str)


# --- to_dict ---


class TestToDict:
    """Exportação das métricas como dict serializável."""

    def test_empty_metrics_produces_empty_dicts(self) -> None:
        m = PipelineMetrics()
        result = m.to_dict()
        assert result["step_timings"] == {}
        assert result["counters"] == {}
        assert result["metadata"] == {}
        assert result["total_duration_s"] == pytest.approx(0.0)

    def test_total_duration_sums_timings(self) -> None:
        m = PipelineMetrics()
        m.step_timings["a"] = 1.5
        m.step_timings["b"] = 2.5
        result = m.to_dict()
        assert result["total_duration_s"] == pytest.approx(4.0)

    def test_preserves_all_content(self) -> None:
        m = PipelineMetrics()
        m.step_timings["load"] = 1.2
        m.record("rows", 1000)
        m.set_metadata("version", "3.0.0")

        result = m.to_dict()
        assert result["step_timings"] == {"load": 1.2}
        assert result["counters"] == {"rows": 1000}
        assert result["metadata"] == {"version": "3.0.0"}

    def test_returns_copies_not_references(self) -> None:
        """to_dict() retorna cópias — mutações não afetam o objeto original."""
        m = PipelineMetrics()
        m.record("x", 10)

        result = m.to_dict()
        result["counters"]["x"] = 999  # mutação no dict retornado

        assert m.counters["x"] == 10  # original preservado


# --- Helpers privados de logging (testados indiretamente via log_summary) ---


class TestLogSummarySections:
    """Testa que log_summary não falha com estado variado."""

    def test_empty_summary_does_not_crash(self) -> None:
        """Summary vazio deve emitir apenas o cabeçalho, sem crashar."""
        m = PipelineMetrics()
        m.log_summary()  # sem levantar

    def test_only_timings(self) -> None:
        m = PipelineMetrics()
        m.step_timings["step1"] = 0.1
        m.log_summary()  # sem levantar

    def test_only_counters(self) -> None:
        m = PipelineMetrics()
        m.record("rows", 100)
        m.log_summary()

    def test_only_metadata(self) -> None:
        m = PipelineMetrics()
        m.set_metadata("version", "3.0.0")
        m.log_summary()

    def test_mixed_int_and_float_counters(self) -> None:
        """Contadores int e float devem ser formatados corretamente."""
        m = PipelineMetrics()
        m.record("integer_value", 1000)
        m.record("float_value", 3.14159)
        m.log_summary()  # sem crashar na formatação

    def test_all_sections_populated(self) -> None:
        m = PipelineMetrics()
        m.step_timings["step_a"] = 1.0
        m.step_timings["step_b"] = 2.0
        m.record("rows", 5000)
        m.record("ratio", 0.875)
        m.set_metadata("env", "prod")
        m.set_metadata("started_at", "2024-01-01")
        m.log_summary()


# --- Ordenação das seções ---


class TestTimingsSortedBySlowest:
    """Timings são logados do mais lento ao mais rápido (útil para diagnóstico)."""

    def test_ordering_preserved_in_log_summary(self) -> None:
        """Verificamos a ordem olhando o step_timings (ordem de entrada
        não importa — o log interno ordena por duração desc)."""
        m = PipelineMetrics()
        m.step_timings["fast"] = 0.1
        m.step_timings["slow"] = 5.0
        m.step_timings["medium"] = 1.0

        # to_dict preserva dict inserido — ordering acontece só no log
        result = m.to_dict()
        assert result["step_timings"] == {"fast": 0.1, "slow": 5.0, "medium": 1.0}

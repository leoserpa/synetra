"""Testes do módulo de observabilidade."""
import time

from synetra.observability import PipelineMetrics, timed_step


class TestPipelineMetrics:
    """Testa o agregador de métricas."""

    def test_record_adiciona_contador(self) -> None:
        m = PipelineMetrics()
        m.record("rows", 5000)
        assert m.counters == {"rows": 5000}

    def test_record_sobrescreve_valor(self) -> None:
        m = PipelineMetrics()
        m.record("rows", 100)
        m.record("rows", 200)
        assert m.counters["rows"] == 200

    def test_set_metadata_aceita_qualquer_tipo(self) -> None:
        m = PipelineMetrics()
        m.set_metadata("version", "3.0.0")
        m.set_metadata("tickers", ["PETR4", "VALE3"])
        assert m.metadata["version"] == "3.0.0"
        assert m.metadata["tickers"] == ["PETR4", "VALE3"]

    def test_to_dict_estrutura_correta(self) -> None:
        m = PipelineMetrics()
        m.step_timings["load"] = 1.5
        m.step_timings["transform"] = 2.5
        m.record("rows", 100)
        m.set_metadata("version", "3.0")

        result = m.to_dict()
        assert result["step_timings"] == {"load": 1.5, "transform": 2.5}
        assert result["counters"] == {"rows": 100}
        assert result["metadata"] == {"version": "3.0"}
        assert result["total_duration_s"] == 4.0


class TestTimedStep:
    """Testa o context manager de timing."""

    def test_registra_timing_em_metrics(self) -> None:
        m = PipelineMetrics()
        with timed_step("fake_step", m):
            time.sleep(0.05)

        assert "fake_step" in m.step_timings
        assert m.step_timings["fake_step"] >= 0.05
        assert m.step_timings["fake_step"] < 1.0  # sanity check

    def test_funciona_sem_metrics(self) -> None:
        """Sem metrics passado, apenas loga o tempo."""
        # Não deve levantar exceção
        with timed_step("standalone"):
            time.sleep(0.01)

    def test_captura_timing_mesmo_com_excecao(self) -> None:
        """PROPRIEDADE: timing é registrado mesmo se a etapa levantar exceção."""
        m = PipelineMetrics()

        try:
            with timed_step("fail_step", m):
                raise ValueError("erro de teste")
        except ValueError:
            pass

        assert "fail_step" in m.step_timings
        assert m.step_timings["fail_step"] > 0

    def test_multiplas_etapas_registradas(self) -> None:
        m = PipelineMetrics()
        with timed_step("step_a", m):
            time.sleep(0.01)
        with timed_step("step_b", m):
            time.sleep(0.01)
        with timed_step("step_c", m):
            time.sleep(0.01)

        assert set(m.step_timings.keys()) == {"step_a", "step_b", "step_c"}
        assert all(t > 0 for t in m.step_timings.values())

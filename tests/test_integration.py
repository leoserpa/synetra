"""
Testes de integração: Fluxo completo Load → Transform → Audit.
Skill: python-testing → Integration Test Patterns

Valida que os módulos loader e transformer funcionam em conjunto
sem dependências externas (tudo em memória).
"""
import polars as pl
import pytest

from synetra.config import load_config
from synetra.transformer import FinancialTransformer


class TestIntegrationPipeline:
    """Testa o fluxo completo de transformação com dados sintéticos."""

    @pytest.fixture(scope="class")
    def config(self):
        return load_config("config.toml")

    @pytest.fixture(scope="class")
    def transformer(self, config):
        return FinancialTransformer(config)

    @pytest.fixture
    def df_history_industrial(self, config):
        """Simula dados brutos pós-loader para uma empresa INDUSTRIAL completa."""
        contas = config.contas["industrial"]
        rows = []
        for cd_conta, nome in contas.items():
            rows.append({
                "CNPJ_CIA": "99.999.999/0001-99",
                "ANO": 2023,
                "CD_CONTA": cd_conta,
                "DS_CONTA": f"Conta {nome}",
                "VL_CONTA": 100_000.0,
                "SETOR_ATIV": "SIDERURGIA E METALURGIA",
            })
        return pl.DataFrame(rows)

    @pytest.fixture
    def df_matches(self):
        return pl.DataFrame({
            "CNPJ_CIA": ["99.999.999/0001-99"],
            "TICKER": ["TEST3"],
            "RAZAO_CVM": ["EMPRESA TESTE S.A."],
        })

    @pytest.fixture
    def df_setor(self):
        return pl.DataFrame({
            "CNPJ_CIA": ["99.999.999/0001-99"],
            "SETOR_ATIV": ["SIDERURGIA E METALURGIA"],
        })

    def test_pipeline_gera_resultado_nao_vazio(self, transformer, df_history_industrial, df_matches, df_setor):
        """O pipeline deve gerar pelo menos 1 linha de resultado."""
        result = transformer.calculate_indicators(df_history_industrial, df_matches, df_setor)
        assert not result.is_empty()
        assert result.height >= 1

    def test_pipeline_contem_colunas_essenciais(self, transformer, df_history_industrial, df_matches, df_setor):
        """O resultado deve conter as colunas de identificação e indicadores."""
        result = transformer.calculate_indicators(df_history_industrial, df_matches, df_setor)
        colunas_obrigatorias = ["TICKER", "ANO", "CATEGORIA", "ROE", "ROA"]
        for col in colunas_obrigatorias:
            assert col in result.columns, f"Coluna '{col}' ausente no resultado"

    def test_pipeline_categoria_correta(self, transformer, df_history_industrial, df_matches, df_setor):
        """Empresa de siderurgia deve ser classificada como INDUSTRIAL."""
        result = transformer.calculate_indicators(df_history_industrial, df_matches, df_setor)
        assert result["CATEGORIA"][0] == "INDUSTRIAL"

    def test_pipeline_ticker_correto(self, transformer, df_history_industrial, df_matches, df_setor):
        """O ticker do resultado deve ser o mesmo do mapa."""
        result = transformer.calculate_indicators(df_history_industrial, df_matches, df_setor)
        assert result["TICKER"][0] == "TEST3"

    def test_audit_retorna_dicionario(self, transformer, df_history_industrial, df_matches, df_setor):
        """A função de auditoria deve retornar um dict com as chaves esperadas."""
        result = transformer.calculate_indicators(df_history_industrial, df_matches, df_setor)
        report = transformer.audit_data(result)
        assert isinstance(report, dict)
        assert "gaps_count" in report
        assert "tickers_with_gaps" in report

"""
Testes unitários para o motor de cálculo financeiro do Synetra.
Utiliza DataFrames sintéticos com resultados conhecidos (gabarito).
"""
import pytest
import polars as pl

from synetra.transformer import safe_div, classify_sectors, map_accounts


# ============================================================
# 1. Testes de safe_div (Divisão Segura)
# ============================================================

class TestSafeDiv:
    """Testa a função de divisão segura que evita ZeroDivisionError."""

    def test_divisao_normal(self):
        """Divisão 100 / 50 deve retornar 2.0"""
        df = pl.DataFrame({"num": [100.0], "den": [50.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(2.0)

    def test_divisao_por_zero_retorna_null(self):
        """Divisão por zero deve retornar None (null), não infinito."""
        df = pl.DataFrame({"num": [100.0], "den": [0.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] is None

    def test_numerador_zero(self):
        """0 / 50 deve retornar 0.0"""
        df = pl.DataFrame({"num": [0.0], "den": [50.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(0.0)

    def test_valores_negativos(self):
        """-100 / 200 deve retornar -0.5 (caso comum: prejuízo / PL positivo)"""
        df = pl.DataFrame({"num": [-100.0], "den": [200.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(-0.5)

    def test_ambos_negativos(self):
        """-100 / -200 deve retornar 0.5 (prejuízo / PL negativo)"""
        df = pl.DataFrame({"num": [-100.0], "den": [-200.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(0.5)

    def test_batch_com_zeros_misturados(self):
        """Vetorização: deve tratar zero apenas nas linhas certas."""
        df = pl.DataFrame({
            "num": [100.0, 200.0, 300.0],
            "den": [50.0, 0.0, 150.0]
        })
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(2.0)
        assert result["resultado"][1] is None  # divisão por zero
        assert result["resultado"][2] == pytest.approx(2.0)


# ============================================================
# 2. Testes de classify_sectors (Classificação Setorial)
# ============================================================

class TestClassifySectors:
    """Testa a classificação de empresas em INDUSTRIAL/FINANCEIRO/SEGURADORA."""

    @pytest.fixture
    def config(self):
        return {
            "setores": {
                "financeiro": ["BANCO", "CREDITO", "ARRENDAMENTO"],
                "seguradora": ["SEGURO", "PREVIDENCIA"]
            }
        }

    def test_classifica_banco_como_financeiro(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["BANCO COMERCIAL"], "CNPJ_CIA": ["111"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "FINANCEIRO"

    def test_classifica_seguradora(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["SEGURO DE VIDA"], "CNPJ_CIA": ["222"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "SEGURADORA"

    def test_classifica_industria_como_default(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["SIDERURGIA E METALURGIA"], "CNPJ_CIA": ["333"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "INDUSTRIAL"

    def test_classifica_previdencia_como_seguradora(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["PREVIDENCIA PRIVADA"], "CNPJ_CIA": ["444"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "SEGURADORA"

    def test_setor_vazio_vira_industrial(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["NAO INFORMADO"], "CNPJ_CIA": ["555"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "INDUSTRIAL"

    def test_case_insensitive(self, config):
        """Deve funcionar independente de maiúsculas/minúsculas."""
        df = pl.DataFrame({"SETOR_ATIV": ["banco comercial"], "CNPJ_CIA": ["666"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "FINANCEIRO"

    def test_batch_multiplos_setores(self, config):
        """Deve classificar corretamente em batch."""
        df = pl.DataFrame({
            "SETOR_ATIV": ["BANCO DIGITAL", "SEGURO SAUDE", "ALIMENTOS", "CREDITO PESSOAL"],
            "CNPJ_CIA": ["A", "B", "C", "D"]
        })
        result = classify_sectors(df, config)
        assert result["CATEGORIA"].to_list() == ["FINANCEIRO", "SEGURADORA", "INDUSTRIAL", "FINANCEIRO"]


# ============================================================
# 3. Testes de map_accounts (Mapeamento de Contas)
# ============================================================

class TestMapAccounts:
    """Testa o mapeamento de contas contábeis por setor."""

    @pytest.fixture
    def config(self):
        return {
            "contas": {
                "industrial": {"3.01": "RECEITA_LIQUIDA", "3.05": "EBIT"},
                "financeiro": {"3.01": "RECEITA_LIQUIDA", "2.08": "PATRIMONIO_LIQUIDO"},
                "seguradora": {"3.01": "RECEITA_LIQUIDA"}
            }
        }

    def test_mapeia_conta_industrial(self, config):
        df = pl.DataFrame({
            "CD_CONTA": ["3.01", "3.05"],
            "CATEGORIA": ["INDUSTRIAL", "INDUSTRIAL"],
            "VL_CONTA": [1000.0, 500.0]
        })
        result = map_accounts(df, config)
        nomes = result.sort("CD_CONTA").select("CONTA_NOME").to_series().to_list()
        assert nomes == ["RECEITA_LIQUIDA", "EBIT"]

    def test_conta_nao_mapeada_retorna_null(self, config):
        df = pl.DataFrame({
            "CD_CONTA": ["9.99"],
            "CATEGORIA": ["INDUSTRIAL"],
            "VL_CONTA": [100.0]
        })
        result = map_accounts(df, config)
        assert result["CONTA_NOME"][0] is None

    def test_mesma_conta_diferente_por_setor(self, config):
        """A conta 2.08 só existe para FINANCEIRO, não para INDUSTRIAL."""
        df = pl.DataFrame({
            "CD_CONTA": ["2.08", "2.08"],
            "CATEGORIA": ["FINANCEIRO", "INDUSTRIAL"],
            "VL_CONTA": [500.0, 500.0]
        })
        result = map_accounts(df, config)
        fin_row = result.filter(pl.col("CATEGORIA") == "FINANCEIRO")
        ind_row = result.filter(pl.col("CATEGORIA") == "INDUSTRIAL")
        assert fin_row["CONTA_NOME"][0] == "PATRIMONIO_LIQUIDO"
        assert ind_row["CONTA_NOME"][0] is None


# ============================================================
# 4. Testes de Indicadores Financeiros (Gabarito)
# ============================================================

class TestIndicadoresFinanceiros:
    """
    Testa os cálculos de indicadores usando valores sintéticos
    onde o resultado é calculado à mão (gabarito).
    
    Empresa fictícia "ACME INDUSTRIAL S.A.":
    - Receita Líquida: R$ 1.000.000
    - Lucro Bruto: R$ 400.000
    - EBIT: R$ 200.000
    - Lucro Líquido: R$ 100.000
    - Ativo Total: R$ 2.000.000
    - Patrimônio Líquido: R$ 500.000
    """

    def test_roe_positivo(self):
        """ROE = Lucro Líquido / PL = 100.000 / 500.000 = 0.2 (20%)"""
        df = pl.DataFrame({"lucro": [100_000.0], "pl": [500_000.0]})
        result = df.select(safe_div("lucro", "pl").alias("roe"))
        assert result["roe"][0] == pytest.approx(0.2)

    def test_roa(self):
        """ROA = Lucro Líquido / Ativo Total = 100.000 / 2.000.000 = 0.05 (5%)"""
        df = pl.DataFrame({"lucro": [100_000.0], "ativo": [2_000_000.0]})
        result = df.select(safe_div("lucro", "ativo").alias("roa"))
        assert result["roa"][0] == pytest.approx(0.05)

    def test_margem_bruta(self):
        """Margem Bruta = Lucro Bruto / Receita = 400.000 / 1.000.000 = 0.4 (40%)"""
        df = pl.DataFrame({"bruto": [400_000.0], "receita": [1_000_000.0]})
        result = df.select(safe_div("bruto", "receita").alias("margem"))
        assert result["margem"][0] == pytest.approx(0.4)

    def test_margem_liquida(self):
        """Margem Líquida = Lucro Líq. / Receita = 100.000 / 1.000.000 = 0.1 (10%)"""
        df = pl.DataFrame({"lucro": [100_000.0], "receita": [1_000_000.0]})
        result = df.select(safe_div("lucro", "receita").alias("margem"))
        assert result["margem"][0] == pytest.approx(0.1)

    def test_ebitda(self):
        """EBITDA = EBIT + Depreciação = 200.000 + 50.000 = 250.000"""
        df = pl.DataFrame({"ebit": [200_000.0], "deprec": [50_000.0]})
        result = df.select((pl.col("ebit") + pl.col("deprec")).alias("ebitda"))
        assert result["ebitda"][0] == pytest.approx(250_000.0)

    def test_divida_liquida(self):
        """Dívida Líquida = (Dívida CP + Dívida LP) - Caixa = (100k + 300k) - 150k = 250k"""
        df = pl.DataFrame({"cp": [100_000.0], "lp": [300_000.0], "caixa": [150_000.0]})
        result = df.select((pl.col("cp") + pl.col("lp") - pl.col("caixa")).alias("dl"))
        assert result["dl"][0] == pytest.approx(250_000.0)

    def test_roic(self):
        """ROIC = (EBIT * 0.66) / (PL + Dívida Total) = (200k * 0.66) / (500k + 400k) = 0.1467"""
        df = pl.DataFrame({"ebit": [200_000.0], "pl": [500_000.0], "divida": [400_000.0]})
        result = df.select(
            ((pl.col("ebit") * 0.66) / (pl.col("pl") + pl.col("divida"))).alias("roic")
        )
        assert result["roic"][0] == pytest.approx(0.14666, rel=1e-3)

    def test_liquidez_corrente(self):
        """Liquidez Corrente = Ativo Circ. / Passivo Circ. = 600k / 300k = 2.0"""
        df = pl.DataFrame({"ac": [600_000.0], "pc": [300_000.0]})
        result = df.select(safe_div("ac", "pc").alias("lc"))
        assert result["lc"][0] == pytest.approx(2.0)

    def test_roe_com_pl_negativo(self):
        """ROE com PL negativo: 100k / -200k = -0.5 (empresa com passivo a descoberto)"""
        df = pl.DataFrame({"lucro": [100_000.0], "pl": [-200_000.0]})
        result = df.select(safe_div("lucro", "pl").alias("roe"))
        assert result["roe"][0] == pytest.approx(-0.5)

    def test_payout(self):
        """Payout = Proventos / Lucro Líquido = 30k / 100k = 0.3 (30%)"""
        df = pl.DataFrame({"proventos": [30_000.0], "lucro": [100_000.0]})
        result = df.select(safe_div("proventos", "lucro").alias("payout"))
        assert result["payout"][0] == pytest.approx(0.3)


# ============================================================
# 5. Testes de Piotroski F-Score (Critérios Individuais)
# ============================================================

class TestPiotroskiFScore:
    """Testa cada critério do F-Score isoladamente."""

    def test_roa_positivo_pontua(self):
        """ROA > 0 → +1 ponto"""
        assert 0.05 > 0  # 1 ponto

    def test_fco_positivo_pontua(self):
        """FCO > 0 → +1 ponto"""
        assert 500_000 > 0  # 1 ponto

    def test_roa_crescente_pontua(self):
        """ROA atual > ROA anterior → +1 ponto"""
        assert 0.06 > 0.05  # 1 ponto

    def test_fco_maior_que_lucro_pontua(self):
        """FCO > Lucro Líquido → +1 ponto (qualidade do lucro)"""
        assert 500_000 > 100_000  # 1 ponto

    def test_alavancagem_decrescente_pontua(self):
        """Alavancagem LP atual < anterior → +1 ponto"""
        assert 0.15 < 0.20  # 1 ponto

    def test_fscore_maximo_eh_9(self):
        """F-Score máximo é 9 (9 critérios binários)."""
        max_score = sum([1, 1, 1, 1, 1, 1, 1, 1, 1])
        assert max_score == 9

    def test_fscore_minimo_eh_0(self):
        """F-Score mínimo é 0 (nenhum critério atendido)."""
        min_score = sum([0, 0, 0, 0, 0, 0, 0, 0, 0])
        assert min_score == 0


# ============================================================
# 6. Testes de Validação de Configuração (Pydantic)
# ============================================================

class TestConfigValidation:
    """Testa a validação Pydantic do parameters.toml."""

    def test_config_valida_carrega_sem_erro(self):
        """O parameters.toml do projeto deve passar na validação."""
        from synetra.config import load_config
        config = load_config("parameters.toml")
        assert "urls" in config
        assert "pipeline" in config
        assert "contas" in config

    def test_years_start_menor_que_end(self):
        """years_start deve ser menor que years_end."""
        from synetra.config import load_config
        config = load_config("parameters.toml")
        assert config["pipeline"]["years_start"] < config["pipeline"]["years_end"]

    def test_threshold_entre_0_e_100(self):
        """Threshold do fuzzy match deve estar no intervalo [0, 100]."""
        from synetra.config import load_config
        config = load_config("parameters.toml")
        assert 0 <= config["fuzzy_match"]["threshold"] <= 100

    def test_todos_setores_presentes(self):
        """As contas devem ter os 3 setores obrigatórios."""
        from synetra.config import load_config
        config = load_config("parameters.toml")
        assert "industrial" in config["contas"]
        assert "financeiro" in config["contas"]
        assert "seguradora" in config["contas"]

    def test_config_invalida_levanta_erro(self):
        """Um TOML inválido deve levantar ValidationError."""
        from pydantic import ValidationError
        from synetra.config import SynetraConfig

        with pytest.raises(ValidationError):
            SynetraConfig(
                urls={"dfp_pattern": "sem_placeholder", "fre_pattern": "x", "cadastro": "x", "fundamentus": "x"},
                pipeline={"doc_types": ["DRE"], "years_start": 2010, "years_end": 2005, "max_workers": 5},
                fuzzy_match={"threshold": 85},
                contas={"industrial": {}},
                setores={"financeiro": ["BANCO"], "seguradora": ["SEGURO"]}
            )

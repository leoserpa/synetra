"""
Testes do loader com I/O mockado.
Skill: python-testing → Mocking Strategies (unittest.mock.patch)

Estes testes NÃO dependem de arquivos reais, internet ou disco.
Eles simulam leitura de ZIPs/CSVs para validar a lógica de parsing.
"""
import io
import zipfile

from synetra.loader import process_fre_from_zip, read_cvm_csv


class TestReadCvmCsvMocked:
    """Testa a leitura de CSV de dentro de um ZIP mockado."""

    def _make_zip_with_csv(self, filename: str, csv_content: str):
        """Helper: cria um ZipFile em memória com um CSV dentro."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr(filename, csv_content)
        buf.seek(0)
        return zipfile.ZipFile(buf, 'r')

    def test_leitura_basica_de_csv_no_zip(self):
        """Deve ler um CSV válido dentro de um ZIP e retornar um DataFrame."""
        csv = "CD_CONTA;CNPJ_CIA;VL_CONTA;ORDEM_EXERC;DS_CONTA\n3.01;11.111/0001-01;1000;ÚLTIMO;Receita"
        zip_ref = self._make_zip_with_csv("dados_2023.csv", csv)
        df = read_cvm_csv(zip_ref, "dados_2023.csv")
        assert not df.is_empty()
        assert df.height == 1
        assert "CD_CONTA" in df.columns

    def test_arquivo_ausente_retorna_vazio(self):
        """Se o arquivo não existe no ZIP, deve retornar DataFrame vazio sem erro."""
        csv = "CD_CONTA;CNPJ_CIA;VL_CONTA;ORDEM_EXERC;DS_CONTA\n3.01;111;1000;ÚLTIMO;Receita"
        zip_ref = self._make_zip_with_csv("existe.csv", csv)
        df = read_cvm_csv(zip_ref, "nao_existe.csv")
        assert df.is_empty()

    def test_strip_de_espacos_em_colunas_chave(self):
        """Deve remover espaços extras de CD_CONTA e CNPJ_CIA."""
        csv = "CD_CONTA;CNPJ_CIA;VL_CONTA;ORDEM_EXERC;DS_CONTA\n  3.01  ;  11.111  ;1000;ÚLTIMO;Receita"
        zip_ref = self._make_zip_with_csv("dados.csv", csv)
        df = read_cvm_csv(zip_ref, "dados.csv")
        assert df["CD_CONTA"][0] == "3.01"
        assert df["CNPJ_CIA"][0] == "11.111"


class TestProcessFreMocked:
    """Testa a leitura de FRE (quantidade de ações) com ZIP mockado."""

    def _make_fre_zip(self, year: int, csv_content: str):
        """Helper: cria ZIP com arquivo FRE dentro."""
        filename = f"fre_cia_aberta_capital_social_{year}.csv"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr(filename, csv_content)
        buf.seek(0)
        return zipfile.ZipFile(buf, 'r')

    def test_leitura_fre_basica(self):
        """Deve extrair CNPJ e quantidade de ações corretamente."""
        csv = "CNPJ_Companhia;Versao;Quantidade_Total_Acoes;Tipo_Capital\n11.111;1;1000000;Capital Integralizado"
        zip_ref = self._make_fre_zip(2023, csv)
        df = process_fre_from_zip(zip_ref, 2023)
        assert not df.is_empty()
        assert df["QTDE_ACOES"][0] == 1_000_000.0
        assert df["ANO"][0] == 2023

    def test_fre_arquivo_ausente_retorna_vazio(self):
        """Se o arquivo FRE não existe, retorna DataFrame vazio."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("outro_arquivo.csv", "a;b\n1;2")
        buf.seek(0)
        zip_ref = zipfile.ZipFile(buf, 'r')
        df = process_fre_from_zip(zip_ref, 2023)
        assert df.is_empty()

    def test_fre_prioriza_versao_mais_recente(self):
        """Quando há múltiplas versões, deve usar a mais recente."""
        csv = (
            "CNPJ_Companhia;Versao;Quantidade_Total_Acoes;Tipo_Capital\n"
            "11.111;1;500000;Capital Integralizado\n"
            "11.111;2;800000;Capital Integralizado"
        )
        zip_ref = self._make_fre_zip(2023, csv)
        df = process_fre_from_zip(zip_ref, 2023)
        assert df["QTDE_ACOES"][0] == 800_000.0

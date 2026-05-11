"""Dataclasses e enums do módulo de Data Quality."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """Severidade de um problema de qualidade.

    Ordenação natural (pra priorização visual):
        OK < LOW < MEDIUM < HIGH
    """

    OK = "OK"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FlagType(StrEnum):
    """Tipos de problema detectados em cada ticker.

    Escolhemos nomes curtos e em inglês pra manter o CSV amigável a ferramentas
    e facilitar filtros.
    """

    #: Ticker existe no CVM mas nunca teve preço no Yahoo.
    NO_YAHOO_HISTORY = "NO_YAHOO_HISTORY"

    #: Yahoo tem preço, mas o último é de ano(s) anterior(es).
    YAHOO_STALE = "YAHOO_STALE"

    #: Último dado CVM é muito antigo (empresa possivelmente delistada).
    LIKELY_DELISTED = "LIKELY_DELISTED"

    #: Ticker provavelmente incorreto (PN sem liquidez, classe errada, etc.).
    TICKER_MAY_BE_WRONG = "TICKER_MAY_BE_WRONG"

    #: Série histórica tem buracos temporais (anos faltando no meio).
    TEMPORAL_GAP = "TEMPORAL_GAP"

    #: Poucos anos de dados CVM (empresa recente).
    RECENT_LISTING = "RECENT_LISTING"


@dataclass(frozen=True)
class QualityIssue:
    """Um problema detectado em um ticker específico.

    Attributes:
        flag: Tipo do problema.
        severity: Severidade atribuída.
        detail: Texto livre com contexto (ex: "último preço em 2024").
    """

    flag: FlagType
    severity: Severity
    detail: str = ""


@dataclass
class TickerQuality:
    """Resumo de qualidade de um único ticker.

    Attributes:
        ticker: Código B3 (ex: ``"PETR4"``).
        categoria: Setor (INDUSTRIAL / FINANCEIRO / SEGURADORA).
        razao_cvm: Nome da empresa.
        tem_preco_atual: Se há ``PRECO_ATUAL`` no snapshot.
        ultimo_ano_cvm: Último ano com dados CVM.
        ultimo_ano_yahoo: Último ano com preço Yahoo (``None`` se nunca teve).
        anos_cvm_total: Quantidade de anos com dados CVM.
        anos_com_preco_yahoo: Quantidade de anos com preço Yahoo.
        issues: Lista de problemas detectados (pode estar vazia).
    """

    ticker: str
    categoria: str
    razao_cvm: str
    tem_preco_atual: bool
    ultimo_ano_cvm: int
    ultimo_ano_yahoo: int | None
    anos_cvm_total: int
    anos_com_preco_yahoo: int
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        """Severidade máxima entre as issues (ou OK se não houver)."""
        if not self.issues:
            return Severity.OK
        ordering = {Severity.OK: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3}
        return max(self.issues, key=lambda i: ordering[i.severity]).severity

    @property
    def flags(self) -> list[FlagType]:
        """Lista de flags sem duplicatas, preservando ordem."""
        seen: set[FlagType] = set()
        result: list[FlagType] = []
        for issue in self.issues:
            if issue.flag not in seen:
                seen.add(issue.flag)
                result.append(issue.flag)
        return result

    @property
    def gap_anos_preco(self) -> int | None:
        """Diferença entre último ano CVM e último ano Yahoo.

        Retorna ``None`` se Yahoo nunca teve preço.
        """
        if self.ultimo_ano_yahoo is None:
            return None
        return self.ultimo_ano_cvm - self.ultimo_ano_yahoo


@dataclass(frozen=True)
class QualityReport:
    """Relatório consolidado com todos os tickers e estatísticas agregadas.

    Attributes:
        tickers: Lista de ``TickerQuality`` (1 por ticker).
        total: Total de tickers auditados.
        ok_count: Tickers sem nenhuma issue.
        low_count: Tickers com severidade máxima LOW.
        medium_count: Tickers com severidade máxima MEDIUM.
        high_count: Tickers com severidade máxima HIGH.
    """

    tickers: list[TickerQuality]
    total: int
    ok_count: int
    low_count: int
    medium_count: int
    high_count: int

    @classmethod
    def from_tickers(cls, tickers: list[TickerQuality]) -> QualityReport:
        """Agrega contadores a partir da lista de tickers."""
        counts = {Severity.OK: 0, Severity.LOW: 0, Severity.MEDIUM: 0, Severity.HIGH: 0}
        for ticker in tickers:
            counts[ticker.severity] += 1
        return cls(
            tickers=tickers,
            total=len(tickers),
            ok_count=counts[Severity.OK],
            low_count=counts[Severity.LOW],
            medium_count=counts[Severity.MEDIUM],
            high_count=counts[Severity.HIGH],
        )

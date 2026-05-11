"""Checks individuais de qualidade — cada função detecta 1 tipo de problema.

Todos os checks recebem um ``TickerQuality`` já preenchido com os metadados
básicos (anos CVM/Yahoo) e retornam a lista de issues detectadas (pode ser
vazia). Isso segue o Single Responsibility Principle: cada check tem uma
regra clara, testável isoladamente.
"""
from __future__ import annotations

from typing import Final

from synetra.data_quality.models import (
    FlagType,
    QualityIssue,
    Severity,
    TickerQuality,
)

# --- Constantes de threshold (ajustáveis sem mexer em lógica) ---

#: Máxima defasagem aceitável em anos (Yahoo vs CVM) antes de flagar stale.
_MAX_YAHOO_STALE_YEARS: Final[int] = 1

#: Anos sem dado CVM recente para considerar provável delistagem.
_YEARS_WITHOUT_CVM_TO_SUSPECT_DELISTING: Final[int] = 2

#: Menos que isso = empresa recente (não é erro, mas vale sinalizar).
_MIN_YEARS_FOR_ESTABLISHED_TICKER: Final[int] = 3

#: Padrões de ticker por classe (B3):
#:  - Final 3: ordinária (ON)  - Final 4/5/6/7/8: preferencial (PN)
#:  - Final 11: unit
#: Se o ticker é PN/UNIT sem histórico, suspeitamos que a classe ordinária
#: dominante não foi capturada no mapa (caso CYRE4 vs CYRE3).
_PN_SUFFIXES: Final[frozenset[str]] = frozenset({"4", "5", "6", "7", "8"})


# --- Checks individuais ---


def check_no_yahoo_history(ticker: TickerQuality) -> list[QualityIssue]:
    """Ticker existe no CVM mas nunca teve preço Yahoo.

    Severidade: HIGH — não dá pra calcular múltiplos (P/L, P/VP, EV/EBITDA).
    """
    if ticker.anos_com_preco_yahoo > 0:
        return []
    return [
        QualityIssue(
            flag=FlagType.NO_YAHOO_HISTORY,
            severity=Severity.HIGH,
            detail=f"Yahoo nunca retornou preço para {ticker.ticker}",
        )
    ]


def check_yahoo_stale(
    ticker: TickerQuality, current_year: int
) -> list[QualityIssue]:
    """Yahoo tem preço mas não no ano mais recente de dados CVM.

    Severidade: MEDIUM — tem dados históricos, mas snapshot fica desatualizado.
    """
    if ticker.ultimo_ano_yahoo is None:
        return []  # tratado em check_no_yahoo_history

    gap = ticker.ultimo_ano_cvm - ticker.ultimo_ano_yahoo
    if gap <= _MAX_YAHOO_STALE_YEARS:
        return []

    return [
        QualityIssue(
            flag=FlagType.YAHOO_STALE,
            severity=Severity.MEDIUM,
            detail=(
                f"Último preço Yahoo em {ticker.ultimo_ano_yahoo}, "
                f"último dado CVM em {ticker.ultimo_ano_cvm} "
                f"(gap de {gap} anos)"
            ),
        )
    ]


def check_likely_delisted(
    ticker: TickerQuality, current_year: int
) -> list[QualityIssue]:
    """Último dado CVM é muito antigo → provável delistagem.

    Severidade: HIGH — indica que talvez o ticker deva sair do mapa.
    """
    years_behind = current_year - ticker.ultimo_ano_cvm
    if years_behind < _YEARS_WITHOUT_CVM_TO_SUSPECT_DELISTING:
        return []

    return [
        QualityIssue(
            flag=FlagType.LIKELY_DELISTED,
            severity=Severity.HIGH,
            detail=(
                f"Último dado CVM em {ticker.ultimo_ano_cvm}, "
                f"{years_behind} anos atrás (empresa possivelmente delistada)"
            ),
        )
    ]


def check_ticker_may_be_wrong(ticker: TickerQuality) -> list[QualityIssue]:
    """Ticker PN/Unit sem histórico Yahoo — possível classe errada no mapa.

    Severidade: MEDIUM — heurística, não conclusivo. Sinaliza candidatos
    para revisão manual (ex: CYRE4 quando a classe dominante é CYRE3).
    """
    if ticker.anos_com_preco_yahoo > 0:
        return []  # tem preço → não é caso de ticker errado

    last_char = ticker.ticker[-1] if ticker.ticker else ""
    last_two = ticker.ticker[-2:] if len(ticker.ticker) >= 2 else ""

    is_pn = last_char in _PN_SUFFIXES
    is_unit = last_two == "11"
    if not (is_pn or is_unit):
        return []

    classe = "PN" if is_pn else "UNIT"
    return [
        QualityIssue(
            flag=FlagType.TICKER_MAY_BE_WRONG,
            severity=Severity.MEDIUM,
            detail=(
                f"Ticker {classe} sem histórico Yahoo — "
                f"verificar se a classe ordinária (final 3) é a dominante"
            ),
        )
    ]


def check_temporal_gaps(
    ticker: TickerQuality, gap_years: list[int]
) -> list[QualityIssue]:
    """Série histórica tem buracos (anos faltando no meio).

    Severidade: LOW — incômodo mas não crítico. ``gap_years`` é a lista
    de anos ausentes dentro do range observado.
    """
    if not gap_years:
        return []

    return [
        QualityIssue(
            flag=FlagType.TEMPORAL_GAP,
            severity=Severity.LOW,
            detail=f"Anos faltando na série CVM: {gap_years}",
        )
    ]


def check_recent_listing(ticker: TickerQuality) -> list[QualityIssue]:
    """Empresa com pouco histórico CVM (IPO recente).

    Severidade: LOW — não é erro, apenas sinaliza que análises históricas
    ficam limitadas.
    """
    if ticker.anos_cvm_total >= _MIN_YEARS_FOR_ESTABLISHED_TICKER:
        return []

    return [
        QualityIssue(
            flag=FlagType.RECENT_LISTING,
            severity=Severity.LOW,
            detail=(
                f"Apenas {ticker.anos_cvm_total} anos de dados CVM "
                "(histórico limitado para análise)"
            ),
        )
    ]


# --- Orquestração: aplica TODOS os checks em um ticker ---


def run_all_checks(
    ticker: TickerQuality,
    current_year: int,
    gap_years: list[int],
) -> list[QualityIssue]:
    """Executa todos os checks e retorna a lista consolidada de issues.

    Args:
        ticker: Resumo de qualidade já preenchido.
        current_year: Ano de referência (geralmente o ano atual).
        gap_years: Anos ausentes na série CVM do ticker.

    Returns:
        Lista de issues (pode ser vazia = ticker OK).
    """
    issues: list[QualityIssue] = []
    issues.extend(check_no_yahoo_history(ticker))
    issues.extend(check_yahoo_stale(ticker, current_year))
    issues.extend(check_likely_delisted(ticker, current_year))
    issues.extend(check_ticker_may_be_wrong(ticker))
    issues.extend(check_temporal_gaps(ticker, gap_years))
    issues.extend(check_recent_listing(ticker))
    return issues

"""Testes unitários do synetra.downloader com httpx.MockTransport.

Estratégia: usar `httpx.MockTransport` para simular respostas do servidor
CVM sem fazer rede real. Isso permite testar:

    - Verificação de freshness (Last-Modified vs mtime local)
    - Fail-open em timeout/erros de rede
    - Construção correta de ZipFile a partir de response
    - Paralelismo via asyncio.gather

Princípios F.I.R.S.T.: sem I/O de rede, fixtures pequenas, cache em tmp_path.
"""
from __future__ import annotations

import io
import pathlib
import time
import zipfile
from email.utils import formatdate

import httpx
import pytest

from synetra.downloader import (
    _DEFAULT_MAX_WORKERS,
    _DFP_SUBDIR,
    _DOWNLOAD_RETRIES,
    _DOWNLOAD_TIMEOUT_SECONDS,
    _FRE_SUBDIR,
    _HEAD_RETRIES,
    _HEAD_TIMEOUT_SECONDS,
    _HTTP_OK,
    CVMDownloader,
    _build_zip_from_response,
    _server_is_newer_than_cache,
)

# --- Constantes do módulo ---


class TestModuleConstants:
    """Valores semânticos das constantes HTTP e filesystem."""

    def test_head_timeout_is_shorter_than_download(self) -> None:
        assert _HEAD_TIMEOUT_SECONDS < _DOWNLOAD_TIMEOUT_SECONDS

    def test_download_retries_allow_recovery(self) -> None:
        assert _DOWNLOAD_RETRIES >= _HEAD_RETRIES

    def test_default_max_workers_is_polite(self) -> None:
        """Deve estar em um intervalo razoável para não abusar do servidor CVM."""
        assert 1 <= _DEFAULT_MAX_WORKERS <= 20

    def test_http_ok_constant(self) -> None:
        assert _HTTP_OK == 200

    def test_subdirs_are_distinct(self) -> None:
        assert _DFP_SUBDIR != _FRE_SUBDIR
        assert _DFP_SUBDIR and _FRE_SUBDIR


# --- CVMDownloader — construção e paths ---


class TestDownloaderSetup:
    """Criação de diretórios e geração de caminhos de cache."""

    def test_creates_cache_directories(self, tmp_path: pathlib.Path) -> None:
        CVMDownloader(cache_dir=str(tmp_path / "cache"))
        assert (tmp_path / "cache").exists()
        assert (tmp_path / "cache" / _DFP_SUBDIR).exists()
        assert (tmp_path / "cache" / _FRE_SUBDIR).exists()

    def test_get_cache_path_dfp(self, tmp_path: pathlib.Path) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        path = downloader.get_cache_path(2024, "dfp")
        assert path == tmp_path / _DFP_SUBDIR / "2024.parquet"

    def test_get_cache_path_fre(self, tmp_path: pathlib.Path) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        path = downloader.get_cache_path(2023, "fre")
        assert path == tmp_path / _FRE_SUBDIR / "2023.parquet"

    def test_get_cache_path_default_is_dfp(self, tmp_path: pathlib.Path) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        assert downloader.get_cache_path(2024) == downloader.get_cache_path(2024, "dfp")


# --- _server_is_newer_than_cache ---


def _make_response(
    status: int = 200,
    last_modified: str | None = None,
    content: bytes = b"",
) -> httpx.Response:
    """Helper para criar um httpx.Response com headers controlados."""
    headers = {}
    if last_modified is not None:
        headers["Last-Modified"] = last_modified
    return httpx.Response(
        status_code=status,
        headers=headers,
        content=content,
        request=httpx.Request("HEAD", "https://example.com/x.zip"),
    )


class TestServerIsNewerThanCache:
    """Compara timestamp do servidor com mtime do cache local."""

    def test_returns_false_when_server_is_older(
        self, tmp_path: pathlib.Path
    ) -> None:
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")
        # Servidor: 1 hora atrás; Local: agora → local é mais recente
        old_ts = formatdate(time.time() - 3600, usegmt=True)
        response = _make_response(status=200, last_modified=old_ts)
        assert _server_is_newer_than_cache(response, cache_file) is False

    def test_returns_true_when_server_is_newer(
        self, tmp_path: pathlib.Path
    ) -> None:
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")
        # Envelhecer cache em 2 horas para garantir que servidor seja mais novo
        old_mtime = time.time() - 7200
        import os

        os.utime(cache_file, (old_mtime, old_mtime))
        future_ts = formatdate(time.time(), usegmt=True)
        response = _make_response(status=200, last_modified=future_ts)
        assert _server_is_newer_than_cache(response, cache_file) is True

    def test_returns_false_on_non_200_status(
        self, tmp_path: pathlib.Path
    ) -> None:
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")
        response = _make_response(status=500)
        assert _server_is_newer_than_cache(response, cache_file) is False

    def test_returns_false_when_no_last_modified(
        self, tmp_path: pathlib.Path
    ) -> None:
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")
        response = _make_response(status=200, last_modified=None)
        assert _server_is_newer_than_cache(response, cache_file) is False


# --- _build_zip_from_response ---


class TestBuildZipFromResponse:
    """Constrói ZipFile em memória a partir do body HTTP."""

    def test_builds_readable_zip(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("arquivo.txt", b"conteudo de teste")
        zip_bytes = buffer.getvalue()

        response = _make_response(status=200, content=zip_bytes)
        result = _build_zip_from_response(response)

        assert isinstance(result, zipfile.ZipFile)
        assert "arquivo.txt" in result.namelist()
        assert result.read("arquivo.txt") == b"conteudo de teste"


# --- Integração async com httpx.MockTransport ---


def _zip_bytes_with(filename: str, content: bytes) -> bytes:
    """Helper: gera um ZIP em memória com um único arquivo."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(filename, content)
    return buffer.getvalue()


class TestIsCacheValid:
    """Testes da verificação de freshness via HEAD request mockada."""

    @pytest.mark.asyncio
    async def test_returns_false_when_cache_missing(
        self, tmp_path: pathlib.Path
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        cache_file = tmp_path / "nonexistent.parquet"

        transport = httpx.MockTransport(lambda req: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await downloader._is_cache_valid(
                client, "https://x.com/y.zip", cache_file
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_timeout(self, tmp_path: pathlib.Path) -> None:
        """Fail-open: timeout não deve invalidar o cache."""
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        transport = httpx.MockTransport(timeout_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await downloader._is_cache_valid(
                client, "https://x.com/y.zip", cache_file
            )
        assert result is True  # fail-open

    @pytest.mark.asyncio
    async def test_returns_true_on_connection_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")

        def conn_error(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("servidor fora do ar")

        transport = httpx.MockTransport(conn_error)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await downloader._is_cache_valid(
                client, "https://x.com/y.zip", cache_file
            )
        assert result is True  # fail-open

    @pytest.mark.asyncio
    async def test_returns_false_when_server_has_newer(
        self, tmp_path: pathlib.Path
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        cache_file = tmp_path / "c.parquet"
        cache_file.write_text("x")

        # Envelhecer cache
        import os

        old_mtime = time.time() - 7200
        os.utime(cache_file, (old_mtime, old_mtime))

        future_ts = formatdate(time.time(), usegmt=True)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Last-Modified": future_ts})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await downloader._is_cache_valid(
                client, "https://x.com/y.zip", cache_file
            )
        assert result is False


class TestGetMissingYears:
    """Verificação paralela via HEAD."""

    @pytest.mark.asyncio
    async def test_returns_years_without_cache(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))

        # Substitui httpx.AsyncClient para usar MockTransport
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        original_client_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            original_client_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        missing = await downloader.get_missing_years(
            years=[2022, 2023, 2024],
            url_pattern="https://cvm.example.com/{year}.zip",
        )
        # Nenhum cache existe → todos faltam
        assert set(missing) == {2022, 2023, 2024}

    @pytest.mark.asyncio
    async def test_excludes_years_with_fresh_cache(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))

        # Cria cache "fresco" (mtime recente) para 2023
        cache_2023 = downloader.get_cache_path(2023, "dfp")
        cache_2023.write_text("x")

        # Servidor responde com Last-Modified antigo para 2023 → cache vence
        old_ts = formatdate(time.time() - 7200, usegmt=True)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Last-Modified": old_ts})

        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        missing = await downloader.get_missing_years(
            years=[2023, 2024],
            url_pattern="https://cvm.example.com/{year}.zip",
        )
        assert 2023 not in missing
        assert 2024 in missing


class TestDownloadYearsParallel:
    """Download paralelo de múltiplos anos."""

    @pytest.mark.asyncio
    async def test_successful_download_returns_zipfile(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        zip_content = _zip_bytes_with("dados.txt", b"conteudo CVM")

        async def fake_fetch(client, url):
            return httpx.Response(
                200,
                content=zip_content,
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(downloader, "_fetch_zip_response", fake_fetch)

        results = await downloader.download_years_parallel(
            years=[2024],
            url_pattern="https://cvm.example.com/{year}.zip",
        )
        assert len(results) == 1
        zip_ref, year = results[0]
        assert year == 2024
        assert zip_ref is not None
        assert "dados.txt" in zip_ref.namelist()

    @pytest.mark.asyncio
    async def test_http_404_returns_none(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))

        async def fake_fetch(client, url):
            return None  # simula falha

        monkeypatch.setattr(downloader, "_fetch_zip_response", fake_fetch)

        results = await downloader.download_years_parallel(
            years=[9999],
            url_pattern="https://cvm.example.com/{year}.zip",
        )
        assert results == [(None, 9999)]

    @pytest.mark.asyncio
    async def test_timeout_returns_none(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        downloader = CVMDownloader(cache_dir=str(tmp_path))

        async def fake_fetch(client, url):
            return None  # simula timeout

        monkeypatch.setattr(downloader, "_fetch_zip_response", fake_fetch)

        results = await downloader.download_years_parallel(
            years=[2024],
            url_pattern="https://cvm.example.com/{year}.zip",
        )
        assert results == [(None, 2024)]

    @pytest.mark.asyncio
    async def test_parallel_downloads_all_complete(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """3 anos em paralelo devem retornar 3 resultados."""
        downloader = CVMDownloader(cache_dir=str(tmp_path))
        zip_content = _zip_bytes_with("a.txt", b"x")

        call_count = {"n": 0}

        async def fake_fetch(client, url):
            call_count["n"] += 1
            return httpx.Response(
                200,
                content=zip_content,
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(downloader, "_fetch_zip_response", fake_fetch)

        results = await downloader.download_years_parallel(
            years=[2022, 2023, 2024],
            url_pattern="https://cvm.example.com/{year}.zip",
            max_workers=2,
        )
        assert len(results) == 3
        assert call_count["n"] == 3
        assert all(zip_ref is not None for zip_ref, _ in results)

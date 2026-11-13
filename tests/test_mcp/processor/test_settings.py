"""Configuration: one name per knob, a server that runs unconfigured, and a token kept quiet."""

import pytest

from crossfoot.db.connect import readonly_database_url
from crossfoot.mcp.processor.settings import (
    DEFAULT_TOKEN,
    DEFAULT_URL,
    DSN_ENV,
    ERP_SCHEMA_ENV,
    MAX_LIMIT_ENV,
    TOKEN_ENV,
    URL_ENV,
    ProcessorSettings,
)
from crossfoot.mock_processor import settings as mock_settings

SECRET = "s3cr3t-live-token"
ENVIRONMENT = (URL_ENV, TOKEN_ENV, DSN_ENV, ERP_SCHEMA_ENV, MAX_LIMIT_ENV)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_with_no_configuration_the_server_points_at_just_proc_api(
    clean_env: pytest.MonkeyPatch,
) -> None:
    settings = ProcessorSettings.from_env()
    assert settings.base_url == DEFAULT_URL == "http://127.0.0.1:8099"
    assert settings.dsn == readonly_database_url()
    assert (settings.erp_schema, settings.default_limit, settings.max_limit) == ("erp", 50, 200)
    # Restated, not imported -- so pinned: the two defaults have to be the same token.
    assert settings.token == DEFAULT_TOKEN == mock_settings.DEFAULT_TOKEN
    # And it is the same variable the API reads, so one secret has one name.
    clean_env.setenv(TOKEN_ENV, SECRET)
    assert mock_settings.ApiSettings.from_env().token == ProcessorSettings.from_env().token


def test_the_environment_is_read(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(URL_ENV, "https://processor.example/api/")
    clean_env.setenv(TOKEN_ENV, SECRET)
    clean_env.setenv(DSN_ENV, "postgresql://ro@db/x")
    clean_env.setenv(ERP_SCHEMA_ENV, "erp_test")
    clean_env.setenv(MAX_LIMIT_ENV, "75")
    settings = ProcessorSettings.from_env()
    # A trailing slash is dropped, so a path can always be appended.
    assert settings.base_url == "https://processor.example/api"
    assert (settings.token, settings.dsn) == (SECRET, "postgresql://ro@db/x")
    assert (settings.erp_schema, settings.max_limit) == ("erp_test", 75)
    # An explicit argument -- the command line -- wins over the environment.
    assert (
        ProcessorSettings.from_env(base_url="http://localhost:1").base_url == "http://localhost:1"
    )


def test_the_token_and_the_dsn_stay_out_of_the_repr() -> None:
    settings = ProcessorSettings(token=SECRET, dsn="postgresql://ro:hunter2@db/x")
    assert SECRET not in repr(settings)
    assert "hunter2" not in repr(settings)
    assert SECRET not in str(settings)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "127.0.0.1:8099",
        "file:///etc/passwd",
        "ftp://processor.example",
        "http://",
        "http://user:pw@processor.example",
        "http://processor.example/v1?limit=1",
        "http://processor.example/#top",
    ],
)
def test_a_url_that_is_not_a_plain_http_base_is_refused_at_startup(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        ProcessorSettings(base_url=url)


@pytest.mark.parametrize("token", ["", " ", "two words", "line\r\nX-Injected: 1", "tab\there", "é"])
def test_a_token_that_cannot_be_a_bearer_token_is_refused_without_quoting_it(token: str) -> None:
    with pytest.raises(ValueError) as caught:
        ProcessorSettings(token=token)
    assert TOKEN_ENV in str(caught.value)
    if token.strip():
        assert token not in str(caught.value)


def test_the_other_knobs_are_checked_too(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="not a usable schema name"):
        ProcessorSettings(erp_schema="erp; DROP SCHEMA public")
    with pytest.raises(ValueError, match="limits"):
        ProcessorSettings(default_limit=10, max_limit=5)
    with pytest.raises(ValueError, match="timeout"):
        ProcessorSettings(timeout=0)
    with pytest.raises(ValueError, match="negative"):
        ProcessorSettings(max_retries=-1)
    clean_env.setenv(MAX_LIMIT_ENV, "lots")
    with pytest.raises(ValueError, match=MAX_LIMIT_ENV):
        ProcessorSettings.from_env()

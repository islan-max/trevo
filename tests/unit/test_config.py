from __future__ import annotations

from app.core.config import Settings


def test_trusted_hosts_defaults_to_empty_without_restriction(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    assert Settings().trusted_hosts == []


def test_trusted_hosts_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "trevo-finance.vercel.app, outro.exemplo.com")
    assert Settings().trusted_hosts == ["trevo-finance.vercel.app", "outro.exemplo.com"]


def test_trusted_hosts_ignores_blank_entries(monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "trevo-finance.vercel.app,, ")
    assert Settings().trusted_hosts == ["trevo-finance.vercel.app"]

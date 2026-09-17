from __future__ import annotations

import httpx
import pytest

from app.core import storage


@pytest.fixture(autouse=True)
def supabase_configured(monkeypatch):
    # DEP-07: força o backend Supabase (em vez do fallback de disco local)
    # para exercitar as chamadas REST recém-escritas em app/core/storage.py.
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    monkeypatch.setenv("SUPABASE_AVATARS_BUCKET", "avatars")


def _mock_client(handler) -> httpx.Client:
    # Carries the same apiKey/Authorization headers _storage_client() would
    # set for real, since this replaces that function wholesale in tests.
    return httpx.Client(
        base_url="https://example.supabase.co/storage/v1",
        headers={"apiKey": "service-role-key", "Authorization": "Bearer service-role-key"},
        transport=httpx.MockTransport(handler),
    )


def test_store_avatar_uploads_to_object_endpoint_with_auth_headers(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["apiKey"] = request.headers.get("apikey")
        captured["authorization"] = request.headers.get("authorization")
        captured["upsert"] = request.headers.get("x-upsert")
        return httpx.Response(200, json={"Key": "avatars/whatever"})

    monkeypatch.setattr(storage, "_storage_client", lambda: _mock_client(handler))

    ref = storage.store_avatar("user-1", b"fake-image-bytes", "png", "image/png")

    assert ref.startswith("supabase://user-1/")
    assert ref.endswith(".png")
    assert captured["method"] == "POST"
    assert "/object/avatars/user-1/" in captured["url"]
    assert captured["apiKey"] == "service-role-key"
    assert captured["authorization"] == "Bearer service-role-key"
    assert captured["upsert"] == "true"


def test_resolve_avatar_url_signs_supabase_ref(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/object/sign/avatars/user-1/photo.png" in str(request.url)
        assert request.headers.get("authorization") == "Bearer service-role-key"
        return httpx.Response(
            200,
            json={"signedURL": "/object/sign/avatars/user-1/photo.png?token=abc"},
        )

    monkeypatch.setattr(storage, "_storage_client", lambda: _mock_client(handler))

    url = storage.resolve_avatar_url("supabase://user-1/photo.png")

    assert url == "https://example.supabase.co/storage/v1/object/sign/avatars/user-1/photo.png?token=abc"


def test_resolve_avatar_url_returns_none_on_signing_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    monkeypatch.setattr(storage, "_storage_client", lambda: _mock_client(handler))

    assert storage.resolve_avatar_url("supabase://user-1/missing.png") is None


def test_resolve_avatar_url_passes_through_non_supabase_refs():
    assert storage.resolve_avatar_url(None) is None
    assert storage.resolve_avatar_url("https://cdn.example.com/x.png") == "https://cdn.example.com/x.png"
    assert storage.resolve_avatar_url("/media/profile-photos/a.png") == "/media/profile-photos/a.png"


def test_remove_avatar_deletes_supabase_ref(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json=[{"name": "user-1/photo.png"}])

    monkeypatch.setattr(storage, "_storage_client", lambda: _mock_client(handler))

    assert storage.remove_avatar("supabase://user-1/photo.png") is True
    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://example.supabase.co/storage/v1/object/avatars"
    assert b"user-1/photo.png" in captured["body"]


def test_remove_avatar_returns_false_on_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    monkeypatch.setattr(storage, "_storage_client", lambda: _mock_client(handler))

    assert storage.remove_avatar("supabase://user-1/photo.png") is False


def test_remove_avatar_no_op_for_missing_or_external_ref():
    assert storage.remove_avatar(None) is True
    assert storage.remove_avatar("https://cdn.example.com/x.png") is True

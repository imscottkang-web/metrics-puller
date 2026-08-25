"""Tests for metrics_lib.auth - the read-only OAuth token orchestration.

The four google-touching steps (load cached token, refresh, one-time consent,
save) are injected as fakes, so these tests never import a Google library or
open a socket. They exercise the DECISION logic: use a valid cached token,
refresh an expired one, run first-time consent, and turn a dead refresh token
into a plain-English ReauthRequired instead of a silent re-consent.

The _default_* seams (the ones that actually touch google-auth) are also
unit-tested below, without ever importing a real Google library: each test
pre-registers a small fake module object under the EXACT dotted name the seam
lazily imports (e.g. "google.oauth2.credentials"), via sys.modules. Python's
import system finds that name already cached and uses it directly, so the
real google/google-auth-oauthlib packages need not be installed. This mirrors
"fake the seam, never the network/library" used everywhere else in this
suite; see _stub_module below.
"""

import json
import sys
import types

import pytest

from metrics_lib.auth import (
    REAUTH_MESSAGE,
    CredentialFileError,
    OAuthTokenProvider,
    ReauthRequired,
)
from metrics_lib.http import MetricsApiError


class FakeCreds:
    def __init__(self, token="tok", valid=True, expired=False, refresh_token=None):
        self.token = token
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token


def test_valid_cached_token_used_without_refresh_or_consent(cfg):
    calls = []
    p = OAuthTokenProvider(
        cfg,
        loader=lambda: FakeCreds(token="cached", valid=True),
        refresher=lambda c: calls.append("refresh"),
        consenter=lambda: calls.append("consent") or FakeCreds(),
        saver=lambda c: calls.append("save"),
    )
    assert p.bearer() == "cached"
    assert calls == []  # a valid cached token is used as-is


def test_first_run_no_token_runs_consent_and_saves(cfg):
    saved = []
    p = OAuthTokenProvider(
        cfg,
        loader=lambda: None,
        consenter=lambda: FakeCreds(token="fresh", valid=True),
        saver=lambda c: saved.append(c.token),
    )
    assert p.bearer() == "fresh"
    assert saved == ["fresh"]  # one-time consent result is cached


def test_expired_but_refreshable_refreshes_and_saves(cfg):
    saved = []

    def refresher(c):
        c.token = "refreshed"
        c.valid = True

    p = OAuthTokenProvider(
        cfg,
        loader=lambda: FakeCreds(token="old", valid=False, expired=True, refresh_token="rt"),
        refresher=refresher,
        consenter=lambda: pytest.fail("must not consent when a refresh works"),
        saver=lambda c: saved.append(c.token),
    )
    assert p.bearer() == "refreshed"
    assert saved == ["refreshed"]


def test_refresh_failure_raises_reauth_and_never_silently_reconsents(cfg):
    calls = []

    def refresher(c):
        raise ReauthRequired(REAUTH_MESSAGE)

    p = OAuthTokenProvider(
        cfg,
        loader=lambda: FakeCreds(valid=False, expired=True, refresh_token="rt"),
        refresher=refresher,
        consenter=lambda: calls.append("consent") or FakeCreds(),
        saver=lambda c: calls.append("save"),
    )
    with pytest.raises(ReauthRequired):
        p.bearer()
    assert calls == []  # no silent browser re-consent; nothing saved


def test_unusable_creds_without_refresh_token_raises_reauth(cfg):
    p = OAuthTokenProvider(
        cfg,
        loader=lambda: FakeCreds(valid=False, expired=True, refresh_token=None),
    )
    with pytest.raises(ReauthRequired):
        p.bearer()


def test_reauth_message_is_plain_english_and_current(cfg):
    # The OAuth consent screen was published ("In production") on 2026-07-12,
    # so the old "click Publish app to stop the 7-day Testing expiry" advice
    # is stale and would misdirect during a real incident - it must be gone.
    assert "Publish app" not in REAUTH_MESSAGE
    assert "7 days" not in REAUTH_MESSAGE
    # Current diagnosis: with a published app, a dead sign-in usually means a
    # revoked grant or a Google account security event.
    assert "no longer valid" in REAUTH_MESSAGE
    assert "revoked" in REAUTH_MESSAGE
    assert "password reset" in REAUTH_MESSAGE
    # The local fix.
    assert "delete secrets/token.json" in REAUTH_MESSAGE
    assert "sign in again" in REAUTH_MESSAGE
    # The GitHub Actions half of the fix, by exact repository-secret name.
    assert "YT_OAUTH_TOKEN_JSON" in REAUTH_MESSAGE
    # Pointers into the runbook, by its exact section titles (verified to
    # exist in shared-brain/runbooks/metrics-api-setup.md).
    assert "metrics-api-setup.md" in REAUTH_MESSAGE
    assert "When re-auth can happen" in REAUTH_MESSAGE
    assert "The GitHub Actions side" in REAUTH_MESSAGE
    # Leak-safety: a fixed plain-English message - no token, no URL, no file
    # contents (paths only).
    assert "Bearer" not in REAUTH_MESSAGE
    assert "http" not in REAUTH_MESSAGE.lower()


def test_token_never_leaks_in_repr(cfg):
    p = OAuthTokenProvider(cfg, loader=lambda: FakeCreds(token="SECRET-TOKEN", valid=True))
    p.bearer()
    assert "SECRET-TOKEN" not in repr(p)


def test_auth_module_imports_without_google_installed():
    # Proven by the fact this whole suite runs with no google packages present:
    # the google imports must live inside the default seams, not at module top.
    import metrics_lib.auth as auth_mod

    assert hasattr(auth_mod, "OAuthTokenProvider")
    assert hasattr(auth_mod, "ReauthRequired")
    assert hasattr(auth_mod, "CredentialFileError")


# --- _default_* seam tests -------------------------------------------------
#
# These exercise the real _default_loader/_default_refresher/_default_consenter
# methods (not injected fakes), to prove their exception-to-plain-English
# mapping. Each lazily does `from <dotted.module> import <Name>`; stubbing
# sys.modules[<dotted.module>] with a throwaway module object satisfies that
# import without needing google-auth/google-auth-oauthlib installed or real.


def _stub_module(monkeypatch, dotted_name: str, **attrs) -> types.ModuleType:
    """Register a throwaway module under sys.modules[dotted_name] with attrs.

    Only the exact leaf name being imported needs registering (Python's import
    system checks sys.modules for the full dotted name before it ever tries to
    resolve parent packages), so this works whether or not "google" is
    installed at all.
    """
    mod = types.ModuleType(dotted_name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, dotted_name, mod)
    return mod


class FakeRefreshError(Exception):
    """Stands in for google.auth.exceptions.RefreshError."""


class FakeTransportError(Exception):
    """Stands in for google.auth.exceptions.TransportError (a SIBLING of
    RefreshError in real google-auth - neither subclasses the other)."""


class FakeGoogleRequest:
    """Stands in for google.auth.transport.requests.Request (unused by the
    fakes here beyond being constructible)."""


def _stub_auth_exceptions(monkeypatch):
    _stub_module(
        monkeypatch,
        "google.auth.exceptions",
        RefreshError=FakeRefreshError,
        TransportError=FakeTransportError,
    )
    _stub_module(monkeypatch, "google.auth.transport.requests", Request=FakeGoogleRequest)


def test_default_loader_wraps_missing_field_value_error_as_credential_file_error(monkeypatch, cfg):
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text('{"contains": "SECRET-TOKEN-CONTENTS"}', encoding="utf-8")

    class FakeCredentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            raise ValueError(
                "Authorized user info was not in the expected format, missing fields refresh_token."
            )

    _stub_module(monkeypatch, "google.oauth2.credentials", Credentials=FakeCredentials)

    p = OAuthTokenProvider(cfg)
    with pytest.raises(CredentialFileError) as ei:
        p._default_loader()
    msg = str(ei.value)
    assert str(cfg.token_path) in msg
    assert "malformed" in msg or "incomplete" in msg
    assert "delete secrets/token.json" in msg
    assert "sign in again" in msg
    assert "YT_OAUTH_TOKEN_JSON" in msg
    assert "GitHub Actions" in msg
    # Leak-safety: the fake token file's contents never appear in the message.
    assert "SECRET-TOKEN-CONTENTS" not in msg


def test_default_loader_wraps_json_decode_error_as_credential_file_error(monkeypatch, cfg):
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text("{not valid json, SECRET-TOKEN-CONTENTS", encoding="utf-8")

    class FakeCredentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            raise json.JSONDecodeError("Expecting value", "SECRET-TOKEN-CONTENTS", 0)

    _stub_module(monkeypatch, "google.oauth2.credentials", Credentials=FakeCredentials)

    p = OAuthTokenProvider(cfg)
    with pytest.raises(CredentialFileError) as ei:
        p._default_loader()
    msg = str(ei.value)
    assert str(cfg.token_path) in msg
    assert "malformed" in msg or "incomplete" in msg
    assert "delete secrets/token.json" in msg
    assert "YT_OAUTH_TOKEN_JSON" in msg
    # Leak-safety: neither the file contents nor the raw parser doc leak.
    assert "SECRET-TOKEN-CONTENTS" not in msg


def test_default_loader_passes_through_a_good_token(monkeypatch, cfg):
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text('{"token": "whatever"}', encoding="utf-8")

    sentinel = object()

    class FakeCredentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            return sentinel

    _stub_module(monkeypatch, "google.oauth2.credentials", Credentials=FakeCredentials)

    p = OAuthTokenProvider(cfg)
    assert p._default_loader() is sentinel


def test_default_refresher_transport_error_becomes_retryable_metrics_error(monkeypatch, cfg):
    _stub_auth_exceptions(monkeypatch)

    class FailingCreds:
        token = "SECRET-TOKEN"

        def refresh(self, request):
            raise FakeTransportError("temporary DNS failure")

    p = OAuthTokenProvider(cfg)
    with pytest.raises(MetricsApiError) as ei:
        p._default_refresher(FailingCreds())
    assert ei.value.status == 0
    msg = str(ei.value)
    assert "network error while refreshing the sign-in token" in msg
    assert "safe to retry later" in msg
    # Must NOT be mis-diagnosed as a dead sign-in (wrong advice for a blip):
    # no re-auth instructions in a network-blip message.
    assert "delete secrets/token.json" not in msg
    # Leak-safety.
    assert "SECRET-TOKEN" not in msg


def test_default_refresher_refresh_error_still_raises_reauth_required(monkeypatch, cfg):
    _stub_auth_exceptions(monkeypatch)

    class FailingCreds:
        token = "SECRET-TOKEN"

        def refresh(self, request):
            raise FakeRefreshError("refresh token revoked")

    p = OAuthTokenProvider(cfg)
    with pytest.raises(ReauthRequired) as ei:
        p._default_refresher(FailingCreds())
    assert str(ei.value) == REAUTH_MESSAGE
    assert "SECRET-TOKEN" not in str(ei.value)


def test_default_refresher_success_refreshes_in_place(monkeypatch, cfg):
    _stub_auth_exceptions(monkeypatch)

    class OkCreds:
        def __init__(self):
            self.refreshed = False

        def refresh(self, request):
            self.refreshed = True

    p = OAuthTokenProvider(cfg)
    creds = OkCreds()
    p._default_refresher(creds)
    assert creds.refreshed is True


def test_default_consenter_missing_client_secret_raises_credential_file_error(monkeypatch, cfg):
    class FakeInstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            raise FileNotFoundError(2, "No such file or directory", path)

    _stub_module(monkeypatch, "google_auth_oauthlib.flow", InstalledAppFlow=FakeInstalledAppFlow)

    p = OAuthTokenProvider(cfg)
    with pytest.raises(CredentialFileError) as ei:
        p._default_consenter()
    msg = str(ei.value)
    assert str(cfg.client_secret_path) in msg
    assert "metrics-api-setup.md" in msg
    # The message is a clean, composed explanation, not a re-wrapped OS error.
    assert "No such file or directory" not in msg


def test_default_consenter_runs_flow_when_client_secret_present(monkeypatch, cfg):
    cfg.client_secret_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.client_secret_path.write_text('{"installed": {}}', encoding="utf-8")

    ran = {}

    class FakeFlow:
        def run_local_server(self, port=0):
            ran["port"] = port
            return "creds-from-flow"

    class FakeInstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            ran["path"] = path
            return FakeFlow()

    _stub_module(monkeypatch, "google_auth_oauthlib.flow", InstalledAppFlow=FakeInstalledAppFlow)

    p = OAuthTokenProvider(cfg)
    assert p._default_consenter() == "creds-from-flow"
    assert ran["port"] == 0

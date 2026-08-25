"""Read-only OAuth token acquisition for the metrics puller.

`bearer()` returns a valid read-only access token, doing the least work needed:
use a valid cached token, else refresh an expired one, else (first run) run the
one-time browser consent. A dead or revoked refresh token (with the app
published, that means a revoked grant or an account security event, not a
scheduled expiry) becomes a plain-English `ReauthRequired`, never a silent
re-consent. A token
file or client-secret file that is missing, malformed, or unreadable becomes a
plain-English `CredentialFileError`, never a raw ValueError/JSONDecodeError/
FileNotFoundError traceback. A network hiccup during refresh becomes a
`MetricsApiError` marked safe to retry, never mis-diagnosed as a dead sign-in.

The orchestration is unit-tested with injected fakes, so the tests never import
the Google libraries or open a socket. The default loader/refresher/consenter/
saver wrap google-auth / google-auth-oauthlib with LAZY imports (inside the
methods), so importing this module needs no Google packages. Their own
exception-to-plain-English mapping IS unit-tested (without Google installed) by
pre-registering fake modules under the exact dotted names they import, in
sys.modules; the real Google libraries are exercised only at the real sign-in
(Task 8).
"""

from __future__ import annotations

from typing import Callable, Optional
import json
import os

from metrics_lib.http import MetricsApiError

# The OAuth consent screen was published ("In production") on 2026-07-12, so
# the old "Testing mode expires every 7 days" gotcha no longer applies; a dead
# sign-in now means the grant itself was invalidated. Keep this message
# aligned with the two runbook sections it names at the end.
REAUTH_MESSAGE = (
    "Your read-only YouTube sign-in is no longer valid and could not be "
    "refreshed. The app is published ('In production'), so this usually means "
    "the access was revoked or a Google account security event (for example a "
    "password reset that revokes active sessions) invalidated the saved "
    "sign-in. Fix: delete secrets/token.json and run this tool locally to "
    "sign in again. If this broke the GitHub Actions run, ALSO re-paste the "
    "full contents of the fresh local token.json into the YT_OAUTH_TOKEN_JSON "
    "repository secret - otherwise the cloud pull keeps using the dead token "
    "and fails every day. Details: shared-brain/runbooks/metrics-api-setup.md, "
    "sections 'When re-auth can happen' and 'The GitHub Actions side'."
)


class ReauthRequired(Exception):
    """The saved sign-in is gone or expired and cannot be refreshed silently.

    The message is the plain-English fix only; it never contains the token or
    the client-secret contents.
    """


class CredentialFileError(Exception):
    """A credential file (token.json or client_secret.json) is missing,
    malformed, or incomplete, and cannot be used as-is.

    str(exc) is a complete plain-English explanation of the problem and the
    exact fix (local, and - where relevant - for a GitHub Actions secret). It
    names only the file's PATH, never its contents (leak-safety).
    """


class OAuthTokenProvider:
    """Turns the local client-secret + cached token into a bearer token.

    The four Google-touching steps are injectable so the decision logic is
    testable without Google or a network:
      loader()      -> credentials object or None (None = no cached token)
      refresher(c)  -> refresh c in place; raise ReauthRequired if it cannot
      consenter()   -> run the one-time browser consent, return credentials
      saver(c)      -> persist credentials to the token cache
    A credentials object exposes `.token`, `.valid`, `.expired`, `.refresh_token`
    (exactly what google.oauth2.credentials.Credentials provides).
    """

    def __init__(
        self,
        config,
        *,
        loader: Optional[Callable[[], object]] = None,
        refresher: Optional[Callable[[object], None]] = None,
        consenter: Optional[Callable[[], object]] = None,
        saver: Optional[Callable[[object], None]] = None,
    ) -> None:
        self.config = config
        self._loader = loader or self._default_loader
        self._refresher = refresher or self._default_refresher
        self._consenter = consenter or self._default_consenter
        self._saver = saver or self._default_saver

    def bearer(self) -> str:
        creds = self._loader()
        if creds is None:
            # First run: the one-time browser consent Scott approves once.
            creds = self._consenter()
            self._saver(creds)
            return creds.token
        if getattr(creds, "valid", False):
            return creds.token
        if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            self._refresher(creds)  # raises ReauthRequired if the token is dead
            self._saver(creds)
            return creds.token
        # Present but unusable and not refreshable -> tell Scott how to re-auth.
        raise ReauthRequired(REAUTH_MESSAGE)

    # --- google-touching seams (lazy imports) ---------------------------------
    # Their own exception handling IS unit-tested (see tests/test_auth.py),
    # by stubbing sys.modules for the exact dotted names imported below - the
    # real google-auth / google-auth-oauthlib flow is exercised only at a real
    # sign-in.

    def _default_loader(self):
        if not os.path.exists(self.config.token_path):
            return None
        from google.oauth2.credentials import Credentials

        try:
            return Credentials.from_authorized_user_file(
                str(self.config.token_path), self.config.scopes
            )
        except (ValueError, json.JSONDecodeError):
            # from_authorized_user_file raises plain ValueError for a token
            # missing a required field (e.g. refresh_token) and
            # json.JSONDecodeError for invalid JSON; json.JSONDecodeError is
            # itself a ValueError, but both are named here for clarity. Never
            # include the file's contents below (leak-safety) - path only.
            raise CredentialFileError(
                f"The saved sign-in file at {self.config.token_path} is malformed "
                "or incomplete (a required field is missing, or the file is not "
                "valid JSON). Local fix: delete secrets/token.json and run this "
                "command again to sign in again. If this happened in GitHub "
                "Actions, the YT_OAUTH_TOKEN_JSON repository secret is bad - "
                "re-paste the full contents of a freshly created local "
                "token.json into it."
            ) from None

    def _default_refresher(self, creds) -> None:
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except TransportError:
            # A network hiccup during refresh, not a dead sign-in - re-auth is
            # the wrong advice here; this is safe to just retry later.
            raise MetricsApiError(
                0, "network error while refreshing the sign-in token; safe to retry later"
            ) from None
        except RefreshError:
            raise ReauthRequired(REAUTH_MESSAGE) from None

    def _default_consenter(self):
        from google_auth_oauthlib.flow import InstalledAppFlow

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.client_secret_path), self.config.scopes
            )
        except FileNotFoundError:
            raise CredentialFileError(
                f"The OAuth client-secret file was not found at "
                f"{self.config.client_secret_path}. Download it from Google "
                "Cloud Console and save it there (or point YT_OAUTH_CLIENT_SECRET "
                "at wherever you saved it), then run this command again. "
                "Full steps: shared-brain/runbooks/metrics-api-setup.md"
            ) from None
        # port=0 lets the OS pick a free loopback port for the one-time consent.
        return flow.run_local_server(port=0)

    def _default_saver(self, creds) -> None:
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_path.write_text(creds.to_json(), encoding="utf-8")

    def __repr__(self) -> str:
        # Never leak the token or the secret contents; the path is not sensitive.
        return f"OAuthTokenProvider(token_path={self.config.token_path!s})"

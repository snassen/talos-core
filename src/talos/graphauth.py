"""Signing in to Microsoft Graph as the owner: read-only scopes for sync, and write scopes kept apart.

The Entra app "Talos (local)" is a public client: no secret exists. The first
sign-in uses the device-code flow. Talos prints a code, and the owner enters it at
microsoft.com/devicelogin themselves, so their password never passes through Talos.
MSAL's token cache (it holds the refresh token) is kept in the Keychain; later
runs refresh silently.

Sync reads with read-only scopes (SCOPES). The same sign-in also asks for write scopes, each
consented to by the owner:
- Mail.ReadWrite (WRITE_SCOPES), used only by the write-back executor for changesets the owner commits;
- Mail.Send (SEND_SCOPES), used only by the send module when the owner presses Send and confirms the account;
- Calendars.ReadWrite (CALENDAR_SCOPES), used only by talos.calwrite for entries the owner saves in Talos Web.
Each use asks for a token with exactly its own scopes, so sync never holds a write token.
"""

from __future__ import annotations

import msal

from talos import secrets

SCOPES = ["User.Read", "Mail.Read", "Chat.Read", "ChannelMessage.Read.All", "Calendars.Read",
          "Team.ReadBasic.All", "Channel.ReadBasic.All"]
"""Read-only. The last two (admin consent in Entra) let the Teams sync list the teams the owner is in
and their channels; without them /me/joinedTeams answered 403 and no channel was read."""

WRITE_SCOPES = ["Mail.ReadWrite"]
"""For the write-back executor (talos.writeback.graph) only: changesets the owner commits."""

SEND_SCOPES = ["Mail.Send"]
"""For the send module only: a mail the owner composed, sent after they confirmed the account."""

CALENDAR_SCOPES = ["Calendars.ReadWrite"]
"""For talos.calwrite only: an entry the owner saves in Talos Web, written to their Outlook calendar."""

TEAMS_SEND_SCOPES = ["ChannelMessage.Send", "ChatMessage.Send"]
"""For the send module only: a Teams post the owner wrote, sent when they pressed Enter in its conversation."""

CONSENT_SCOPES = SCOPES + WRITE_SCOPES + SEND_SCOPES + CALENDAR_SCOPES + TEAMS_SEND_SCOPES
"""What the interactive sign-in asks the owner to consent to, in one go."""


class GraphAuth:
    def __init__(self, account_id: str, tenant_id: str, client_id: str):
        self.key = f"graph-token-cache:{account_id}"
        self.cache = msal.SerializableTokenCache()
        stored = secrets.get_optional(self.key)
        if stored:
            self.cache.deserialize(stored)
        self.app = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", token_cache=self.cache)

    def _save(self) -> None:
        if self.cache.has_state_changed:
            secrets.put(self.key, self.cache.serialize())

    def sign_in(self, show=print) -> str:
        """Interactive device-code sign-in. Returns the signed-in username."""
        flow = self.app.initiate_device_flow(scopes=CONSENT_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"device flow failed: {flow.get('error_description', flow)}")
        show(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            if result.get("error") in ("authorization_pending", "expired_token", "code_expired"):
                raise RuntimeError("The sign-in code expired before it was entered (it lasts about 15 minutes). "
                                   "Run the command again when you are at the keyboard.")
            raise RuntimeError(f"sign-in failed: {result.get('error_description', result)}")
        self._save()
        return (result.get("id_token_claims") or {}).get("preferred_username", "?")

    def token(self, *, scopes: list[str] | None = None, force_refresh: bool = False) -> str:
        """An access token from the cache for exactly these scopes (default: the read-only SCOPES).
        force_refresh (after a 401) redeems the refresh token."""
        accounts = self.app.get_accounts()
        if not accounts:
            raise secrets.MissingSecret("Not signed in to Microsoft. Run: talos auth graph <account>")
        result = self.app.acquire_token_silent(scopes or SCOPES, account=accounts[0], force_refresh=force_refresh)
        if not result or "access_token" not in result:
            if scopes and scopes != SCOPES:
                raise secrets.MissingSecret(f"The Microsoft sign-in does not include {', '.join(scopes)}. Add it to the"
                                            " Entra app, then sign in again: talos auth graph <account>")
            raise secrets.MissingSecret("Microsoft sign-in expired. Run: talos auth graph <account>")
        self._save()
        return result["access_token"]

    def granted(self, scope: str) -> bool:
        """Whether the cached sign-in can give a token for this scope (no network when it cannot)."""
        try:
            self.token(scopes=[scope])
            return True
        except secrets.MissingSecret:
            return False

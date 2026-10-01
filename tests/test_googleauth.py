"""Signing in to Google (talos.googleauth.sign_in): the consent comes back to a one-shot local
server, the code is exchanged for a refresh token, and the token goes to the Keychain. Nothing
here reaches Google: the browser is a function and the token endpoint a mock."""

import json
import urllib.parse
import urllib.request

import httpx

from talos import googleauth, secrets


def test_signing_in_takes_the_code_from_the_local_server_and_keeps_the_refresh_token(monkeypatch):
    stored = {}
    monkeypatch.setattr(secrets, "get", lambda key: json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    monkeypatch.setattr(secrets, "put", lambda key, value: stored.__setitem__(key, value))

    def browser(url):  # Google's consent page answers by sending the browser back to Talos
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        back = q["redirect_uri"] + "?" + urllib.parse.urlencode({"state": q["state"], "code": "the-code"})
        urllib.request.urlopen(back, timeout=5).read()

    sent = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        sent.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, json={"refresh_token": "rt", "scope": "calendar.events"})

    scope = googleauth.sign_in("gmail", open_browser=browser, show=lambda s: None, timeout=10,
                               client=httpx.Client(transport=httpx.MockTransport(token_endpoint)))
    assert scope == "calendar.events"
    assert sent[0]["code"] == "the-code" and sent[0]["grant_type"] == "authorization_code" and sent[0]["code_verifier"]
    assert json.loads(stored[googleauth.token_key("gmail")]) == {"refresh_token": "rt", "scope": "calendar.events"}

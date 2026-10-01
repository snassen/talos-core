"""Sign-in consents to read + write + send at once; each use asks for exactly its own scopes."""

from talos import graphauth, secrets


class FakeApp:
    def __init__(self, granted):
        self.granted, self.asked = set(granted), []

    def get_accounts(self):
        return [{"username": "me"}]

    def acquire_token_silent(self, scopes, account=None, force_refresh=False):
        self.asked.append(list(scopes))
        return {"access_token": "t-" + "+".join(scopes)} if set(scopes) <= self.granted else None

    def initiate_device_flow(self, scopes):
        self.flow_scopes = list(scopes)
        return {}


def auth_with(granted):
    a = graphauth.GraphAuth.__new__(graphauth.GraphAuth)
    a.key, a.app = "graph-token-cache:test", FakeApp(granted)
    a.cache = type("C", (), {"has_state_changed": False})()
    return a


def test_the_default_token_is_read_only_and_write_tokens_are_asked_for_by_name():
    a = auth_with(graphauth.SCOPES + ["Mail.ReadWrite"])
    assert a.token() == "t-" + "+".join(graphauth.SCOPES)
    assert a.token(scopes=graphauth.WRITE_SCOPES) == "t-Mail.ReadWrite"
    assert a.granted("Mail.ReadWrite") and not a.granted("Mail.Send")


def test_a_missing_write_scope_says_how_to_add_it():
    a = auth_with(graphauth.SCOPES)
    try:
        a.token(scopes=graphauth.SEND_SCOPES)
    except secrets.MissingSecret as exc:
        assert "Mail.Send" in str(exc) and "talos auth graph" in str(exc)
    else:
        raise AssertionError("expected MissingSecret")


def test_sign_in_asks_for_read_write_send_calendar_write_and_teams_posting_in_one_consent():
    assert graphauth.CONSENT_SCOPES == graphauth.SCOPES + ["Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite",
                                                           "ChannelMessage.Send", "ChatMessage.Send"]
    assert "ReadWrite" not in " ".join(graphauth.SCOPES) and "Send" not in " ".join(graphauth.SCOPES)

"""API key resolution and masking.

The credential store itself is not exercised -- writing to the real Windows
Credential Manager from a test would leave state on the developer's machine.
What is tested is the pure logic around it: precedence, masking, and the
degrade-to-environment path when no store exists.
"""

from __future__ import annotations

import pytest

from sway import credentials


@pytest.fixture
def no_keyring(monkeypatch):
    """Pretend this machine has no credential store at all."""
    monkeypatch.setattr(credentials, "_keyring", lambda: None)


@pytest.fixture
def fake_keyring(monkeypatch):
    """A stand-in credential store, so precedence is testable in isolation."""

    class FakeKeyring:
        def __init__(self):
            self.values: dict[tuple[str, str], str] = {}

        def get_password(self, service, account):
            return self.values.get((service, account))

        def set_password(self, service, account, value):
            self.values[(service, account)] = value

        def delete_password(self, service, account):
            del self.values[(service, account)]

    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    return fake


# -- normalising -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  gsk_abc  ", "gsk_abc"),
        ('"gsk_abc"', "gsk_abc"),
        ("'gsk_abc'", "gsk_abc"),
        ("\ngsk_abc\n", "gsk_abc"),
        ("", ""),
        (None, ""),
        (42, ""),
    ],
)
def test_normalise_key(raw, expected):
    assert credentials.normalise_key(raw) == expected


def test_normalise_caps_absurd_input():
    assert len(credentials.normalise_key("x" * 5000)) == 512


# -- masking ---------------------------------------------------------------


def test_mask_keeps_the_last_four_characters():
    """Two keys must stay tellable apart; that is what masking must not take."""
    masked = credentials.mask("gsk_abcdefghijklmnopqrstuvwxyz7d4f")
    assert masked.startswith("gsk_")
    assert masked.endswith("7d4f")
    assert "abcdefgh" not in masked


def test_mask_of_a_short_value_reveals_nothing():
    assert set(credentials.mask("abcd")) == {"•"}


def test_mask_of_nothing_is_nothing():
    assert credentials.mask("") == ""


# -- plausibility ----------------------------------------------------------


def test_looks_like_key_accepts_a_real_shape():
    assert credentials.looks_like_key("gsk_" + "a" * 40)


@pytest.mark.parametrize(
    "value",
    ["", "curl https://api.groq.com", "sk-openai-style", "gsk_short"],
)
def test_looks_like_key_rejects_obvious_mistakes(value):
    assert not credentials.looks_like_key(value)


# -- precedence ------------------------------------------------------------


def test_keyring_wins_over_everything(fake_keyring):
    fake_keyring.set_password(credentials.SERVICE_NAME, credentials.ACCOUNT_NAME, "gsk_stored")
    key, source = credentials.resolve_key(
        {"FLOW_GROQ_API_KEY": "gsk_override", "GROQ_API_KEY": "gsk_dotenv"}
    )
    assert (key, source) == ("gsk_stored", "keyring")


def test_explicit_override_beats_the_bare_variable(no_keyring):
    """A key exported for another tool must not outrank a deliberate one."""
    key, source = credentials.resolve_key(
        {"FLOW_GROQ_API_KEY": "gsk_override", "GROQ_API_KEY": "gsk_dotenv"}
    )
    assert (key, source) == ("gsk_override", "env")


def test_falls_back_to_the_bare_variable(no_keyring):
    assert credentials.resolve_key({"GROQ_API_KEY": "gsk_dotenv"}) == ("gsk_dotenv", "dotenv")


def test_nothing_configured(no_keyring):
    assert credentials.resolve_key({}) == ("", "")


def test_blank_values_are_not_treated_as_configured(no_keyring):
    assert credentials.resolve_key({"GROQ_API_KEY": "   "}) == ("", "")


# -- migration -------------------------------------------------------------


def test_migration_copies_an_env_key_into_the_store(fake_keyring):
    assert credentials.migrate_to_keyring({"GROQ_API_KEY": "gsk_dotenv"}) is True
    assert credentials.read_stored_key() == "gsk_dotenv"


def test_migration_never_overwrites_an_existing_stored_key(fake_keyring):
    fake_keyring.set_password(credentials.SERVICE_NAME, credentials.ACCOUNT_NAME, "gsk_stored")
    assert credentials.migrate_to_keyring({"GROQ_API_KEY": "gsk_dotenv"}) is False
    assert credentials.read_stored_key() == "gsk_stored"


def test_migration_with_nothing_to_copy(fake_keyring):
    assert credentials.migrate_to_keyring({}) is False


def test_migration_is_a_no_op_without_a_store(no_keyring):
    """A machine with no credential store must still start and run."""
    assert credentials.migrate_to_keyring({"GROQ_API_KEY": "gsk_dotenv"}) is False


# -- degradation -----------------------------------------------------------


def test_reading_without_a_store_returns_nothing(no_keyring):
    assert credentials.read_stored_key() == ""


def test_storing_without_a_store_says_why(no_keyring):
    with pytest.raises(credentials.CredentialError, match="no credential store"):
        credentials.store_key("gsk_abc")


def test_storing_an_empty_key_is_refused(fake_keyring):
    with pytest.raises(credentials.CredentialError, match="empty"):
        credentials.store_key("   ")


def test_clearing_a_missing_key_is_not_an_error(fake_keyring):
    credentials.clear_key()  # must not raise


def test_a_broken_store_degrades_rather_than_raising(monkeypatch):
    """A credential store that throws must not take the app down."""

    class Exploding:
        def get_password(self, *_):
            raise RuntimeError("vault on fire")

    monkeypatch.setattr(credentials, "_keyring", lambda: Exploding())
    assert credentials.read_stored_key() == ""

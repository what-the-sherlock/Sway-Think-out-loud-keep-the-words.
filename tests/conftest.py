"""Shared fixtures.

Kept deliberately small: the suite's style is to build what a test needs
inline, and the only thing worth centralising is a fully-resolved
:class:`~flow.config.Config`, which several of the newer tests need and which
is fiddly to construct by hand.
"""

from __future__ import annotations

import pytest

from sway.config import load_config


@pytest.fixture(autouse=True)
def _no_real_credential_store(monkeypatch, request):
    """Keep every test out of the real Windows Credential Manager.

    ``flow.config`` resolves the API key through :mod:`flow.credentials`,
    which reads the OS credential vault. Without this the suite would behave
    differently depending on whether the developer happens to have a Flow key
    stored -- and a test could, in principle, write to their real vault.

    Tests that are specifically *about* credential resolution opt out by
    requesting the ``fake_keyring`` or ``no_keyring`` fixture, which install
    their own stand-in after this one runs.
    """
    if {"fake_keyring", "no_keyring"} & set(request.fixturenames):
        return
    from sway import credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)


@pytest.fixture
def make_config(tmp_path):
    """A real, validated Config rooted in a temporary data directory.

    Goes through :func:`~flow.config.load_config` rather than constructing the
    dataclass directly, so a test exercising the settings overlay is working
    against the same object the app builds at startup -- including any
    validation or defaulting that happens on the way.
    """

    def _make(**env):
        environment = {
            "FLOW_DATA_DIR": str(tmp_path),
            "GROQ_API_KEY": "gsk_test_key_not_real",
            **env,
        }
        # env_file points at a path that does not exist, so a developer's real
        # .env can never leak into a test run.
        return load_config(env=environment, env_file=tmp_path / "absent.env")

    return _make

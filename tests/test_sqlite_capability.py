"""Behaviour when the interpreter cannot load SQLite extensions."""
from __future__ import annotations

import pytest

from agent_history import store


def test_missing_extension_support_raises_an_actionable_error(monkeypatch, fake_embed):
    """Some Python builds compile extension loading out; say so clearly."""
    import sqlite3

    real_connect = sqlite3.connect

    class NoExtensions:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "enable_load_extension":
                raise AttributeError(name)
            return getattr(self._inner, name)

    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: NoExtensions(real_connect(*a, **k)))
    with pytest.raises(store.StoreError) as e:
        store.open_for_write()
    msg_text = str(e.value)
    assert "extension support" in msg_text
    assert "enable_load_extension" in msg_text, "must show the check to run"


def test_extensions_supported_returns_a_bool():
    assert isinstance(store.extensions_supported(), bool)

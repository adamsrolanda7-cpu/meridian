"""Chargement du .env : parsing, priorité et masquage des secrets."""
from __future__ import annotations

import os

from meridian.env import load_env, mask_secret, parse_env


def test_parse_env_supports_quotes_comments_and_export():
    values = parse_env(
        """
        # commentaire
        MERIDIAN_EXCHANGE_ID=binance
        export MERIDIAN_QUOTE="USDT"
        SECRET='abc#def'
        INLINE=value # commentaire inline
        VIDE=
        """
    )
    assert values["MERIDIAN_EXCHANGE_ID"] == "binance"
    assert values["MERIDIAN_QUOTE"] == "USDT"
    assert values["SECRET"] == "abc#def"
    assert values["INLINE"] == "value"
    assert values["VIDE"] == ""


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("MERIDIAN_TEST_KEY=fichier\n", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_TEST_KEY", "environnement")
    load_env(str(path))
    assert os.environ["MERIDIAN_TEST_KEY"] == "environnement"
    load_env(str(path), override=True)
    assert os.environ["MERIDIAN_TEST_KEY"] == "fichier"


def test_load_env_missing_file_is_silent(tmp_path):
    assert load_env(str(tmp_path / "absent.env")) == {}


def test_mask_secret_never_returns_full_value():
    masked = mask_secret("abcdef123456")
    assert masked.startswith("abcd")
    assert "ef123456" not in masked
    assert mask_secret("") == "(vide)"

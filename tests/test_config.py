from pathlib import Path

import pytest

from streakbot.config import ConfigError, load_settings

HASH = "scrypt$16384$8$1$c2FsdA==$aGFzaA=="


def test_defaults():
    s = load_settings({"PANEL_PASSWORD_HASH": HASH})
    assert s.password_hash == HASH
    assert s.port == 8080 and s.default_timezone == "UTC" and s.data_dir == Path("data")


def test_all_values():
    s = load_settings({"PANEL_PASSWORD_HASH": HASH, "PANEL_PORT": "9000",
                       "DEFAULT_TIMEZONE": "Europe/Moscow", "DATA_DIR": "/x"})
    assert (s.port, s.default_timezone, s.data_dir) == (9000, "Europe/Moscow", Path("/x"))


def test_missing_hash():
    with pytest.raises(ConfigError, match="PANEL_PASSWORD_HASH"):
        load_settings({})


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_bad_port(port):
    with pytest.raises(ConfigError, match="PANEL_PORT"):
        load_settings({"PANEL_PASSWORD_HASH": HASH, "PANEL_PORT": port})


def test_bad_timezone():
    with pytest.raises(ConfigError, match="DEFAULT_TIMEZONE"):
        load_settings({"PANEL_PASSWORD_HASH": HASH, "DEFAULT_TIMEZONE": "Mars/Base"})


@pytest.mark.parametrize("raw", [f"'{HASH}'", f'"{HASH}"'])
def test_quoted_hash_unwrapped(raw):
    assert load_settings({"PANEL_PASSWORD_HASH": raw}).password_hash == HASH

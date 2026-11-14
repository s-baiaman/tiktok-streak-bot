from streakbot.crypto import CookieBox, load_or_create_key


def test_roundtrip(tmp_path):
    box = CookieBox(load_or_create_key(tmp_path / "k"))
    cookies = [{"name": "sessionid", "value": "secret123", "domain": ".tiktok.com", "path": "/"}]
    blob = box.seal(cookies)
    assert b"secret123" not in blob
    assert box.open(blob) == cookies


def test_key_file_is_private_and_stable(tmp_path):
    k1 = load_or_create_key(tmp_path / "k")
    k2 = load_or_create_key(tmp_path / "k")
    assert k1 == k2
    assert (tmp_path / "k").stat().st_mode & 0o777 == 0o600


def test_key_parent_dir_created(tmp_path):
    load_or_create_key(tmp_path / "nested" / "k")
    assert (tmp_path / "nested" / "k").exists()

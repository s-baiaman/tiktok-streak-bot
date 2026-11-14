import os
import subprocess
import sys

from streakbot.auth import verify_password


def run(args, stdin="", env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PANEL_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "streakbot", *args], input=stdin, capture_output=True,
                          text=True, env=env, cwd=cwd, timeout=60)


def test_missing_password_hash_exits_with_message(tmp_path):
    r = run([], env_extra={"DATA_DIR": str(tmp_path)})
    assert r.returncode == 1
    assert "Configuration error" in r.stderr and "PANEL_PASSWORD_HASH" in r.stderr


def test_hash_password_command():
    r = run(["hash-password"], stdin="correct horse battery\n")
    assert r.returncode == 0
    stored = r.stdout.strip()
    assert verify_password("correct horse battery", stored)
    assert "correct horse" not in r.stdout


def test_hash_password_rejects_empty():
    r = run(["hash-password"], stdin="\n")
    assert r.returncode == 1 and "empty" in r.stderr


def test_unknown_command():
    r = run(["bogus"])
    assert r.returncode == 2

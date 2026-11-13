import subprocess

from fireball.calendars.gog import _explain_failure


def _failed(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gog"], returncode=1, stdout="", stderr=stderr)


def test_missing_keyring_password_points_to_daemon_env():
    proc = _failed(
        "read encoded file keyring item: no TTY available for keyring file "
        "backend password prompt; set GOG_KEYRING_PASSWORD"
    )
    message = _explain_failure(proc, "pessoa@exemplo.com")
    assert "GOG_KEYRING_PASSWORD" in message
    assert "fireball daemon restart" in message


def test_expired_token_still_asks_for_auth_add():
    message = _explain_failure(_failed("oauth2: invalid_grant"), "pessoa@exemplo.com")
    assert "gog auth add pessoa@exemplo.com" in message

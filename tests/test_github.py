import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vivado_runners.github import GitHubClient, GitHubError, JitRunner, Runner

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


class Recorder:
    """A Transport that answers from a script and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload).encode() if payload is not None else b""


TOKEN = (201, {"token": "ghs_abc", "expires_at": "2026-10-02T01:00:00Z"})
NOW = 1790900400.0  # 2026-10-02T00:20:00Z


def client(rec, now=NOW):
    return GitHubClient("fpgas-online", 123, 456, KEY_PEM, transport=rec, now=lambda: now)


def test_app_jwt_is_rs256_signed_and_short_lived():
    rec = Recorder(TOKEN, (200, {"total_count": 0, "runners": []}))
    client(rec).list_runners()
    method, url, headers, _ = rec.calls[0]
    assert (method, url) == ("POST", "https://api.github.com/app/installations/456/access_tokens")
    claims = jwt.decode(
        headers["Authorization"].removeprefix("Bearer "),
        PUBLIC_PEM,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": "123"}


def test_installation_token_is_reused_until_near_expiry():
    rec = Recorder(TOKEN, (200, {"runners": []}), (200, {"runners": []}))
    c = client(rec)
    c.list_runners()
    c.list_runners()
    assert [u for _, u, _, _ in rec.calls].count("https://api.github.com/app/installations/456/access_tokens") == 1
    assert rec.calls[1][2]["Authorization"] == "Bearer ghs_abc"


def test_installation_token_is_refreshed_five_minutes_before_expiry():
    clock = {"t": NOW}
    rec = Recorder(TOKEN, (200, {"runners": []}), TOKEN, (200, {"runners": []}))
    c = GitHubClient("fpgas-online", 123, 456, KEY_PEM, transport=rec, now=lambda: clock["t"])
    c.list_runners()
    clock["t"] = NOW + 36 * 60  # 00:56, four minutes before expiry
    c.list_runners()
    assert [u for _, u, _, _ in rec.calls].count("https://api.github.com/app/installations/456/access_tokens") == 2


def test_create_jit_posts_the_documented_body():
    rec = Recorder(TOKEN, (201, {"runner": {"id": 23}, "encoded_jit_config": "abc=="}))
    jit = client(rec).create_jit("alpha-slot0-deadbeef", 7, ["self-hosted", "vivado-2025.2"])
    assert jit == JitRunner(id=23, encoded_jit_config="abc==")
    method, url, _, body = rec.calls[1]
    assert (method, url) == ("POST", "https://api.github.com/orgs/fpgas-online/actions/runners/generate-jitconfig")
    assert json.loads(body) == {
        "name": "alpha-slot0-deadbeef",
        "runner_group_id": 7,
        "labels": ["self-hosted", "vivado-2025.2"],
    }


def test_runner_group_id_finds_by_name_and_fails_when_absent():
    groups = {"runner_groups": [{"id": 1, "name": "Default"}, {"id": 7, "name": "vivado"}]}
    assert client(Recorder(TOKEN, (200, groups))).runner_group_id("vivado") == 7
    with pytest.raises(GitHubError, match="runner group 'nope' not found"):
        client(Recorder(TOKEN, (200, groups))).runner_group_id("nope")


def test_list_runners_follows_pages():
    page1 = {"runners": [{"id": i, "name": f"r{i}", "status": "online", "busy": False} for i in range(100)]}
    page2 = {"runners": [{"id": 100, "name": "r100", "status": "offline", "busy": True}]}
    rec = Recorder(TOKEN, (200, page1), (200, page2))
    runners = client(rec).list_runners()
    assert len(runners) == 101
    assert runners[-1] == Runner(id=100, name="r100", status="offline", busy=True)
    assert rec.calls[1][1].endswith("/actions/runners?per_page=100&page=1")
    assert rec.calls[2][1].endswith("/actions/runners?per_page=100&page=2")


def test_delete_runner_accepts_204_and_404_and_raises_otherwise():
    client(Recorder(TOKEN, (204, None))).delete_runner(23)
    client(Recorder(TOKEN, (404, {"message": "Not Found"}))).delete_runner(23)
    with pytest.raises(GitHubError) as e:
        client(Recorder(TOKEN, (422, {"message": "busy"}))).delete_runner(23)
    assert e.value.status == 422


def test_errors_carry_status_and_body():
    with pytest.raises(GitHubError) as e:
        client(Recorder((401, {"message": "Bad credentials"}))).list_runners()
    assert e.value.status == 401
    assert "Bad credentials" in str(e.value)

"""The four GitHub API calls the controller makes, authenticated as a GitHub App."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import jwt

API = "https://api.github.com"
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class GitHubError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"GitHub API {status}: {body}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Runner:
    id: int
    name: str
    status: str
    busy: bool


@dataclass(frozen=True)
class JitRunner:
    id: int
    encoded_jit_config: str


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class GitHubClient:
    def __init__(
        self,
        org: str,
        app_id: int,
        installation_id: int,
        key_pem: str,
        transport: Transport = urllib_transport,
        now: Callable[[], float] = time.time,
    ):
        self._org = org
        self._app_id = app_id
        self._installation_id = installation_id
        self._key_pem = key_pem
        self._transport = transport
        self._now = now
        self._token: str | None = None
        self._token_expiry = 0.0

    def _headers(self, bearer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vivado-runners",
        }

    def _installation_token(self) -> str:
        if self._token and self._now() < self._token_expiry - 300:
            return self._token
        now = int(self._now())
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)}, self._key_pem, algorithm="RS256"
        )
        url = f"{API}/app/installations/{self._installation_id}/access_tokens"
        status, body = self._transport("POST", url, self._headers(app_jwt), b"")
        if status != 201:
            raise GitHubError(status, body.decode(errors="replace"))
        data = json.loads(body)
        self._token = data["token"]
        self._token_expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        return self._token

    def _call(self, method: str, path: str, payload: dict | None = None, ok: tuple[int, ...] = (200,)):
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self._transport(method, f"{API}{path}", self._headers(self._installation_token()), body)
        if status not in ok:
            raise GitHubError(status, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    def runner_group_id(self, name: str) -> int:
        groups = self._call("GET", f"/orgs/{self._org}/actions/runner-groups?per_page=100")["runner_groups"]
        for group in groups:
            if group["name"] == name:
                return group["id"]
        raise GitHubError(404, f"runner group {name!r} not found in {self._org}")

    def create_jit(self, name: str, group_id: int, labels: list[str]) -> JitRunner:
        data = self._call(
            "POST",
            f"/orgs/{self._org}/actions/runners/generate-jitconfig",
            {"name": name, "runner_group_id": group_id, "labels": labels},
            ok=(201,),
        )
        return JitRunner(id=data["runner"]["id"], encoded_jit_config=data["encoded_jit_config"])

    def list_runners(self) -> list[Runner]:
        runners: list[Runner] = []
        page = 1
        while True:
            batch = self._call("GET", f"/orgs/{self._org}/actions/runners?per_page=100&page={page}")["runners"]
            runners += [Runner(id=r["id"], name=r["name"], status=r["status"], busy=r["busy"]) for r in batch]
            if len(batch) < 100:
                return runners
            page += 1

    def delete_runner(self, runner_id: int) -> None:
        self._call("DELETE", f"/orgs/{self._org}/actions/runners/{runner_id}", ok=(204, 404))

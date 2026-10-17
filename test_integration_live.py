"""Live integration test for SPEC acceptance scenario 1.

Unlike ``test_agent_relay.py`` (which drives the app in-process through
TestClient), this starts a real uvicorn server in a subprocess against its own
freshly created PostgreSQL database, talks to it over HTTP, and then inspects
the stored rows directly with psycopg.  It deliberately imports nothing from
the app so the server's database is the only one involved.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent
STARTUP_TIMEOUT_SECONDS = 20


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_relay(tmp_path: Path, scratch_database) -> Iterator[tuple[str, str]]:
    """Run the relay as a separate process against a scratch PostgreSQL database."""

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "RELAY_DATABASE_URL": scratch_database.app_url}
    env.pop("DATABASE_URL", None)
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            if server.poll() is not None:
                pytest.fail(f"relay exited during startup:\n{log_path.read_text(errors='replace')}")
            try:
                if httpx.get(f"{base_url}/ready", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"relay not ready after {STARTUP_TIMEOUT_SECONDS}s:\n{log_path.read_text(errors='replace')}")
            time.sleep(0.2)
        yield base_url, scratch_database.libpq_url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def register(client: httpx.Client, name: str) -> tuple[str, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    assert data["agent_id"].startswith("agent_") and data["token"]
    return data["agent_id"], {"Authorization": f"Bearer {data['token']}"}


def test_scenario_1_two_agents_exchange_task_and_result(live_relay):
    base_url, db_url = live_relay
    task_input = "Review this Python function: def add(a, b): return a - b"
    task_output = "Bug: add() subtracts. Use return a + b."

    with httpx.Client(base_url=base_url, timeout=40) as client:
        # 1. Register two agents.
        alice_id, alice = register(client, "alice-sender")
        bob_id, bob = register(client, "bob-reviewer")

        # 2. Alice sends a task to Bob; the server records Alice as sender.
        sent = client.post(
            "/api/v1/tasks",
            headers={**alice, "Idempotency-Key": "scenario-1"},
            json={"to": bob_id, "input": task_input},
        )
        assert sent.status_code == 201
        assert sent.json()["status"] == "queued"
        task_id = sent.json()["task_id"]
        assert client.get(f"/api/v1/tasks/{task_id}", headers=alice).json()["status"] == "queued"

        # 3. Bob claims it.
        claim = client.post("/api/v1/tasks/claim", headers=bob, json={"worker_id": "bob-laptop-1", "wait_seconds": 5})
        assert claim.status_code == 200
        claimed = claim.json()
        assert claimed["task_id"] == task_id
        assert claimed["from"] == alice_id
        assert claimed["input"] == task_input
        assert claimed["attempt"] == 1
        assert claimed["claim_token"] and claimed["lease_expires_at"]
        assert client.get(f"/api/v1/tasks/{task_id}", headers=alice).json()["status"] == "processing"

        # 4. Bob completes it.
        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=bob,
            json={"claim_token": claimed["claim_token"], "output": task_output},
        )
        assert complete.status_code == 200
        assert complete.json() == {"task_id": task_id, "status": "completed"}

        # 5. Alice reads the result.
        result = client.get(f"/api/v1/tasks/{task_id}", headers=alice)
        assert result.status_code == 200
        task = result.json()
        assert task["from"] == alice_id
        assert task["to"] == bob_id
        assert task["input"] == task_input
        assert task["status"] == "completed"
        assert task["output"] == task_output
        assert task["error"] is None
        assert task["attempt_count"] == 1
        assert task["finished_at"] is not None

        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=alice).json()["items"]
        assert len(attempts) == 1
        assert attempts[0]["worker_id"] == "bob-laptop-1"
        assert attempts[0]["outcome"] == "completed"
        assert "claim_token" not in attempts[0]

        # The inbox is drained: nothing left to claim.
        empty = client.post("/api/v1/tasks/claim", headers=bob, json={"wait_seconds": 0})
        assert empty.status_code == 204

    # The persisted state matches what the API reported.
    with psycopg.connect(db_url) as db:
        rows = db.execute(
            "SELECT sender_id, recipient_id, status, output, attempt_count FROM tasks WHERE id = %s", (task_id,)
        ).fetchall()
        assert rows == [(alice_id, bob_id, "completed", task_output, 1)]
        attempt_rows = db.execute(
            "SELECT attempt_number, worker_id, outcome, claim_token_hash FROM attempts WHERE task_id = %s", (task_id,)
        ).fetchall()
        assert len(attempt_rows) == 1
        assert attempt_rows[0][:3] == (1, "bob-laptop-1", "completed")
        # Only a hash of the claim token is stored, never the token itself.
        assert attempt_rows[0][3] != claimed["claim_token"]
        assert len(attempt_rows[0][3]) == 64
        token_hashes = [row[0] for row in db.execute("SELECT token_hash FROM agents")]
        assert len(token_hashes) == 2
        assert alice["Authorization"].split()[1] not in token_hashes

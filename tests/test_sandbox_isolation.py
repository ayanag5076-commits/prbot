"""Sandbox isolation: Agent C must give untrusted test code zero network egress and no privileges.

Two layers:
  * always: the docker command carries every isolation flag (runs anywhere, including CI without Docker)
  * PRBOT_RUN_DOCKER_TESTS=1 + Docker: a real container tries to open sockets and must send 0 bytes
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from prbot.agents.tester import DockerRunner

IMAGE = os.environ.get("PRBOT_TEST_IMAGE", "prbot-tester:py3.12")


def test_sandbox_isolation_flags():
    cmd = DockerRunner(IMAGE).build_cmd(Path("/tmp/x"), "c1")
    joined = " ".join(cmd)
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in cmd and "no-new-privileges" in joined
    assert "--memory" in cmd and "--pids-limit" in cmd and "--cpus" in cmd
    assert "/tmp/x:/workspace:ro" in cmd  # the repo is mounted read-only
    assert "--privileged" not in cmd and "--net=host" not in joined and "host" != cmd[cmd.index("--network") + 1]


PROBE = r'''
import socket
sent = 0
for host, port in [("1.1.1.1", 53), ("8.8.8.8", 443), ("github.com", 443)]:
    try:
        s = socket.create_connection((host, port), timeout=3)
        sent += s.send(b"ping")
        s.close()
    except OSError:
        pass
print("EGRESS_BYTES", sent)
'''

docker_ready = (os.environ.get("PRBOT_RUN_DOCKER_TESTS") == "1" and shutil.which("docker") is not None)


@pytest.mark.skipif(not docker_ready, reason="set PRBOT_RUN_DOCKER_TESTS=1 with Docker and the tester image built")
def test_sandbox_isolation_live_egress(tmp_path):
    (tmp_path / "probe.py").write_text(PROBE)
    # the container user has no CAP_DAC_OVERRIDE (--cap-drop ALL), so the mount must be world-readable
    tmp_path.chmod(0o755)
    (tmp_path / "probe.py").chmod(0o644)
    cmd = DockerRunner(IMAGE, command=["python", "probe.py"]).build_cmd(tmp_path, "prbot-egress-probe")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert "EGRESS_BYTES 0" in out.stdout, out.stdout + out.stderr

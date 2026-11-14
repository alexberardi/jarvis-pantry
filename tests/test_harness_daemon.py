"""The container harness checks a daemon without ever awaiting run(), which never returns."""
import pytest

from app.services import test_harness


def _run(tmp_path, monkeypatch, source: str) -> dict:
    pytest.importorskip("jarvis_command_sdk.daemon")
    (tmp_path / "daemons" / "bridge").mkdir(parents=True)
    (tmp_path / "daemons" / "bridge" / "daemon.py").write_text(source)
    monkeypatch.setattr(test_harness, "_REPO_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path / "daemons" / "bridge"))
    monkeypatch.delitem(__import__("sys").modules, "daemon", raising=False)
    results: dict = {}
    test_harness._run_daemon_tests(lambda name, ok, err=None: results.__setitem__(name, ok), "daemons/bridge/daemon.py")
    return results


DAEMON = """\
import asyncio
from jarvis_command_sdk import IJarvisDaemon

class Bridge(IJarvisDaemon):
    name = "bridge"
    description = "Holds a connection."
    required_secrets = []

    {run}
"""


def test_a_daemon_passes_and_run_is_not_awaited(tmp_path, monkeypatch):
    results = _run(tmp_path, monkeypatch, DAEMON.format(run="async def run(self):\n        await asyncio.Event().wait()"))
    assert results == {"find_class": True, "instantiate": True, "name": True, "description": True,
                       "required_secrets": True, "run": True}


def test_a_daemon_whose_run_is_not_async_fails(tmp_path, monkeypatch):
    results = _run(tmp_path, monkeypatch, DAEMON.format(run="def run(self):\n        return None"))
    assert results["run"] is False


def test_the_daemon_folder_is_inferred(tmp_path, monkeypatch):
    (tmp_path / "daemons" / "bridge").mkdir(parents=True)
    (tmp_path / "daemons" / "bridge" / "daemon.py").write_text("")
    monkeypatch.setattr(test_harness, "_REPO_DIR", str(tmp_path))
    assert {"type": "daemon", "name": "bridge", "path": "daemons/bridge/daemon.py"} in test_harness._infer_components_from_structure()

from unittest import mock

from registry_update_helpers.integration_update_orchestrator import IntegrationUpdateOrchestrator


def test_uv_command_uses_cached_tool_dependencies(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_TEST_UV", "/tools/uv")
    orchestrator = IntegrationUpdateOrchestrator(str(tmp_path))

    assert orchestrator._uv_command() == [
        "/tools/uv",
        "run",
        "--no-project",
        "--with",
        "pyyaml",
        "--with",
        "filelock",
        "--",
        "python",
    ]


def test_run_reuses_uv_tool_environment_for_both_commands(monkeypatch, tmp_path):
    script = tmp_path / IntegrationUpdateOrchestrator.MAIN_UPDATE_SCRIPT
    script.parent.mkdir(parents=True)
    script.touch()
    data = tmp_path / "updates.json"
    data.write_text("{}")
    orchestrator = IntegrationUpdateOrchestrator(str(tmp_path))
    monkeypatch.setattr(orchestrator, "_uv_command", lambda: ["uv", "run", "--", "python"])
    run = mock.Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_run_subprocess", run)

    orchestrator.run(str(data))

    assert run.call_count == 2
    assert run.call_args_list[0].args[0][:4] == ["uv", "run", "--", "python"]
    assert run.call_args_list[1].args[0] == ["uv", "run", "--", "python", str(script)]

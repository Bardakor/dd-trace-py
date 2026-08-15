import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Union


class IntegrationUpdateOrchestrator:
    TOOLING_DEPS = ["pyyaml", "filelock"]
    REGISTRY_UPDATER_MODULE = "registry_update_helpers.integration_registry_updater"
    REGISTRY_UPDATER_CLASS = "IntegrationRegistryUpdater"
    MAIN_UPDATE_SCRIPT = "scripts/integration_registry/update_and_format_registry.py"
    UPDATER_LOCK_FILE = "scripts/integration_registry/registry.yaml.lock"

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.updater_lock_file_path = os.path.join(project_root, self.UPDATER_LOCK_FILE)

    def _uv_command(self) -> list[str]:
        uv = os.environ.get("DD_TEST_UV") or shutil.which("uv")
        if uv is None:
            return []
        command = [uv, "run", "--no-project"]
        for dependency in self.TOOLING_DEPS:
            command.extend(("--with", dependency))
        return command + ["--", "python"]

    def _run_subprocess(self, cmd: list, timeout: int, cwd: str, description: str, verbose: bool = True) -> bool:
        """Helper to run subprocess. Prints stderr on failure by default."""
        try:
            process = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
            if verbose:
                if process.stdout:
                    print(f"\n--- stdout: {description} ---\n{process.stdout.strip()}", file=sys.stdout)
                if process.stderr:
                    print(f"\n--- stderr: {description} ---\n{process.stderr.strip()}", file=sys.stdout)
            return True
        except subprocess.CalledProcessError as e:
            if e.stderr:
                # Only print stderr if it exists since the registry update command returns a non-zero exit code
                # when no changes are needed
                print(f"Error: {description} failed (code {e.returncode}).", file=sys.stderr)
            return False
        except Exception:
            return False

    @staticmethod
    def export_registry_data(data: dict, request) -> Union[str, None]:
        """Exports registry data to a temporary file for pytest worker use."""
        data_file_path = None
        try:
            unique_id = f"pid{os.getpid()}"
            worker_input = getattr(request.config, "workerinput", None)
            if worker_input and "workerid" in worker_input:
                unique_id += f"_{worker_input['workerid']}"
            temp_dir = getattr(request.config, "_tmp_path_factory", None)
            base_dir = temp_dir.getbasetemp() if temp_dir else None
            fd, data_file_path = tempfile.mkstemp(
                prefix=f"registry_data_{unique_id}_", suffix=".json", dir=base_dir, text=True
            )
            with open(fd, "w", encoding="utf-8") as temp_f:
                json.dump(data, temp_f)
            request.config._registry_session_data_file = data_file_path
            return data_file_path
        except Exception:
            if data_file_path and os.path.exists(data_file_path):
                try:
                    os.remove(data_file_path)
                except OSError:
                    pass
            if hasattr(request.config, "_registry_session_data_file"):
                delattr(request.config, "_registry_session_data_file")
            return None

    @staticmethod
    def cleanup_session_data(session):
        data_file_path = getattr(session.config, "_registry_session_data_file", None)
        if data_file_path and os.path.exists(data_file_path):
            try:
                os.remove(data_file_path)
            except OSError:
                pass
        if hasattr(session.config, "_registry_session_data_file"):
            delattr(session.config, "_registry_session_data_file")

    def run(self, data_file_path: str):
        """Main method for orchestrating the integrationregistry update process."""
        updater_succeeded = False

        try:
            # Remove potentially stale updater lock file
            if os.path.exists(self.updater_lock_file_path):
                try:
                    os.remove(self.updater_lock_file_path)
                except OSError:
                    pass

            # Run Update Process
            uv_command = self._uv_command()
            if not uv_command:
                return

            # 1. Run IntegrationRegistryUpdater
            integration_registry_dir = os.path.join(self.project_root, "scripts", "integration_registry")
            escaped_path = data_file_path.replace("'", "'\\''")
            py_cmd = (
                f"import sys; sys.path.insert(0, '{integration_registry_dir}'); "
                f"from {self.REGISTRY_UPDATER_MODULE} import {self.REGISTRY_UPDATER_CLASS}; "
                f"updater = {self.REGISTRY_UPDATER_CLASS}(); success = updater.run('{escaped_path}'); "
                f"sys.exit(0 if success else 1);"
            )
            cmd_updater = [*uv_command, "-c", py_cmd]
            updater_succeeded = self._run_subprocess(
                cmd_updater, 20, self.project_root, self.REGISTRY_UPDATER_CLASS, verbose=False
            )

            # 2. Run Main IntegrationRegistry Update/Format Script if we have changes to the registry
            if updater_succeeded:
                script_path = os.path.join(self.project_root, self.MAIN_UPDATE_SCRIPT)
                if os.path.exists(script_path):
                    cmd_main = [*uv_command, script_path]
                    self._run_subprocess(cmd_main, 20, self.project_root, "Main Update Script", verbose=True)

        finally:
            # Cleanup updater's lock file
            if os.path.exists(self.updater_lock_file_path):
                try:
                    os.remove(self.updater_lock_file_path)
                except OSError:
                    pass

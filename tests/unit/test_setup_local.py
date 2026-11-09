import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows PowerShell virtual-environment test")
class SetupLocalTests(unittest.TestCase):
    def test_recreate_stops_process_using_project_virtual_environment(self):
        source = Path(__file__).parents[2] / "scripts" / "setup-local.ps1"
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            setup = scripts / "setup-local.ps1"
            shutil.copy2(source, setup)
            (project / "requirements.txt").write_text("", encoding="utf-8")

            command = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(setup),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            venv_python = project / ".venv" / "Scripts" / "python.exe"
            using_process = subprocess.Popen(
                [str(venv_python), "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                activated_environment = os.environ.copy()
                activated_environment["PATH"] = (
                    str(venv_python.parent) + os.pathsep + activated_environment["PATH"]
                )
                result = subprocess.run(
                    [*command, "-Recreate"], capture_output=True, text=True, timeout=90,
                    env=activated_environment,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                using_process.wait(timeout=10)
                subprocess.run(
                    [str(venv_python), "-c", "import sys"], check=True, timeout=10,
                )
                configuration = (project / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
                self.assertNotIn("WindowsApps", configuration)
            finally:
                if using_process.poll() is None:
                    using_process.kill()
                    using_process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

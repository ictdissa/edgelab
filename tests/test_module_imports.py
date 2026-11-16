"""Fresh interpreters expose dependency cycles hidden by pytest collection order."""

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(
    {
        ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
        for path in (ROOT / "edgelab").rglob("*.py")
    }
)


@pytest.mark.parametrize("module", MODULES)
def test_module_import_is_independent_of_import_order(module):
    program = (
        "import importlib, socket, sys\n"
        "def blocked(*args, **kwargs):\n"
        "    raise AssertionError('Network prohibited during isolated import test')\n"
        "socket.socket.connect = blocked\n"
        "socket.create_connection = blocked\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "importlib.import_module(sys.argv[2])\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(ROOT), module],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"Isolated import failed for {module}:\n{result.stderr}"

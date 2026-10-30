"""The inline uv fallback in install.sh must match sigmond's ensure_uv.sh:
managed Pythons go to /opt/uv/python, and _ensure_uv creates that dir 0755.
Only the fallback block is evaluated; main never runs.  No sudo is used."""
import os
import stat
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[1] / "scripts/install.sh"


def _fallback_block() -> str:
    lines = INSTALL_SH.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("_ENSURE_UV_SH="))
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    block = "\n".join(lines[start:end + 1])
    # Force the fallback branch: point the helper at a path that is absent.
    return block.replace("/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh",
                         "/nonexistent/ensure_uv.sh", 1)


def _run(script: str, env: dict):
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True)


def _base_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "UV_PYTHON_INSTALL_DIR"}
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "uv"
    stub.write_text("#!/bin/sh\necho 'uv 9.9.9'\n")
    stub.chmod(0o755)
    # Non-root runs reach `sudo install -d`; a stub that just execs its
    # arguments keeps the test off real sudo.
    sudo = stub_dir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    return env


def test_fallback_exports_default_install_dir(tmp_path):
    env = _base_env(tmp_path)
    r = _run(_fallback_block() + '\nprintf "%s" "$UV_PYTHON_INSTALL_DIR"', env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "/opt/uv/python"


def test_fallback_creates_install_dir_0755(tmp_path):
    env = _base_env(tmp_path)
    target = tmp_path / "uvpy" / "python"
    env["UV_PYTHON_INSTALL_DIR"] = str(target)
    assert not target.exists()
    old = os.umask(0o077)  # a restrictive umask must not leak into the mode
    try:
        r = _run(_fallback_block() + "\n_ensure_uv", env)
    finally:
        os.umask(old)
    assert r.returncode == 0, r.stderr
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755

"""One check: a clone missing its pieces must say so, and --fix must seed .env."""
from plexus.sandbox import toolchain


def test_reports_missing_config(tmp_path):
    out = "\n".join(toolchain(str(tmp_path)))
    assert "plexus.toml: missing" in out
    assert "python 3." in out


def test_fix_seeds_env_from_example(tmp_path):
    (tmp_path / ".env.example").write_text("FOO=\n")
    assert ".env: missing" in "\n".join(toolchain(str(tmp_path)))
    toolchain(str(tmp_path), fix=True)
    assert (tmp_path / ".env").read_text() == "FOO=\n"
    assert ".env: ok" in "\n".join(toolchain(str(tmp_path)))

import os
from plexus import cli, observe


def test_default_root_reaches_commands_absolute(tmp_path, monkeypatch):
    # heart mounts <root>/.git into the sandbox; Docker refuses a relative path.
    seen = []
    monkeypatch.setattr(observe, "status", lambda root, **kw: (seen.append(root), ([], 0))[1])
    monkeypatch.chdir(tmp_path)
    cli.main(["status"])
    assert seen == [str(tmp_path)] and os.path.isabs(seen[0])

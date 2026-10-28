"""credentials risk class: a diff that handles secrets is held, whatever the
plan called it and whether or not the capability is new to the file."""
from plexus import review
from plexus.spec import GoalSpec


def _diff(path: str, added: str, removed: str = "") -> str:
    minus = f"-{removed}\n" if removed else ""
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n{minus}+{added}\n")


def test_egress_proxy_path_is_credentials():
    path = "contrib/egress-proxy.py"
    assert review.credentials([path], _diff(path, "x = 1")) == [path]


def test_cert_loading_in_any_file_is_credentials():
    path = "src/app/net.py"
    diff = _diff(path, "ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.load_cert_chain(c)")
    hits = review.credentials([path], diff)
    assert len(hits) == 1 and hits[0].startswith("src/app/net.py: ")


def test_removed_token_line_is_credentials():
    path = "src/app/auth.py"
    assert review.credentials([path], _diff(path, "pass", removed="check(access_token)"))


def test_secrets_dir_is_credentials():
    assert review.credentials(["deploy/secrets/seat.env"], "") == ["deploy/secrets/seat.env"]


def test_docs_only_diff_is_not_credentials():
    path = "docs/setup.md"
    assert review.credentials([path], _diff(path, "Put your access_token in auth.json")) == []


def test_ordinary_code_is_not_credentials():
    path = "src/app/util.py"
    assert review.credentials([path], _diff(path, "return self.key + 1")) == []


def test_default_hold_list_holds_credentials():
    assert "credentials" in GoalSpec.__dataclass_fields__["review_hold"].default
    assert "credentials" in review.CLASSES

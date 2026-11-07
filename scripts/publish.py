"""Audited, source-only publication to the verified private repository."""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jzbot.infra.instance_lock import SingleInstanceLock
from scripts.check import validate

MODULE_ROOT = Path(__file__).resolve().parents[1]
# The mirror may run offline tests, but is never a second development/push root.
ROOT = MODULE_ROOT.parent if MODULE_ROOT.name == ".github-publish" else MODULE_ROOT
REPOSITORY = "zz77vv/jzbot"
REPOSITORY_ID = 1434510965
BRANCH = "main"
REMOTE = "https://github.com/" + REPOSITORY + ".git"
GH = Path(r"D:\Codex\.tools\github-cli\bin\gh.exe")
MIRROR = ROOT / ".github-publish"
TOP = {
    "AGENTS.md",
    "README.md",
    "PROJECT_CONTEXT.md",
    "DEPLOYMENT.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "constraints.txt",
    ".gitignore",
}
SECRET_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
]


class PublishError(RuntimeError):
    pass


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if len(path.parts) == 1:
        return name in TOP
    return (
        path.parts[0] in {"jzbot", "scripts", "tests"}
        and path.suffix == ".py"
        and "__pycache__" not in path.parts
    ) or (len(path.parts) == 2 and path.parts[0] == "docs" and path.suffix == ".md")


def scan(text, name):
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise PublishError("Possible credential in " + name + "; value withheld")
    if name.endswith(".py"):
        for node in ast.walk(ast.parse(text, filename=name)):
            if not isinstance(node, ast.Assign) or not isinstance(
                node.value, ast.Constant
            ):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and re.search(
                        r"(?:token|password|secret|api_key)$", target.id, re.I
                    )
                    and isinstance(node.value.value, str)
                    and len(node.value.value) > 16
                    and node.value.value
                    not in {"<REDACTED>", "development-placeholder"}
                ):
                    raise PublishError(
                        "Literal credential assignment in " + name + "; value withheld"
                    )


def collect(root):
    root = Path(root).resolve(strict=True)
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(
            part
            in {
                ".github-publish",
                ".git",
                ".pytest_tmp",
                ".pytest_cache",
                "__pycache__",
                ".venv",
                "reports",
            }
            for part in relative.parts
        ):
            continue
        name = relative.as_posix()
        if not allowed(name):
            continue
        if path.is_symlink() or not path.is_file():
            if path.is_dir():
                continue
            raise PublishError("Unexpected publishable filesystem entry")
        text = path.read_text(encoding="utf-8")
        scan(text, name)
        files[name] = text
    if (
        not {
            "jzbot/__init__.py",
            "jzbot/app.py",
            "pyproject.toml",
            "README.md",
            "AGENTS.md",
        }
        <= files.keys()
    ):
        raise PublishError("Incomplete V2 source allowlist")
    return files


def fingerprint(files):
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def run(args, cwd=None):
    args = [str(a) for a in args]
    if args[0] == "git":
        args[0] = shutil.which("git") or str(
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe"
        )
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1"),
    )
    if result.returncode:
        raise PublishError(
            Path(args[0]).name + " failed (output withheld); no success claimed"
        )
    return result.stdout


def git(*args):
    return run(["git", "-c", "credential.interactive=false", *args], cwd=MIRROR)


def api(endpoint):
    return json.loads(run([GH, "api", endpoint]))


def verify_repository():
    user = api("user")
    repo = api("repos/" + REPOSITORY)
    if (
        user["login"] != "zz77vv"
        or repo.get("id") != REPOSITORY_ID
        or repo["full_name"] != REPOSITORY
        or repo["owner"]["login"] != "zz77vv"
        or repo["private"] is not True
        or not repo.get("permissions", {}).get("push")
    ):
        raise PublishError("Unexpected repository identity/owner/privacy/writer")
    if repo.get("default_branch") not in (None, BRANCH):
        raise PublishError("Unexpected default branch")
    return user


def audit_index(files):
    names = git("ls-files", "-z").rstrip("\0").split("\0")
    if set(names) != set(files):
        raise PublishError("Index contains missing/unexpected files")
    for name in names:
        text = git("show", ":" + name)
        scan(text, name)
        if text != files[name]:
            raise PublishError("Staged source differs from reviewed mirror")
    git("diff", "--cached", "--check")
    scan(git("diff", "--cached", "--no-ext-diff", "--no-color"), "staged.diff")


def audit_history():
    for sha in git("rev-list", "HEAD").splitlines():
        for name in git("ls-tree", "-r", "--name-only", sha).splitlines():
            if not allowed(name):
                raise PublishError("Unexpected historical file: " + name)
            scan(git("show", sha + ":" + name), name)


def publish(*, create_private=False):
    files = collect(ROOT)
    original = fingerprint(files)
    validate(ROOT)
    if fingerprint(collect(ROOT)) != original:
        raise PublishError("Source changed during tests")
    if create_private:
        raise PublishError("Publication requires the existing repository; creation is disabled")
    user = verify_repository()
    MIRROR.mkdir(exist_ok=True)
    initialized = (MIRROR / ".git").exists()
    if initialized:
        if (
            git("remote", "get-url", "origin").strip() != REMOTE
            or git("branch", "--show-current").strip() != BRANCH
        ):
            raise PublishError("Unexpected mirror remote/branch")
        if git("status", "--porcelain").strip():
            raise PublishError("Unreviewed mirror modifications")
        git("fetch", "origin", BRANCH)
        if (
            git("rev-parse", "HEAD").strip()
            != git("rev-parse", "origin/" + BRANCH).strip()
        ):
            raise PublishError("Local/remote history diverged; stop for review")
        for name in git("ls-files").splitlines():
            if name not in files:
                if not allowed(name):
                    raise PublishError("Unexpected tracked file")
                (MIRROR / name).unlink()
    else:
        refs = run(["git", "-c", "credential.interactive=false", "ls-remote", REMOTE])
        if refs.strip():
            raise PublishError(
                "Existing repository is nonempty; review before initial publication"
            )
        if any(MIRROR.iterdir()):
            raise PublishError("Initial mirror must be empty")
        git("init", "-b", BRANCH)
        git("remote", "add", "origin", REMOTE)
        git("config", "user.name", "zz77vv")
        git(
            "config", "user.email", str(user["id"]) + "+zz77vv@users.noreply.github.com"
        )
        git("config", "core.autocrlf", "false")
        git("config", "credential.helper", "")
        git(
            "config",
            "--add",
            "credential.helper",
            "!" + GH.as_posix() + " auth git-credential",
        )
    for name, text in files.items():
        target = MIRROR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    validate(MIRROR)
    if collect(MIRROR) != files or fingerprint(collect(ROOT)) != original:
        raise PublishError("Source/mirror changed during validation")
    git("add", "--all")
    audit_index(files)
    changes = git("diff", "--cached", "--name-only").strip()
    if changes:
        print("AUDITED_CHANGED_FILES\n" + changes, flush=True)
        git(
            "-c", "core.hooksPath=NUL", "commit", "-m", "Release tested JZBot V2 source"
        )
    audit_history()
    verify_repository()
    if fingerprint(collect(ROOT)) != original:
        raise PublishError("Source changed before push")
    git("push", "--set-upstream", "origin", BRANCH)
    verify_repository()
    sha = git("rev-parse", "HEAD").strip()
    if api("repos/" + REPOSITORY + "/commits/" + BRANCH)["sha"] != sha:
        raise PublishError("Remote SHA mismatch")
    tree = api("repos/" + REPOSITORY + "/git/trees/" + sha + "?recursive=1")
    actual = {v["path"]: v["sha"] for v in tree["tree"] if v["type"] == "blob"}
    expected = {
        name: hashlib.sha1(
            ("blob " + str(len(text.encode())) + "\0").encode() + text.encode()
        ).hexdigest()
        for name, text in files.items()
    }
    if tree.get("truncated") or actual != expected:
        raise PublishError("Remote file contents mismatch")
    state = dict(
        commit=sha,
        source_fingerprint=original,
        private=True,
        synced_at_utc=datetime.now(timezone.utc).isoformat(),
        files=len(files),
    )
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "publish-state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    print("GITHUB_VERIFIED " + json.dumps(state), flush=True)
    return state


if __name__ == "__main__":
    if MODULE_ROOT != ROOT:
        raise PublishError(
            "Run publication from the development source, not its mirror"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-private", action="store_true",
                        help="Deprecated: creation is disabled for the fixed repository identity")
    args = parser.parse_args()
    with SingleInstanceLock(ROOT / ".github-publish.lock"):
        publish(create_private=args.create_private)


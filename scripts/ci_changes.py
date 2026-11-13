import json
import os
from pathlib import Path, PurePosixPath
import subprocess


DEPENDENCIES = {"requirements.txt", "requirements-dev.txt", "requirements-build.txt", "pyproject.toml"}


def classify(paths, event="push", ref="", unknown=False):
    packaging = event == "workflow_dispatch" or ref.startswith("refs/tags/")
    paths = [PurePosixPath(path) for path in paths]
    documentation_only = all(path.suffix.lower() in {".md", ".rst"} or str(path) == "LICENSE" for path in paths)
    return {
        "tests": packaging or unknown or not documentation_only,
        "dependencies": unknown or any(str(path) in DEPENDENCIES for path in paths),
        "packages": packaging,
    }


def main():
    base = os.environ.get("PR_BASE") or os.environ.get("EVENT_BASE")
    if not base or not base.strip("0"):
        previous = subprocess.run(["git", "rev-parse", "--verify", "HEAD^"], capture_output=True, text=True)
        base = previous.stdout.strip() if previous.returncode == 0 else None
    paths = []
    if base:
        comparison = subprocess.run(["git", "diff", "--name-only", "-z", "--no-renames", base, "HEAD", "--"], check=True, capture_output=True)
        paths = [os.fsdecode(path) for path in comparison.stdout.split(b"\0") if path]
        subprocess.run(["git", "diff", "--check", base, "HEAD", "--"], check=True)
    decisions = classify(paths, os.environ.get("GITHUB_EVENT_NAME", "push"), os.environ.get("GITHUB_REF", ""), unknown=base is None)
    print(json.dumps({"changed_paths": paths, **decisions}, indent=2))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in decisions.items():
            output.write(f"{key}={str(value).lower()}\n")


if __name__ == "__main__":
    main()

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ecoflow_monitor import __version__


def git(*arguments):
    return subprocess.run(["git", *arguments], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise ValueError("Repository and GitHub token are required")
        self.base = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, path, method="GET", data=None, missing=False):
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.base + path, data=payload, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"GitHub {method} {path}: HTTP {error.code}") from None

    def assets(self, identity):
        result = []
        page = 1
        while True:
            batch = self.request(f"/releases/{identity}/assets?per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def asset_digest(self, asset):
        digest = asset.get("digest")
        if digest and digest.startswith("sha256:"):
            return digest.removeprefix("sha256:")
        request = Request(self.base + f"/releases/assets/{asset['id']}", headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/octet-stream",
        })
        digest = hashlib.sha256()
        with urlopen(request, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()


def release_files(directory, tag):
    expected = {f"EcoFlowMonitor-{tag}-x64.exe", f"EcoFlowMonitor-{tag}-portable.zip",
                f"EcoFlowMonitor-{tag}-x64-machine-setup.exe", f"GetItSimple.EcoFlowMonitor-{tag}-winget.zip"}
    checksums = {}
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None or match[2] in checksums:
            raise ValueError("Invalid or duplicate release checksum")
        checksums[match[2]] = match[1].lower()
    if set(checksums) != expected:
        raise ValueError("Release asset set does not match the version tag")
    for name, digest in checksums.items():
        if file_digest(directory / name) != digest:
            raise ValueError(f"Release checksum mismatch: {name}")
    checksums["SHA256SUMS.txt"] = file_digest(directory / "SHA256SUMS.txt")
    return checksums


def previous_tag(tag):
    version = tuple(map(int, tag.split(".")))
    tags = [value for value in git("tag", "--merged", tag).splitlines() if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value)]
    candidates = [value for value in tags if tuple(map(int, value.split("."))) < version]
    return max(candidates, key=lambda value: tuple(map(int, value.split(".")))) if candidates else None


def markdown(value):
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", " ".join(value.splitlines()))


def release_notes(api, tag, repository):
    base = previous_tag(tag)
    arguments = {"tag_name": tag}
    if base:
        arguments["previous_tag_name"] = base
    generated = api.request("/releases/generate-notes", "POST", arguments)["body"]
    records = git("log", "--reverse", "--format=%H%x00%s%x00%an", f"{base}..{tag}" if base else tag).splitlines()
    commits, authors = [], set()
    for record in records:
        sha, subject, name = record.split("\0", 2)
        commit = api.request("/commits/" + sha)
        author = commit.get("author")
        author_text = "@" + author["login"] if author is not None else markdown(name)
        authors.add(author_text)
        commits.append(f"- {markdown(subject)} — {author_text} ([{sha[:7]}](https://github.com/{repository}/commit/{sha}))")
    return generated.rstrip() + "\n\n## Commits\n\n" + ("\n".join(commits) or "No new commits.") + "\n\n## Authors\n\n" + ", ".join(sorted(authors, key=str.casefold)) + "\n"


def publish(api, tag, directory, repository, upload=None):
    if tag != __version__:
        raise ValueError("Release tag must match the checked-out application version")
    local_commit = git("rev-parse", tag + "^{commit}")
    if git("rev-parse", "HEAD") != local_commit:
        raise ValueError("Release checkout does not match the tag")
    remote_commit = api.request("/commits/" + quote(tag, safe=""))["sha"]
    if remote_commit != local_commit:
        raise ValueError("Remote tag differs from the verified checkout")
    checksums = release_files(directory, tag)
    release = api.request("/releases/tags/" + quote(tag, safe=""), missing=True)
    if release is not None and release.get("immutable", False):
        raise ValueError("This GitHub Release is immutable and cannot be updated")
    notes = release_notes(api, tag, repository)
    created = release is None
    if created:
        release = api.request("/releases", "POST", {"tag_name": tag, "name": tag, "draft": True, "target_commitish": local_commit})
    assets = {asset["name"]: asset for asset in api.assets(release["id"])}
    paths = [directory / name for name, digest in checksums.items() if name not in assets or api.asset_digest(assets[name]) != digest]
    if paths:
        if upload is None:
            subprocess.run(["gh", "release", "upload", tag, *map(str, paths), "--clobber", "--repo", repository], check=True)
        else:
            upload(paths)
    assets = {asset["name"]: asset for asset in api.assets(release["id"])}
    for name, digest in checksums.items():
        if name not in assets or api.asset_digest(assets[name]) != digest:
            raise ValueError(f"Uploaded release asset verification failed: {name}")
    changes = {"body": notes, "name": tag}
    if created or release.get("draft", False):
        changes["draft"] = False
    api.request(f"/releases/{release['id']}", "PATCH", changes)
    print(f"Release {tag}: assets and generated notes verified and updated")


def main():
    parser = argparse.ArgumentParser(description="Create or update a verified version-tag release")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    arguments = parser.parse_args()
    publish(GitHub(arguments.repository, os.environ.get("GH_TOKEN", "")), arguments.tag, arguments.directory, arguments.repository)


if __name__ == "__main__":
    main()

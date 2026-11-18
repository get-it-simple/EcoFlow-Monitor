from urllib.error import HTTPError

import pytest

from ecoflow_monitor import __version__
from scripts import publish_release as publisher


class GitHubFixture:
    def __init__(self, existing):
        self.release = {"id": 1, "draft": False, "body": "manual text", "immutable": False} if existing else None
        self.files = {}
        self.calls = []

    def request(self, path, method="GET", data=None, missing=False):
        self.calls.append((method, path, data))
        if path.startswith("/commits/"):
            return {"sha": "a" * 40, "author": None}
        if path.startswith("/releases/tags/"):
            return self.release
        if path == "/releases/generate-notes":
            return {"body": "Generated overview"}
        if path == "/releases" and method == "POST":
            self.release = {"id": 1, **data}
            return self.release
        if path == "/releases/1" and method == "PATCH":
            self.release.update(data)
            return self.release
        raise AssertionError((method, path))

    def assets(self, identity):
        return [{"name": name, "digest": digest} for name, digest in self.files.items()]

    def asset_digest(self, asset):
        return asset["digest"]

    def upload(self, paths):
        self.files.update({path.name: publisher.file_digest(path) for path in paths})


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    tag = __version__
    names = [f"EcoFlowMonitor-{tag}-x64.exe", f"EcoFlowMonitor-{tag}-portable.zip",
             f"EcoFlowMonitor-{tag}-x64-machine-setup.exe", f"GetItSimple.EcoFlowMonitor-{tag}-winget.zip"]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "SHA256SUMS.txt").write_text("".join(f"{publisher.file_digest(tmp_path / name)}  {name}\n" for name in names), encoding="utf-8")
    def git(*args):
        if args[0] == "rev-parse":
            return "a" * 40
        if args[0] == "tag":
            return "0.18.2\n" + tag
        if args[0] == "log":
            assert args[-1] == "0.18.2.." + tag
            return "a" * 40 + "\0Improve [monitoring]\0Example Author"
        raise AssertionError(args)
    monkeypatch.setattr(publisher, "git", git)
    return tmp_path


@pytest.mark.parametrize("existing", [False, True])
def test_publish_creates_or_updates_existing_release_and_is_repeatable(prepared, existing):
    api = GitHubFixture(existing)
    publisher.publish(api, __version__, prepared, "example/project", upload=api.upload)
    assert api.release["draft"] is False
    assert "manual text" not in api.release["body"]
    assert "Generated overview" in api.release["body"]
    assert "Example Author" in api.release["body"]
    assert "/commit/" + "a" * 40 in api.release["body"]
    assert "Improve \\[monitoring\\]" in api.release["body"]
    creates = [call for call in api.calls if call[:2] == ("POST", "/releases")]
    assert len(creates) == (0 if existing else 1)
    before = api.release["body"]
    def forbidden(paths):
        pytest.fail("Unchanged assets were uploaded again")
    publisher.publish(api, __version__, prepared, "example/project", upload=forbidden)
    assert api.release["body"] == before and len(api.files) == 5


def test_failed_upload_does_not_replace_existing_notes(prepared):
    api = GitHubFixture(True)
    def fail(paths):
        api.upload(paths[:1])
        raise RuntimeError("Synthetic upload failure")
    with pytest.raises(RuntimeError, match="upload failure"):
        publisher.publish(api, __version__, prepared, "example/project", upload=fail)
    assert api.release["body"] == "manual text"
    publisher.publish(api, __version__, prepared, "example/project", upload=api.upload)
    assert "Generated overview" in api.release["body"]


def test_corrupt_upload_is_detected_before_notes_change(prepared):
    api = GitHubFixture(True)
    def corrupt(paths):
        api.upload(paths)
        api.files["SHA256SUMS.txt"] = "0" * 64
    with pytest.raises(ValueError, match="verification failed"):
        publisher.publish(api, __version__, prepared, "example/project", upload=corrupt)
    assert api.release["body"] == "manual text"


def test_existing_draft_is_published_only_after_assets_are_verified(prepared):
    api = GitHubFixture(True)
    api.release["draft"] = True
    publisher.publish(api, __version__, prepared, "example/project", upload=api.upload)
    assert not api.release["draft"]
    assert len(api.files) == 5


def test_immutable_release_and_corrupt_local_files_do_not_upload(prepared):
    api = GitHubFixture(True)
    api.release["immutable"] = True
    with pytest.raises(ValueError, match="immutable"):
        publisher.publish(api, __version__, prepared, "example/project", upload=api.upload)
    assert not api.files
    api.release["immutable"] = False
    (prepared / f"EcoFlowMonitor-{__version__}-x64.exe").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        publisher.publish(api, __version__, prepared, "example/project", upload=api.upload)
    assert not api.files


@pytest.mark.parametrize("status", [403, 404, 500])
def test_only_explicit_not_found_can_be_treated_as_missing(monkeypatch, status):
    def fail(*args, **kwargs):
        raise HTTPError("https://api.github.com/", status, "synthetic", {}, None)
    monkeypatch.setattr(publisher, publisher.urlopen.__name__, fail)
    api = publisher.GitHub("example/project", "synthetic-token")
    if status == 404:
        assert api.request("/releases/tags/1.0.0", missing=True) is None
    else:
        with pytest.raises(RuntimeError, match=str(status)):
            api.request("/releases/tags/1.0.0", missing=True)

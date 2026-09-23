"""官方源码获取的合成响应测试；不会访问 GitHub。"""
import io
import json
import tarfile
import time
import urllib.error

import pytest

from deploy import github_source as source

A, B, C = "a" * 40, "b" * 40, "c" * 40


class Response(io.BytesIO):
    def __init__(self, status, payload=b"", headers=None):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        super().__init__(payload)
        self.status, self.headers = status, headers or {}


def reference(sha):
    return {"ref": "refs/heads/deploy", "object": {"type": "commit", "sha": sha}}


def ahead(base):
    return {"status": "ahead", "ahead_by": 1, "behind_by": 0,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": base}}


def queue(client, monkeypatch, responses):
    calls = []

    def opened(request, timeout):
        calls.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client.opener, "open", opened)
    return calls


def test_unchanged_reference_uses_validated_conditional_cache(tmp_path, monkeypatch):
    client = source.GitHubSource(tmp_path)
    calls = queue(client, monkeypatch, [Response(200, reference(A), {"ETag": '"a"'}), Response(304)])
    assert client.tip() == A
    assert client.tip() == A
    assert calls[1].get_header("If-none-match") == '"a"'
    assert len(calls) == 2


def test_timeout_never_uses_cached_sha_for_deployment(tmp_path, monkeypatch):
    client = source.GitHubSource(tmp_path)
    queue(client, monkeypatch, [Response(200, reference(A)), urllib.error.URLError("synthetic timeout")])
    assert client.tip() == A
    with pytest.raises(urllib.error.URLError):
        client.tip()


def test_updates_verify_against_last_observed_release_even_after_failed_install(tmp_path, monkeypatch):
    client = source.GitHubSource(tmp_path)
    client.state = {"observed_sha": B, "cached_sha": B}
    calls = queue(client, monkeypatch, [Response(200, reference(C)), Response(200, ahead(B))])
    # 正在运行的仍是 A，B 即使安装失败也不能忽略其发布顺序。
    assert client.tip(current=A) == C
    assert f"/compare/{B}...{C}" in calls[1].full_url
    assert source.GitHubSource(tmp_path).state["observed_sha"] == C


@pytest.mark.parametrize("changed", [
    {"status": "behind"}, {"status": "diverged"}, {"behind_by": 1},
    {"merge_base_commit": {"sha": C}}, {"base_commit": {"sha": C}},
])
def test_backwards_or_unrelated_release_is_rejected(tmp_path, monkeypatch, changed):
    client = source.GitHubSource(tmp_path)
    client.state = {"observed_sha": B, "cached_sha": B}
    queue(client, monkeypatch, [Response(200, reference(A)), Response(200, {**ahead(B), **changed})])
    with pytest.raises(ValueError):
        client.tip(current=B)
    assert client.state["observed_sha"] == B


@pytest.mark.parametrize("payload", [
    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": A}},
    {"ref": "refs/heads/deploy", "object": {"type": "tag", "sha": A}},
    {"ref": "refs/heads/deploy", "object": {"type": "commit", "sha": "main"}},
])
def test_only_exact_commit_reference_is_accepted(tmp_path, monkeypatch, payload):
    client = source.GitHubSource(tmp_path)
    queue(client, monkeypatch, [Response(200, payload)])
    with pytest.raises(ValueError):
        client.tip()
    assert "observed_sha" not in client.state


def test_rate_limit_cooldown_persists_without_busy_retry(tmp_path, monkeypatch):
    client = source.GitHubSource(tmp_path)
    until = int(time.time()) + 600
    calls = queue(client, monkeypatch, [Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(until)})])
    with pytest.raises(ValueError):
        client.tip()
    assert len(calls) == 1
    restored = source.GitHubSource(tmp_path)
    calls = queue(restored, monkeypatch, [])
    with pytest.raises(ValueError):
        restored.tip()
    assert calls == []
    assert restored.state["not_before"] >= until


@pytest.mark.parametrize("location", [
    f"http://codeload.github.com/DanielGao9527/fitness-agent/legacy.tar.gz/{A}",
    f"https://example.com/DanielGao9527/fitness-agent/legacy.tar.gz/{A}",
    f"https://codeload.github.com/other/fitness-agent/legacy.tar.gz/{A}",
    f"https://codeload.github.com/DanielGao9527/fitness-agent/legacy.tar.gz/{B}",
    f"https://user@codeload.github.com/DanielGao9527/fitness-agent/legacy.tar.gz/{A}",
])
def test_archive_redirect_cannot_change_host_repository_or_commit(tmp_path, monkeypatch, location):
    client = source.GitHubSource(tmp_path)
    calls = queue(client, monkeypatch, [Response(302, headers={"Location": location})])
    with pytest.raises(ValueError):
        client.download_archive(A, tmp_path / "archive.tar.gz")
    assert len(calls) == 1
    assert not (tmp_path / "archive.tar.gz").exists()


def test_archive_download_has_a_size_limit(tmp_path, monkeypatch):
    client = source.GitHubSource(tmp_path)
    queue(client, monkeypatch, [Response(302, headers={"Location": f"https://codeload.github.com/DanielGao9527/fitness-agent/legacy.tar.gz/{A}"}), Response(200, b"too large")])
    monkeypatch.setattr(source, "MAX_ARCHIVE", 2)
    with pytest.raises(ValueError):
        client.download_archive(A, tmp_path / "archive.tar.gz")


def make_tar(path, names):
    with tarfile.open(path, "w") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = 7
            archive.addfile(member, io.BytesIO(b"example"))


def test_archive_strips_exactly_one_root_directory(tmp_path):
    archive = tmp_path / "source.tar"
    make_tar(archive, ["owner-repository-sha/app.py", "owner-repository-sha/static/index.html"])
    target = tmp_path / "release"
    source.extract_archive(archive, target)
    assert (target / "app.py").read_bytes() == b"example"
    assert (target / "static/index.html").exists()
    assert not (target / "owner-repository-sha").exists()


@pytest.mark.parametrize("names", [["first/app.py", "second/file.py"], ["first/app.py", "first/app.py"]])
def test_mixed_roots_and_duplicate_paths_fail_before_writing(tmp_path, names):
    archive = tmp_path / "source.tar"
    make_tar(archive, names)
    target = tmp_path / "release"
    with pytest.raises(ValueError):
        source.extract_archive(archive, target)
    assert not target.exists()

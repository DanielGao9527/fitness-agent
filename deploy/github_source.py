"""仅通过 GitHub 官方 HTTPS API 获取固定仓库的发布提交与源码归档。"""
import argparse
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

API = "https://api.github.com/repos/DanielGao9527/fitness-agent"
API_VERSION = "2022-11-28"
MAX_JSON = 8 * 1024 * 1024
MAX_ARCHIVE = 25 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_FILES = 10000


def valid_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("A full lowercase 40-character commit SHA is required")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


def extract_archive(archive, destination):
    """GitHub 归档有一层根目录；完整校验后再写入全新的目标目录。"""
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Archive destination must be a new empty directory")
    with tarfile.open(archive) as contents:
        members, root, expanded, names = [], None, 0, set()
        for member in contents:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Release contains an unsafe path or link")
            root = root or path.parts[0]
            if path.parts[0] != root or (len(path.parts) == 1 and not member.isdir()):
                raise ValueError("Archive must contain one top-level directory")
            relative = PurePosixPath(*path.parts[1:])
            if relative in names:
                raise ValueError("Duplicate archive path")
            names.add(relative)
            if member.size < 0:
                raise ValueError("Invalid archive member size")
            expanded += member.size
            members.append((member, relative))
            if len(members) > MAX_FILES or expanded > MAX_EXPANDED:
                raise ValueError("Release archive is too large")
        if not members or not any(member.isfile() for member, _ in members):
            raise ValueError("Empty release archive")
        for member, relative in members:
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with contents.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


class GitHubSource:
    def __init__(self, state_directory):
        self.directory = Path(state_directory)
        self.path = self.directory / "github-state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.state, dict):
            raise ValueError("Invalid release state")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def save(self):
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(self.state, output)
        os.replace(temporary, self.path)

    def request(self, url, *, headers=None, api=True):
        if api and time.time() < self.state.get("not_before", 0):
            raise ValueError("GitHub requests paused until the server rate-limit window resets")
        selected = {"User-Agent": "fitness-agent-deploy", "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION, **(headers or {})}
        request = urllib.request.Request(url, headers=selected)
        try:
            response = self.opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        if api:
            self.note_limit(response.status, response.headers)
        return response

    def note_limit(self, status, headers):
        now = time.time()
        until = 0
        if headers.get("X-RateLimit-Remaining") == "0":
            until = max(now + 60, float(headers.get("X-RateLimit-Reset", now + 3600)) + 1)
        if status in (403, 429):
            count = min(int(self.state.get("rate_failures", 0)) + 1, 5)
            self.state["rate_failures"] = count
            until = max(until, now + float(headers.get("Retry-After", min(300 * 2 ** (count - 1), 3600))))
        elif status == 404:
            # 未创建 deploy 分支时不每五分钟重复请求不存在的资源。
            until = max(until, now + 3600)
        elif status in (200, 302, 304):
            self.state["rate_failures"] = 0
        if until:
            self.state["not_before"] = until
        self.save()

    def json(self, route, *, headers=None):
        with self.request(API + route, headers=headers) as response:
            if response.status == 304:
                return None, response.headers
            if response.status != 200:
                raise ValueError("GitHub API request was not successful")
            body = response.read(MAX_JSON + 1)
            if len(body) > MAX_JSON:
                raise ValueError("GitHub response is too large")
            return json.loads(body), response.headers

    def require_descendant(self, previous, selected):
        valid_sha(previous)
        valid_sha(selected)
        if previous == selected:
            return
        result, _ = self.json(f"/compare/{previous}...{selected}?per_page=1")
        if (not isinstance(result, dict) or result.get("status") != "ahead" or result.get("behind_by") != 0
                or result.get("ahead_by", 0) < 1
                or result.get("base_commit", {}).get("sha") != previous
                or result.get("merge_base_commit", {}).get("sha") != previous):
            raise ValueError("Release branch moved backwards or diverged")

    def tip(self, current=None):
        headers = {"Cache-Control": "no-cache"}
        if self.state.get("etag"):
            headers["If-None-Match"] = self.state["etag"]
        result, response_headers = self.json("/git/ref/heads/deploy", headers=headers)
        if result is None:
            selected = valid_sha(self.state.get("cached_sha"))
        else:
            if (not isinstance(result, dict) or result.get("ref") != "refs/heads/deploy"
                    or result.get("object", {}).get("type") != "commit"):
                raise ValueError("Unexpected release reference")
            selected = valid_sha(result["object"]["sha"])
        # observed_sha 独立于运行中的 current；失败发布后也禁止倒退发布分支。
        observed = self.state.get("observed_sha") or current
        if observed:
            self.require_descendant(observed, selected)
        self.state.update(observed_sha=selected, cached_sha=selected)
        if result is not None:
            self.state["etag"] = response_headers.get("ETag", "")
        self.save()
        return selected

    def download_archive(self, sha, destination):
        valid_sha(sha)
        with self.request(API + "/tarball/" + sha) as response:
            if response.status != 302:
                raise ValueError("Expected the official archive redirect")
            location = response.headers.get("Location", "")
        parsed = urlsplit(location)
        if (parsed.scheme != "https" or parsed.hostname != "codeload.github.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment
                or parsed.path != f"/DanielGao9527/fitness-agent/legacy.tar.gz/{sha}"):
            raise ValueError("Archive redirect is outside the expected official source")
        with self.request(location, api=False) as response, Path(destination).open("xb") as output:
            if response.status != 200:
                raise ValueError("Archive download failed")
            total, started = 0, time.monotonic()
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE or time.monotonic() - started > 180:
                    raise ValueError("Archive exceeded download limits")
                output.write(chunk)
            if total == 0:
                raise ValueError("Empty archive response")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="首次安装：下载已人工核对的完整提交，不启动应用")
    parser.add_argument("sha")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    valid_sha(args.sha)
    args.destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="fitness-bootstrap-") as directory:
        archive = Path(directory) / "release.tar.gz"
        GitHubSource(directory).download_archive(args.sha, archive)
        extract_archive(archive, args.destination)
    print("Fixed-commit source archive extracted. Review deploy/install.sh before initialization.")

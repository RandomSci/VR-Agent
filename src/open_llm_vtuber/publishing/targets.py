"""Where a published bundle goes: GitHub, or a local dry-run folder.

Both expose the same three operations, so the publishing service does not
care which one it talks to:

    read(path)            current bytes of a repo file, or None
    has_same(path, data)  True when the file is already there unchanged
    commit(files, msg)    write every file in one commit

GitHubTarget uses the Git Data API: one commit per publish, unchanged files
(the libraries after the first game) are skipped, and the branch moves with
a normal fast-forward (never a force push). The token is only ever placed in
the Authorization header and is scrubbed from every error message.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Optional

import httpx

API = "https://api.github.com"


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class PublishTargetError(RuntimeError):
    pass


class DryRunTarget:
    """A local mirror of the games repository. Nothing leaves the machine."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.commits: list[str] = []

    def _path(self, repo_path: str) -> Path:
        path = (self.folder / repo_path).resolve()
        if self.folder.resolve() not in path.parents:
            raise PublishTargetError(
                f"refusing to write outside the dry-run folder: {repo_path}"
            )
        return path

    def read(self, repo_path: str) -> Optional[bytes]:
        path = self._path(repo_path)
        return path.read_bytes() if path.is_file() else None

    def has_same(self, repo_path: str, data: bytes) -> bool:
        return self.read(repo_path) == data

    def list_paths(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix else self.folder
        if not base.exists():
            return []
        root = self.folder.resolve()
        return sorted(
            str(p.resolve().relative_to(root)) for p in base.rglob("*") if p.is_file()
        )

    def commit(
        self, files: dict[str, bytes], message: str, delete: tuple[str, ...] = ()
    ) -> None:
        for repo_path, data in files.items():
            path = self._path(repo_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for repo_path in delete:
            path = self._path(repo_path)
            if path.is_file():
                path.unlink()
        self.commits.append(message)


class GitHubTarget:
    def __init__(
        self, repo: str, branch: str, token: str, client: Optional[httpx.Client] = None
    ):
        if repo.count("/") != 1:
            raise PublishTargetError("GITHUB_PUBLISH_REPO must look like owner/name")
        self.repo = repo
        self.branch = branch or "main"
        self._token = token
        self._client = client or httpx.Client(timeout=30)
        self._tree: Optional[dict[str, str]] = (
            None  # path -> blob sha at the branch head
        )
        self._head: Optional[str] = None
        self._base_tree: Optional[str] = None

    # -- http ------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs):
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.request(
                method, API + path, headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise PublishTargetError(
                self._scrub(f"GitHub unreachable: {exc}")
            ) from None
        if response.status_code >= 400:
            raise PublishTargetError(
                self._scrub(
                    f"GitHub {method} {path} failed: {response.status_code} {response.text[:300]}"
                )
            )
        return response.json() if response.content else {}

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # -- state -------------------------------------------------------------
    def _load(self) -> None:
        if self._tree is not None:
            return
        ref = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        self._head = ref["object"]["sha"]
        commit = self._request("GET", f"/repos/{self.repo}/git/commits/{self._head}")
        self._base_tree = commit["tree"]["sha"]
        tree = self._request(
            "GET",
            f"/repos/{self.repo}/git/trees/{self._base_tree}",
            params={"recursive": "1"},
        )
        self._tree = {
            item["path"]: item["sha"]
            for item in tree.get("tree", [])
            if item.get("type") == "blob"
        }

    def read(self, repo_path: str) -> Optional[bytes]:
        self._load()
        sha = self._tree.get(repo_path)
        if not sha:
            return None
        blob = self._request("GET", f"/repos/{self.repo}/git/blobs/{sha}")
        return base64.b64decode(blob.get("content", ""))

    def has_same(self, repo_path: str, data: bytes) -> bool:
        self._load()
        return self._tree.get(repo_path) == git_blob_sha(data)

    def list_paths(self, prefix: str) -> list[str]:
        self._load()
        return sorted(p for p in self._tree if p.startswith(prefix))

    def commit(
        self, files: dict[str, bytes], message: str, delete: tuple[str, ...] = ()
    ) -> None:
        self._load()
        entries = []
        for repo_path in delete:
            if repo_path in self._tree and repo_path not in files:
                # A null sha removes the file from the new tree.
                entries.append(
                    {"path": repo_path, "mode": "100644", "type": "blob", "sha": None}
                )
        for repo_path, data in files.items():
            blob = self._request(
                "POST",
                f"/repos/{self.repo}/git/blobs",
                json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
            )
            entries.append(
                {
                    "path": repo_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree = self._request(
            "POST",
            f"/repos/{self.repo}/git/trees",
            json={"base_tree": self._base_tree, "tree": entries},
        )
        commit = self._request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [self._head]},
        )
        # A normal fast-forward: if someone else pushed meanwhile, this fails
        # instead of overwriting their work.
        self._request(
            "PATCH",
            f"/repos/{self.repo}/git/refs/heads/{self.branch}",
            json={"sha": commit["sha"], "force": False},
        )
        self._tree = None  # re-read on the next publish

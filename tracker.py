#!/usr/bin/env python3
"""Watch GitHub repositories and post new commits to a Discord webhook.

Designed to run repeatedly from a GitHub Action. Repositories are declared in
repos.yml; the last seen commit per repository/branch is kept in state.json so
that each run only reports what is new.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from typing import Any, Final, Literal, NotRequired, TypedDict, cast

import yaml

JSONObject = dict[str, Any]

GITHUB_API: Final = "https://api.github.com"
USER_AGENT: Final = "commit-webhook-tracker"

PER_PAGE: Final = 100
MAX_PAGES: Final = 5            # hard ceiling on how far back we walk per run
MAX_COMMITS_PER_REPO: Final = 100   # ~4h of commits on a busy repo
LINES_PER_EMBED: Final = 10
EMBEDS_PER_MESSAGE: Final = 10
MESSAGE_CHAR_BUDGET: Final = 5500   # Discord counts all embed text in a message
NETWORK_ERROR: Final = 599          # synthetic status so retry loops engage

log: Final = logging.getLogger("tracker")


# --------------------------------------------------------------------------- #
# github payload shapes (only the fields we actually read)
# --------------------------------------------------------------------------- #

class GitUser(TypedDict, total=False):
    name: str
    email: str
    date: str


class CommitDetail(TypedDict, total=False):
    message: str
    author: GitUser


class Commit(TypedDict, total=False):
    sha: str
    html_url: str
    commit: CommitDetail


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True, slots=True)
class RepoConfig:
    """One tracked repository/branch pair."""

    repo: str
    branch: str | None = None
    webhook_env: str | None = None
    label: str | None = None
    notify_on_first_run: bool = False

    def __post_init__(self) -> None:
        if self.repo.count("/") != 1 or not all(self.repo.split("/")):
            raise ValueError(f"repo must look like 'owner/name', got {self.repo!r}")

    @property
    def key(self) -> str:
        return f"{self.repo}@{self.branch or '<default>'}"

    @property
    def display(self) -> str:
        return self.label or self.repo


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    repos: tuple[RepoConfig, ...]
    webhook_env: str = "DISCORD_WEBHOOK_URL"
    username: str | None = None
    avatar_url: str | None = None


def _opt_str(value: object) -> str | None:
    """Coerce a YAML scalar to a trimmed string, treating blanks as absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_repo_entry(entry: object, path: str, default_first_run: bool) -> RepoConfig:
    if isinstance(entry, str):
        name, _, branch = entry.partition("@")
        return RepoConfig(name.strip(), branch.strip() or None,
                          notify_on_first_run=default_first_run)

    if isinstance(entry, dict):
        raw = cast(JSONObject, entry)
        name = _opt_str(raw.get("repo") or raw.get("name")) or ""
        if not name:
            raise ValueError(f"{path}: repository entry missing 'repo': {entry!r}")
        ref: str | None = _opt_str(raw.get("branch"))
        if ref is None and "@" in name:
            name, _, ref = name.partition("@")
        return RepoConfig(
            repo=name,
            branch=ref or None,
            webhook_env=_opt_str(raw.get("webhook_env")),
            label=_opt_str(raw.get("label")),
            notify_on_first_run=bool(raw.get("notify_on_first_run", default_first_run)),
        )

    raise ValueError(f"{path}: unsupported repository entry: {entry!r}")


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        loaded: object = yaml.safe_load(fh) or {}

    if isinstance(loaded, list):        # bare list of repos, no defaults block
        loaded = {"repositories": loaded}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a mapping or a list at the top level")

    raw = cast(JSONObject, loaded)
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: 'defaults' must be a mapping")

    entries = raw.get("repositories")
    if entries is None:
        raise ValueError(f"{path}: no 'repositories' key")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'repositories' must be a list")

    default_first_run = bool(defaults.get("notify_on_first_run", False))
    repos = tuple(_parse_repo_entry(e, path, default_first_run)
                  for e in cast(list[object], entries))
    if not repos:
        raise ValueError(f"{path}: no repositories configured")

    seen: set[str] = set()
    for repo in repos:
        if repo.key in seen:
            raise ValueError(f"{path}: duplicate entry for {repo.key}")
        seen.add(repo.key)

    return Config(
        repos=repos,
        webhook_env=_opt_str(defaults.get("webhook_env")) or "DISCORD_WEBHOOK_URL",
        username=_opt_str(defaults.get("username")),
        avatar_url=_opt_str(defaults.get("avatar_url")),
    )


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

class RepoState(TypedDict):
    last_sha: NotRequired[str | None]
    branch: NotRequired[str]
    etag: NotRequired[str | None]
    last_checked: NotRequired[str]


class State(TypedDict):
    repos: dict[str, RepoState]
    updated_at: NotRequired[str]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_state(path: str) -> State:
    empty: State = {"repos": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, encoding="utf-8") as fh:
            loaded: object = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("could not read state file %s (%s); starting fresh", path, exc)
        return empty
    if not isinstance(loaded, dict) or not isinstance(loaded.get("repos"), dict):
        log.warning("state file %s has an unexpected shape; starting fresh", path)
        return empty
    return cast(State, loaded)


def save_state(path: str, state: State) -> None:
    state["updated_at"] = utcnow()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def prune_state(state: State, repos: Iterable[RepoConfig]) -> None:
    live = {r.key for r in repos}
    for key in list(state["repos"]):
        if key not in live:
            log.info("dropping state for untracked %s", key)
            del state["repos"][key]


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True, slots=True)
class HTTPResult:
    status: int
    body: Any
    headers: dict[str, str]

    def json_object(self) -> JSONObject:
        return self.body if isinstance(self.body, dict) else {}

    def json_list(self) -> list[Any]:
        return self.body if isinstance(self.body, list) else []

    @property
    def error_message(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("message") or self.body)
        return str(self.body)[:200]


def request_json(url: str, *, method: Literal["GET", "POST"] = "GET",
                 token: str | None = None, payload: JSONObject | None = None,
                 extra_headers: dict[str, str] | None = None,
                 timeout: int = 30) -> HTTPResult:
    data: bytes | None = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return HTTPResult(resp.status, _maybe_json(raw), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return HTTPResult(exc.code, _maybe_json(raw), dict(exc.headers or {}))
    except (OSError, http.client.HTTPException) as exc:
        # DNS blips, resets, timeouts: report as a 5xx so callers retry
        return HTTPResult(NETWORK_ERROR, {"message": f"network error: {exc}"}, {})


def _maybe_json(raw: str) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #

class RateLimited(Exception):
    """The token's REST quota is gone; nothing useful will happen this run."""


def github_get(path: str, token: str | None, params: dict[str, str] | None = None,
               etag: str | None = None) -> HTTPResult:
    url = GITHUB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-None-Match"] = etag

    result = HTTPResult(0, None, {})
    for attempt in range(3):
        result = request_json(url, token=token, extra_headers=headers)
        if result.status in (200, 304, 404, 409):
            return result
        if result.status in (403, 429):
            if result.headers.get("X-RateLimit-Remaining") == "0":
                reset = result.headers.get("X-RateLimit-Reset", "?")
                raise RateLimited(f"GitHub rate limit exhausted (resets at {reset})")
            wait = int(result.headers.get("Retry-After") or 2 ** attempt)
            log.warning("GitHub returned %s for %s; retrying in %ss",
                        result.status, path, wait)
            time.sleep(min(wait, 30))
            continue
        if 500 <= result.status < 600 and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        return result
    return result


@dataclasses.dataclass(frozen=True, slots=True)
class CommitPage:
    commits: list[Commit]       # newest first
    etag: str | None
    truncated: bool             # never reached last_sha: force push or a big push


def fetch_new_commits(cfg: RepoConfig, token: str | None, last_sha: str | None,
                      etag: str | None) -> CommitPage:
    collected: list[Commit] = []
    new_etag: str | None = None
    found = False

    for page in range(1, MAX_PAGES + 1):
        params = {"per_page": str(PER_PAGE), "page": str(page)}
        if cfg.branch:
            params["sha"] = cfg.branch
        result = github_get(f"/repos/{cfg.repo}/commits", token, params,
                            etag=etag if page == 1 else None)

        if result.status == 304:
            log.info("%s: unchanged (304)", cfg.key)
            return CommitPage([], etag, False)
        if result.status == 404:
            raise RuntimeError("repository or branch not found (check the token's access)")
        if result.status == 409:
            log.info("%s: empty repository", cfg.key)
            return CommitPage([], None, False)
        if result.status != 200:
            raise RuntimeError(f"GitHub API {result.status}: {result.error_message}")

        if page == 1:
            new_etag = result.headers.get("ETag")

        commits = cast(list[Commit], result.json_list())
        if not commits:
            break

        for commit in commits:
            if commit.get("sha") == last_sha:
                found = True
                break
            collected.append(commit)

        if found or len(commits) < PER_PAGE or len(collected) >= MAX_COMMITS_PER_REPO:
            break

    truncated = last_sha is not None and not found
    if len(collected) > MAX_COMMITS_PER_REPO:
        collected = collected[:MAX_COMMITS_PER_REPO]
        truncated = True
    return CommitPage(collected, new_etag, truncated)


def default_branch(cfg: RepoConfig, token: str | None) -> str | None:
    """The repo's default branch, or None if the lookup failed (do not cache)."""
    result = github_get(f"/repos/{cfg.repo}", token)
    if result.status == 200:
        branch = result.json_object().get("default_branch")
        if isinstance(branch, str) and branch:
            return branch
    log.warning("%s: could not resolve the default branch name", cfg.key)
    return None


# --------------------------------------------------------------------------- #
# discord
# --------------------------------------------------------------------------- #

def repo_color(name: str) -> int:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return (digest[0] << 16) | (digest[1] << 8) | digest[2]


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _escape(text: str) -> str:
    for ch in ("\\", "*", "_", "~", "`", "|", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


def commit_line(commit: Commit) -> str:
    sha = commit.get("sha", "")
    url = commit.get("html_url", "")
    detail = commit.get("commit") or {}
    lines = (detail.get("message") or "").splitlines()
    subject = _escape(_short(lines[0] if lines else "(no message)", 80))
    return f"[`{sha[:7]}`]({url}) {subject}"


def embed_length(embed: JSONObject) -> int:
    """Characters Discord counts against the 6000-per-message embed budget."""
    footer = cast(JSONObject, embed.get("footer") or {})
    author = cast(JSONObject, embed.get("author") or {})
    return (len(str(embed.get("title", "")))
            + len(str(embed.get("description", "")))
            + len(str(footer.get("text", "")))
            + len(str(author.get("name", ""))))


def build_messages(cfg: RepoConfig, commits: Sequence[Commit], branch: str,
                   truncated: bool, conf: Config) -> list[JSONObject]:
    """Chunk commits (newest-first in, oldest-first out) into webhook payloads."""
    ordered = list(reversed(commits))
    branch_url = f"https://github.com/{cfg.repo}/commits/{urllib.parse.quote(branch)}"
    count = len(ordered)
    title = f"[{cfg.display}:{branch}] {count} new commit{'' if count == 1 else 's'}"

    embeds: list[JSONObject] = []
    for start in range(0, count, LINES_PER_EMBED):
        chunk = ordered[start:start + LINES_PER_EMBED]
        embed: JSONObject = {
            "color": repo_color(cfg.repo),
            "description": "\n".join(commit_line(c) for c in chunk),
        }
        if start == 0:
            # Discord merges embeds that share a url, so only the first gets one
            embed["url"] = branch_url
            embed["title"] = title
            embed["author"] = {"name": cfg.repo,
                               "url": f"https://github.com/{cfg.repo}"}
            if truncated:
                embed["footer"] = {
                    "text": "older commits skipped (branch moved or too many commits)"
                }
        timestamp = ((chunk[-1].get("commit") or {}).get("author") or {}).get("date")
        if timestamp:
            embed["timestamp"] = timestamp
        embeds.append(embed)

    messages: list[JSONObject] = []
    batch: list[JSONObject] = []
    batch_chars = 0

    def flush() -> None:
        if not batch:
            return
        payload: JSONObject = {"embeds": list(batch)}
        if conf.username:
            payload["username"] = conf.username
        if conf.avatar_url:
            payload["avatar_url"] = conf.avatar_url
        messages.append(payload)
        batch.clear()

    for embed in embeds:
        size = embed_length(embed)
        if batch and (len(batch) >= EMBEDS_PER_MESSAGE
                      or batch_chars + size > MESSAGE_CHAR_BUDGET):
            flush()
            batch_chars = 0
        batch.append(embed)
        batch_chars += size
    flush()
    return messages


def post_discord(webhook_url: str, payload: JSONObject, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[dry-run] would POST:\n%s", json.dumps(payload, indent=2))
        return

    for attempt in range(4):
        result = request_json(webhook_url, method="POST", payload=payload)
        if 200 <= result.status < 300:
            return
        if result.status == 429:
            retry_after = float(result.json_object().get("retry_after") or 1.0)
            log.warning("Discord rate limited; sleeping %.1fs", retry_after)
            time.sleep(min(retry_after + 0.25, 30))
            continue
        if result.status >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Discord webhook {result.status}: {result.error_message}")
    raise RuntimeError("Discord webhook failed after retries")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def resolve_webhook(cfg: RepoConfig, default_env: str) -> str:
    env_name = cfg.webhook_env or default_env
    url = os.environ.get(env_name, "").strip()
    if not url:
        raise RuntimeError(f"environment variable {env_name} is empty; "
                           "set it to a Discord webhook URL")
    return url


def _new_entry(head_sha: str, branch: str | None, etag: str | None) -> RepoState:
    entry: RepoState = {"last_sha": head_sha, "etag": etag, "last_checked": utcnow()}
    if branch:      # never cache a failed default-branch lookup
        entry["branch"] = branch
    return entry


def process_repo(cfg: RepoConfig, conf: Config, state: State,
                 token: str | None, dry_run: bool) -> int:
    entry: RepoState = state["repos"].get(cfg.key, {})
    last_sha = entry.get("last_sha")
    page = fetch_new_commits(cfg, token, last_sha, entry.get("etag"))

    if not page.commits:
        state["repos"][cfg.key] = {**entry, "last_checked": utcnow(),
                                   "etag": page.etag or entry.get("etag")}
        log.info("%s: no new commits", cfg.key)
        return 0

    head_sha = page.commits[0].get("sha", "")
    branch = cfg.branch or entry.get("branch") or default_branch(cfg, token)
    first_run = last_sha is None

    if first_run and not cfg.notify_on_first_run:
        log.info("%s: first run, baselining at %s (%d commit(s) not announced)",
                 cfg.key, head_sha[:7], len(page.commits))
        state["repos"][cfg.key] = _new_entry(head_sha, branch, page.etag)
        return 0

    webhook_url = resolve_webhook(cfg, conf.webhook_env)
    # a first run that overflowed the page cap is exactly when the footer matters
    truncated = page.truncated or (first_run and len(page.commits) >= MAX_COMMITS_PER_REPO)
    messages = build_messages(cfg, page.commits, branch or "HEAD", truncated, conf)
    for index, payload in enumerate(messages):
        post_discord(webhook_url, payload, dry_run)
        if index + 1 < len(messages):
            time.sleep(0.75)

    log.info("%s: announced %d new commit(s) up to %s",
             cfg.key, len(page.commits), head_sha[:7])
    if not dry_run:
        # only advance after a successful post, so a webhook outage retries later
        state["repos"][cfg.key] = _new_entry(head_sha, branch, page.etag)
    return len(page.commits)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="repos.yml",
                        help="repository list (default: repos.yml)")
    parser.add_argument("-s", "--state", default="state.json",
                        help="state file (default: state.json)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="print payloads instead of posting, and leave state alone")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    try:
        conf = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.error("config error: %s", exc)
        return 2

    state = load_state(args.state)
    prune_state(state, conf.repos)
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    if not token:
        log.warning("no GH_TOKEN/GITHUB_TOKEN set; "
                    "using the unauthenticated API (60 requests/hour)")

    announced = 0
    failures = 0
    for repo_cfg in conf.repos:
        try:
            announced += process_repo(repo_cfg, conf, state, token, args.dry_run)
        except RateLimited as exc:
            log.error("%s: %s - stopping early", repo_cfg.key, exc)
            failures += 1
            break
        except Exception as exc:  # one bad repo shouldn't block the rest
            log.error("%s: %s", repo_cfg.key, exc)
            failures += 1

    if not args.dry_run:
        save_state(args.state, state)

    log.info("done: %d commit(s) announced, %d repo(s) failed", announced, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

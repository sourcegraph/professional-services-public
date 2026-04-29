#!/usr/bin/env python3
"""List all repositories on a Sourcegraph instance via the GraphQL API,
and outputs CSV files with the list of repos and metadata.

Only Python's standard library is used. No additional packages required.

What this script does:
  1. Get SRC_ENDPOINT and SRC_ACCESS_TOKEN from environment variables, falling
     back to a `.env` file in the working directory
  2. Verify connectivity to the instance's GraphQL API
  3. Get repo metadata from the GraphQL API, PAGE_SIZE repos at a time
  4. For each repo, flatten a handful of nested fields into a CSV row,
     using the COLUMNS table for both the header and the row data
  5. Stream the rows straight to disk so memory stays flat regardless of
     how many repositories the instance has

Usage:
  export SRC_ENDPOINT="https://sourcegraph.example.com"
  export SRC_ACCESS_TOKEN="sgp_..."
  # ...or place those in a `.env` file in the working directory

  python3 list-repos.py
  python3 list-repos.py --limit 100     # fetch only 100 repos
  python3 list-repos.py -h              # print helper text

Versions:
- Minimum supported version of Python is 3.10
- Minimum supported version of Sourcegraph is v5.2.0

To download the GraphQL schema from your SG instance, to make changes to this script,
run a command like the following to get it from the introspection service:
npx -y get-graphql-schema -h "Authorization=token $SRC_ACCESS_TOKEN" "$SRC_ENDPOINT/.api/graphql" > schema.graphql
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import csv
import http.client
import json
import logging
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO, cast
from urllib.parse import ParseResult, urlparse, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

# --- Tune-ables -----------------------------------------------------------------

PAGE_SIZE = 500
# When --count-commits is set we ask Sourcegraph to compute a non-paginated
# ancestors.totalCount per repo. That is far more expensive server-side than
# the rest of the listing query, so we use a much smaller page size to keep
# each request well under the gateway timeout.
PAGE_SIZE_WITH_COMMIT_COUNT = 25
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 60
# Counting commits server-side can be slow on big monorepos. Bump the per-
# request timeout so we don't fail on long-but-still-progressing requests.
REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT = 600
DEFAULT_OUTPUT_FILE = "repos.csv"
DEFAULT_CLONING_ERRORS_FILE = "repos-with-cloning-errors.csv"
DEFAULT_INDEXING_ERRORS_FILE = "repos-with-indexing-errors.csv"
DEFAULT_SKIPPED_FILES_FILE = "repos-with-skipped-files.csv"
DEFAULT_LOG_FILE = "repos.log"

# --- GraphQL queries ----------------------------------------------------------

# The optional defaultBranch sub-tree is opt-in via --count-commits because
# ancestors.totalCount makes Sourcegraph walk every commit on the default
# branch (no `first:` argument, otherwise the field is null per the schema).
# That is expensive on big monorepos, so we only ask for it when explicitly
# requested.
COMMIT_COUNT_FRAGMENT = """
      defaultBranch {
        target {
          commit {
            ancestors {
              totalCount
            }
          }
        }
      }"""

GRAPHQL_QUERY_TEMPLATE = """
query ListRepos($first: Int!, $after: String) {
  repositories(first: $first, after: $after) {
    nodes {
      id
      url
      createdAt
      isFork
      isArchived
      isPrivate__COMMIT_COUNT_FRAGMENT__
      mirrorInfo {
        remoteURL
        cloned
        cloneInProgress
        isCorrupted
        lastError
        lastSyncOutput
        corruptionLogs {
          timestamp
          reason
        }
        byteSize
        lastChanged
        updatedAt
        nextSyncAt
        updateSchedule {
          intervalSeconds
        }
        shard
      }
      textSearchIndex {
        status {
          updatedAt
          contentByteSize
          contentFilesCount
          indexByteSize
          indexShardsCount
          newLinesCount
          defaultBranchNewLinesCount
          otherBranchesNewLinesCount
        }
        host {
          name
        }
        refs {
          ref {
            displayName
          }
          skippedIndexed {
            count
            query
          }
        }
      }
      externalServices(first: 100) {
        nodes {
          displayName
        }
      }
    }
    totalCount
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


_COMMIT_COUNT_PLACEHOLDER = "__COMMIT_COUNT_FRAGMENT__"


def build_repos_query(*, count_commits: bool) -> str:
    """Return the GraphQL listing query, optionally with the commit-count fragment.

    Uses a literal placeholder + str.replace rather than str.format so that
    the GraphQL query body's many `{` / `}` braces don't trip up the format
    parser.
    """
    fragment = COMMIT_COUNT_FRAGMENT if count_commits else ""
    return GRAPHQL_QUERY_TEMPLATE.replace(_COMMIT_COUNT_PLACEHOLDER, fragment)


CURRENT_USER_QUERY = """
query { currentUser { username } }
"""

RECLONE_MUTATION = """
mutation Reclone($repo: ID!) {
  recloneRepository(repo: $repo) {
    alwaysNil
  }
}
"""

REINDEX_MUTATION = """
mutation Reindex($repository: ID!) {
  reindexRepository(repository: $repository) {
    alwaysNil
  }
}
"""

SKIPPED_FILES_REASON_QUERY = """
query SkippedFileReasons($query: String!) {
  search(query: $query, version: V2) {
    results {
      results {
        ... on FileMatch {
          repository {
            name
          }
          file {
            path
            byteSize
          }
          chunkMatches {
            content
          }
        }
      }
    }
  }
}
"""

REPO_REV_VALIDATION_QUERY = """
query ValidateRepoRev($name: String!, $rev: String!) {
  repository(name: $name) {
    name
    defaultBranch {
      displayName
    }
    commit(rev: $rev) {
      oid
    }
    textSearchIndex {
      refs {
        ref {
          displayName
        }
        indexed
        indexedCommit {
          oid
        }
      }
    }
  }
}
"""

# --- Metadata extractors used by the COLUMNS table --------------------------------


def decode_repo_id(base64_id: str) -> int:
    """Decode Sourcegraph's base64 repo ID to its integer form."""
    return int(base64.b64decode(base64_id).decode().split(":", 1)[1])


def get_path(repo: dict[str, Any], path: str) -> object | None:
    """Walk a dotted path through nested dicts; return None if any step is missing.

    Example: get_path(repo, "mirrorInfo.updateSchedule.intervalSeconds")
    """
    current: object = repo
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        # cast keeps strict type-checkers happy: isinstance() on `object` only
        # narrows to dict[Unknown, Unknown], so we re-view it concretely.
        current_dict = cast("dict[str, object]", current)
        next_value = current_dict.get(key)
        if next_value is None:
            return None
        current = next_value
    return current


def get_path_mb(repo: dict[str, Any], path: str) -> int | None:
    """Like get_path, but convert to megabytes."""
    value = get_path(repo, path)
    if isinstance(value, (int, str)):
        return int(value) // (1024 * 1024)
    return None


def derive_mirror_status(repo: dict[str, Any]) -> str:
    """Summarize the repo's mirror state into a single status string."""
    mirror: dict[str, Any] = repo.get("mirrorInfo") or {}
    if mirror.get("isCorrupted"):
        return "corrupted"
    if mirror.get("lastError"):
        return "errored"
    if mirror.get("cloneInProgress"):
        return "cloning"
    if mirror.get("cloned"):
        return "cloned"
    return "not_cloned"


def derive_index_status(repo: dict[str, Any]) -> str:
    """Summarize the repo's search-index state as 'indexed' or 'not_indexed'."""
    return (
        "indexed"
        if get_path(repo, "textSearchIndex.status") is not None
        else "not_indexed"
    )


def join_external_services(repo: dict[str, Any]) -> str:
    """Combine all attached code-host display names into one ';'-separated string."""
    services: dict[str, Any] = repo.get("externalServices") or {}
    nodes: list[dict[str, Any]] = services.get("nodes") or []
    return "; ".join(str(es["displayName"]) for es in nodes)


def join_corruption_logs(repo: dict[str, Any]) -> str:
    """Flatten corruptionLogs into a ';'-separated 'timestamp: reason' string."""
    mirror: dict[str, Any] = repo.get("mirrorInfo") or {}
    logs: list[dict[str, Any]] = mirror.get("corruptionLogs") or []
    return "; ".join(
        f"{log.get('timestamp', '')}: {log.get('reason', '')}" for log in logs
    )


def truncate_sync_output(repo: dict[str, Any]) -> str | None:
    """Return lastSyncOutput truncated to first 5 + last 5 lines if >10 lines.

    Only invoked for cloning-error rows (see CLONING_ERROR_EXTRA_COLUMNS), so
    we don't need to filter out healthy 'cloned' repos here.
    """
    value = get_path(repo, "mirrorInfo.lastSyncOutput")
    if not isinstance(value, str):
        return None
    return truncate_lines(value)


def truncate_lines(value: str, head: int = 5, tail: int = 5) -> str:
    """Truncate a multi-line string to the first `head` + last `tail` lines."""
    lines = value.splitlines()
    if len(lines) <= head + tail:
        return value
    omitted = len(lines) - head - tail
    return "\n".join(
        [*lines[:head], f"... [{omitted} lines truncated] ...", *lines[-tail:]],
    )


def has_cloning_error(repo: dict[str, Any]) -> bool:
    """Return True if the repo has any cloning/mirroring error condition.

    Covers repos with lastError, isCorrupted, or that have not yet been
    cloned at all.
    """
    return derive_mirror_status(repo) in {"errored", "corrupted", "not_cloned"}


def has_indexing_error(repo: dict[str, Any]) -> bool:
    """Return True if the repo is cloned but missing a search index.

    Non-cloned repos are excluded — they cannot be indexed until cloning
    finishes, so they are treated as cloning errors instead.
    """
    return (
        derive_mirror_status(repo) == "cloned"
        and get_path(repo, "textSearchIndex.status") is None
    )


def _index_refs(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """Return textSearchIndex.refs (or [] when missing)."""
    index: dict[str, Any] = repo.get("textSearchIndex") or {}
    refs: list[dict[str, Any]] = index.get("refs") or []
    return refs


def total_skipped_files(repo: dict[str, Any]) -> int:
    """Sum skippedIndexed.count across every indexed ref of the repo."""
    total = 0
    for ref in _index_refs(repo):
        skipped: dict[str, Any] = ref.get("skippedIndexed") or {}
        count = skipped.get("count")
        if count is not None:
            total += int(count)
    return total


def refs_with_skips(repo: dict[str, Any]) -> str:
    """Return ';'-joined '<refName>=<count>' for refs with skipped files."""
    parts: list[str] = []
    for ref in _index_refs(repo):
        skipped: dict[str, Any] = ref.get("skippedIndexed") or {}
        count = skipped.get("count")
        if count is None:
            continue
        n = int(count)
        if n <= 0:
            continue
        ref_node: dict[str, Any] = ref.get("ref") or {}
        name = str(ref_node.get("displayName") or "")
        parts.append(f"{name}={n}")
    return "; ".join(parts)


def head_skipped_query(repo: dict[str, Any]) -> str:
    """Return the skippedIndexed.query for the HEAD ref (or first ref with skips).

    The string is the raw Sourcegraph search query produced by the API; paste it
    into the instance's search box (or URL-encode it as ?q=...) to enumerate
    the skipped files and their reasons.
    """
    head_query = ""
    fallback = ""
    for ref in _index_refs(repo):
        skipped: dict[str, Any] = ref.get("skippedIndexed") or {}
        count = skipped.get("count") or 0
        if int(count) <= 0:
            continue
        query = str(skipped.get("query") or "")
        ref_node: dict[str, Any] = ref.get("ref") or {}
        name = str(ref_node.get("displayName") or "")
        if name == "HEAD":
            head_query = query
            break
        if not fallback:
            fallback = query
    return head_query or fallback


def has_skipped_files(repo: dict[str, Any]) -> bool:
    """Return True if zoekt skipped at least one file for this repo."""
    return total_skipped_files(repo) > 0


def default_branch_commit_count(repo: dict[str, Any]) -> int | None:
    """Return the number of commits on the repo's default branch.

    Returns None when the repo has no default branch (e.g. an empty repo,
    or one whose clone has not progressed far enough for HEAD to resolve),
    or when --count-commits was not requested (in which case the listing
    query never asked for the field and the path is missing).
    """
    value = get_path(repo, "defaultBranch.target.commit.ancestors.totalCount")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


# --- CSV format -----------------------------------------------------------
# Each entry is (csv_column_name, extractor_function). Keeping the column name
# next to the function that produces its value eliminates the risk of the
# header drifting out of sync with the row data.
COLUMNS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("id", lambda r: decode_repo_id(r["id"])),
    ("url", lambda r: r.get("url")),
    ("mirrorInfo.remoteURL", lambda r: get_path(r, "mirrorInfo.remoteURL")),
    ("externalServices", join_external_services),
    ("mirrorInfo.status", derive_mirror_status),
    ("isFork", lambda r: r.get("isFork")),
    ("isArchived", lambda r: r.get("isArchived")),
    ("isPrivate", lambda r: r.get("isPrivate")),
    ("mirrorInfo.byteSize(MB)", lambda r: get_path_mb(r, "mirrorInfo.byteSize")),
    ("createdAt", lambda r: r.get("createdAt")),
    ("mirrorInfo.lastChanged", lambda r: get_path(r, "mirrorInfo.lastChanged")),
    ("mirrorInfo.updatedAt", lambda r: get_path(r, "mirrorInfo.updatedAt")),
    ("mirrorInfo.nextSyncAt", lambda r: get_path(r, "mirrorInfo.nextSyncAt")),
    (
        "mirrorInfo.updateSchedule.intervalSeconds",
        lambda r: get_path(r, "mirrorInfo.updateSchedule.intervalSeconds"),
    ),
    ("mirrorInfo.shard", lambda r: get_path(r, "mirrorInfo.shard")),
    ("textSearchIndex.status", derive_index_status),
    (
        "textSearchIndex.status.updatedAt",
        lambda r: get_path(r, "textSearchIndex.status.updatedAt"),
    ),
    (
        "textSearchIndex.status.contentByteSize(MB)",
        lambda r: get_path_mb(r, "textSearchIndex.status.contentByteSize"),
    ),
    (
        "textSearchIndex.status.contentFilesCount",
        lambda r: get_path(r, "textSearchIndex.status.contentFilesCount"),
    ),
    (
        "textSearchIndex.status.indexByteSize(MB)",
        lambda r: get_path_mb(r, "textSearchIndex.status.indexByteSize"),
    ),
    (
        "textSearchIndex.status.indexShardsCount",
        lambda r: get_path(r, "textSearchIndex.status.indexShardsCount"),
    ),
    (
        "textSearchIndex.status.newLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.newLinesCount"),
    ),
    (
        "textSearchIndex.status.defaultBranchNewLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.defaultBranchNewLinesCount"),
    ),
    (
        "textSearchIndex.status.otherBranchesNewLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.otherBranchesNewLinesCount"),
    ),
    ("textSearchIndex.host.name", lambda r: get_path(r, "textSearchIndex.host.name")),
]

CSV_COLUMNS = [name for name, _ in COLUMNS]
URL_COLUMN_INDEX = CSV_COLUMNS.index("url")

# Optional column appended to every CSV (main, cloning-errors, indexing-errors,
# skipped-files) when --count-commits is set. Kept separate from COLUMNS so
# the default CSV format is unchanged for users who don't pass the flag.
COMMIT_COUNT_COLUMN: tuple[str, Callable[[dict[str, Any]], Any]] = (
    "defaultBranch.target.commit.ancestors.totalCount",
    default_branch_commit_count,
)

# Extra columns appended only to the cloning-errors CSV.
CLONING_ERROR_EXTRA_COLUMNS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("mirrorInfo.isCorrupted", lambda r: get_path(r, "mirrorInfo.isCorrupted")),
    ("mirrorInfo.lastError", lambda r: get_path(r, "mirrorInfo.lastError")),
    ("mirrorInfo.lastSyncOutput", truncate_sync_output),
    ("mirrorInfo.corruptionLogs", join_corruption_logs),
]
CLONING_ERROR_CSV_COLUMNS = CSV_COLUMNS + [
    name for name, _ in CLONING_ERROR_EXTRA_COLUMNS
]
# The indexing-errors CSV reuses CSV_COLUMNS verbatim — Sourcegraph's GraphQL
# does not expose any per-repo zoekt error fields beyond textSearchIndex.status.

# Extra columns appended only to the skipped-files CSV. The query is the
# Sourcegraph search query produced by the API; running it lists each skipped
# file along with its NOT-INDEXED reason (too-large / binary / too-many-trigrams
# / too-small / blob-missing).
SKIPPED_FILES_EXTRA_COLUMNS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("skippedIndexed.totalCount", total_skipped_files),
    ("skippedIndexed.refsWithSkips", refs_with_skips),
    ("skippedIndexed.headQuery", head_skipped_query),
]
SKIPPED_FILES_CSV_COLUMNS = CSV_COLUMNS + [
    name for name, _ in SKIPPED_FILES_EXTRA_COLUMNS
]


# --- HTTP / GraphQL plumbing --------------------------------------------------


class GraphQLError(RuntimeError):
    """Raised when the Sourcegraph GraphQL API returns errors."""


class HTTPRequestError(RuntimeError):
    """Raised when the server returns a definitive 4xx/5xx HTTP response."""

    def __init__(
        self,
        status: int,
        reason: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        """Capture the response status, headers, and body for later logging."""
        super().__init__(f"HTTP {status} {reason}")
        self.status = status
        self.reason = reason
        self.url = url
        self.headers = headers
        self.body = body


def open_connection(parsed: ParseResult) -> http.client.HTTPConnection:
    """Open an HTTP(S) connection, rejecting any scheme other than http/https.

    We use http.client directly (rather than urllib.request) so that the set
    of allowed URL schemes is enforced explicitly here, rather than relying
    on urllib.request's broader (file:, ftp:, ...) acceptance.
    """
    if not parsed.hostname:
        msg = f"URL is missing a hostname: {parsed.geturl()!r}"
        raise ValueError(msg)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    if parsed.scheme == "http":
        return http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    msg = f"Unsupported URL scheme: {parsed.scheme!r} (expected http or https)"
    raise ValueError(msg)


def send_once(
    url: str,
    body: bytes,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Send one POST. Returns parsed JSON on 2xx, raises HTTPRequestError on 4xx/5xx."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = open_connection(parsed)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        response_body = resp.read()
        if resp.status >= http.client.BAD_REQUEST:
            raise HTTPRequestError(
                resp.status,
                resp.reason,
                url,
                resp.getheaders(),
                response_body,
            )
        return json.loads(response_body)
    finally:
        conn.close()


def send_or_capture_oserror(
    url: str,
    body: bytes,
    headers: dict[str, str],
) -> dict[str, Any] | OSError:
    """Send once. Re-raises HTTPRequestError; returns OSError instances for retry.

    Pulling the try/except out of the retry loop keeps the loop body simple and
    avoids the per-iteration exception-handler setup cost.
    """
    try:
        return send_once(url, body, headers)
    except OSError as exc:
        return exc


def send_with_retry(
    url: str,
    body: bytes,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Execute an HTTP request, retrying transient network errors only.

    HTTPRequestError (4xx/5xx) propagates straight through — the server gave us
    a definitive answer and retrying won't change it. Only socket-level OSError
    cases (DNS failure, connection refused, timeout, etc.) get retried.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        result = send_or_capture_oserror(url, body, headers)
        if not isinstance(result, OSError):
            return result
        if attempt == MAX_RETRIES:
            raise result
        logger.warning(
            "Request failed (attempt %d/%d): %s — retrying in %ds...",
            attempt,
            MAX_RETRIES,
            result,
            RETRY_DELAY_SECONDS,
        )
        time.sleep(RETRY_DELAY_SECONDS)
    msg = "send_with_retry loop exhausted unexpectedly"
    raise RuntimeError(msg)


def graphql_request(
    endpoint: str,
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Send a GraphQL query to the Sourcegraph API and return the data block."""
    url = endpoint.rstrip("/") + "/.api/graphql"
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
        "User-Agent": "list-repos/0.0.1",
    }
    data = send_with_retry(url, body, headers)
    if data.get("errors"):
        # GraphQL can return both `errors` and partial `data`. If we have data,
        # log the errors and keep going; only abort if no data was returned.
        if data.get("data"):
            logger.warning(
                "GraphQL returned %d partial error(s): %s",
                len(data["errors"]),
                json.dumps(data["errors"], indent=2),
            )
        else:
            msg = f"GraphQL errors: {json.dumps(data['errors'], indent=2)}"
            raise GraphQLError(msg)
    return data["data"]


def fetch_current_username(endpoint: str, token: str) -> str:
    """Return the username of the authenticated user.

    All GraphQL queries used by this script require authentication, so an
    invalid/anonymous token is rejected by validate_token (or by the server
    via HTTPRequestError) before this is ever called.
    """
    data = graphql_request(endpoint, token, CURRENT_USER_QUERY, {})
    return str(data["currentUser"]["username"])


def trigger_reclone(endpoint: str, token: str, repo_id: str) -> bool:
    """Send recloneRepository mutation. Returns True on success, False on GraphQL error."""
    try:
        graphql_request(endpoint, token, RECLONE_MUTATION, {"repo": repo_id})
    except (GraphQLError, HTTPRequestError) as exc:
        logger.warning("recloneRepository failed for %s: %s", repo_id, exc)
        return False
    return True


def trigger_reindex(endpoint: str, token: str, repo_id: str) -> bool:
    """Send reindexRepository mutation. Returns True on success, False on GraphQL error."""
    try:
        graphql_request(endpoint, token, REINDEX_MUTATION, {"repository": repo_id})
    except (GraphQLError, HTTPRequestError) as exc:
        logger.warning("reindexRepository failed for %s: %s", repo_id, exc)
        return False
    return True


def sanitize_for_filename(text: str) -> str:
    """Replace non-[A-Za-z0-9._-] chars with '_' so the string is filesystem-safe."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def sanitize_endpoint_for_filename(endpoint: str) -> str:
    """Sanitize an endpoint URL for use in filenames, dropping the http(s) scheme."""
    return sanitize_for_filename(re.sub(r"^https?://", "", endpoint))


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")


def _split_name_rev(repo_rev: str) -> tuple[str, str | None]:
    """Split 'name[@rev]' into (name, rev_or_None).

    Two paths, depending on whether the input has a URL scheme:

      - With scheme (`https://`, `ssh://`, `git+ssh://`, …): use
        urllib.parse.urlsplit so user[:password]@host is parsed and dropped
        cleanly. After that the only `@` that can remain is the rev
        separator, even if the rev itself contains `/` (e.g. `feature/foo`).

      - Without scheme: the rightmost `@` is the rev separator unless the
        part after it contains a `:` *before* any `/` — that pattern means
        scp-style `user@host:path` (e.g. `git@github.com:org/repo`), so we
        drop the `user@` prefix instead.
    """
    rev: str | None = None
    if _SCHEME_RE.match(repo_rev):
        u = urlsplit(repo_rev)
        # u.hostname is lower-cased and userinfo-stripped.
        name = (u.hostname or "") + u.path
        if "@" in name:
            before, after = name.rsplit("@", 1)
            name, rev = before, after
        return name, rev

    name = repo_rev
    if "@" in name:
        before, after = name.rsplit("@", 1)
        slash = after.find("/")
        colon = after.find(":")
        if colon != -1 and (slash == -1 or colon < slash):
            # scp-style 'user@host:path' — drop the 'user@'.
            name = after
        else:
            name, rev = before, after
    return name, rev


def parse_repo_rev(repo_rev: str) -> str:
    """Extract the revision from 'repo[$]@rev'. Returns 'HEAD' if no '@rev' is present."""
    _, rev = _split_name_rev(repo_rev)
    return rev if rev is not None else "HEAD"


def parse_repo_name(repo_rev: str) -> str:
    """Extract the canonical repo name from 'repo[$]@rev'.

    Normalizes common copy-paste shapes so a user can hand us anything that
    visually identifies a repo:
      - Strips a leading URL scheme (`http://`, `https://`, `ssh://`,
        `git+ssh://`, …). Sourcegraph stores repos as bare names like
        `github.com/foo/bar`, so a pasted URL would otherwise miss.
      - Drops an SSH `user@host` prefix (e.g. `ssh://git@github.com/foo/bar`
        becomes `github.com/foo/bar`).
      - Strips a leading '^' and trailing '$' anchor (Sourcegraph repo regex
        syntax) so the result is safe to pass to `repository(name:)`, which
        expects an exact name.
      - Strips a trailing slash (common when copying URLs from a browser).
    """
    name, _ = _split_name_rev(repo_rev)
    name = name.removeprefix("^").removesuffix("$")
    return name.rstrip("/")


def verify_repo_rev(endpoint: str, token: str, repo_rev: str) -> str:
    """Validate the repo+rev triple and return the resolved revision name.

    Three checks, in order:
    1. The repository exists on this Sourcegraph instance.
    2. The revision resolves to a commit in that repository.
    3. The repository has a search index AND one of its indexed refs points
       at the same commit oid as the resolved revision (so a skipped-file
       search at this rev will return meaningful results).

    Returns the canonical revision name to use downstream: the user's input
    when they explicitly specified a rev, or the repository's default branch
    name (e.g. "main") when they didn't (or specified "@HEAD"). Exits via
    `die()` on any of the three failure conditions.
    """
    name = parse_repo_name(repo_rev)
    rev = parse_repo_rev(repo_rev)
    data = graphql_request(
        endpoint,
        token,
        REPO_REV_VALIDATION_QUERY,
        {"name": name, "rev": rev},
    )
    repository: dict[str, Any] | None = data.get("repository")
    if repository is None:
        die(f"repository {name!r} not found on {endpoint}")
    commit: dict[str, Any] | None = repository.get("commit")
    if commit is None:
        die(f"revision {rev!r} not found in repository {name!r}")

    text_index: dict[str, Any] | None = repository.get("textSearchIndex")
    refs: list[dict[str, Any]] = (text_index or {}).get("refs") or []
    target_oid = commit.get("oid")
    indexed_oids: set[str] = set()
    indexed_names: list[str] = []
    for ref in refs:
        if not ref.get("indexed"):
            continue
        indexed_commit: dict[str, Any] = ref.get("indexedCommit") or {}
        oid = indexed_commit.get("oid")
        if oid:
            indexed_oids.add(str(oid))
        ref_node: dict[str, Any] = ref.get("ref") or {}
        indexed_names.append(str(ref_node.get("displayName") or "?"))
    if target_oid not in indexed_oids:
        if indexed_names:
            indexed_summary = "\n".join(f"  - {n}" for n in indexed_names)
        else:
            indexed_summary = "  (none)"
        die(
            f"revision {rev!r} (commit {target_oid}) is not currently indexed "
            f"in repository {name!r}.\nIndexed refs:\n{indexed_summary}",
        )

    # When the user didn't specify a rev (or explicitly used "HEAD"), substitute
    # the actual default branch name so filenames and URLs read naturally.
    if rev == "HEAD":
        default_branch: dict[str, Any] = repository.get("defaultBranch") or {}
        return str(default_branch.get("displayName") or "HEAD")
    return rev


def file_url(endpoint: str, repo_name: str, rev: str, file_path: str) -> str:
    """Build a clickable Sourcegraph URL pointing at a specific file at a revision."""
    base = endpoint.rstrip("/")
    rev_segment = f"@{rev}" if rev and rev != "HEAD" else ""
    return f"{base}/{repo_name}{rev_segment}/-/blob/{file_path}"


def fetch_skipped_file_matches(
    endpoint: str,
    token: str,
    name: str,
    rev: str,
) -> list[dict[str, Any]]:
    """Run the SkippedFileReasons search query and return non-empty FileMatch results.

    The repo filter is built as `r:^<escaped-name>$@<rev>` so that:
      - the regex is anchored on both ends (a bare prefix like
        "github.com/org/repo" no longer also matches
        "github.com/org/repo-fork" or "github.com/org/repository").
      - dots and other regex specials in the name are escaped (otherwise
        "github.com/foo/bar" would match "githubXcom/foo/bar").
      - the resolved rev is sent explicitly so the search runs against the
        same revision shown in the output filenames and file URLs (rather
        than relying on the server's HEAD pointer, which could shift between
        the verify_repo_rev call and this query).
    """
    repo_filter = f"^{re.escape(name)}$"
    if rev and rev != "HEAD":
        repo_filter += f"@{rev}"
    search_query = f"r:{repo_filter} type:file index:only patternType:regexp count:all ^NOT-INDEXED:"
    data = graphql_request(
        endpoint,
        token,
        SKIPPED_FILES_REASON_QUERY,
        {"query": search_query},
    )
    raw_results: list[dict[str, Any] | None] = (
        data.get("search", {}).get("results", {}).get("results") or []
    )
    # Non-FileMatch results come back as empty objects; drop them.
    return [r for r in raw_results if r and r.get("file")]


def write_skipped_files_reason(
    endpoint: str,
    token: str,
    repo_rev: str,
) -> None:
    """Fetch skipped-file matches for repo_rev and write the per-file and stats CSVs."""
    # Include the endpoint in filenames so a customer comparing results across
    # multiple Sourcegraph instances doesn't overwrite outputs from other runs.
    endpoint_sanitized = sanitize_endpoint_for_filename(endpoint)
    # Pre-emptively unlink any files matching the user's raw input so a
    # validation failure (e.g. unknown rev) doesn't leave stale outputs.
    input_name_sanitized = sanitize_for_filename(parse_repo_name(repo_rev))
    input_rev_sanitized = sanitize_for_filename(parse_repo_rev(repo_rev))
    input_prefix = f"{endpoint_sanitized}-{input_name_sanitized}-{input_rev_sanitized}"
    Path(f"{input_prefix}-skipped-files.csv").unlink(missing_ok=True)
    Path(f"{input_prefix}-skipped-stats.csv").unlink(missing_ok=True)

    rev = verify_repo_rev(endpoint, token, repo_rev)
    # Use the resolved rev so filenames and URLs always include a real branch
    # name, even when the user omitted `@rev` and we defaulted to the repo's
    # default branch.
    name = parse_repo_name(repo_rev)
    name_sanitized = sanitize_for_filename(name)
    rev_sanitized = sanitize_for_filename(rev)
    prefix = f"{endpoint_sanitized}-{name_sanitized}-{rev_sanitized}"
    files_path = Path(f"{prefix}-skipped-files.csv")
    stats_path = Path(f"{prefix}-skipped-stats.csv")
    # Also unlink the resolved-rev files (when distinct from the input rev)
    # so an unexpected fetch failure later doesn't keep a stale CSV around.
    if prefix != input_prefix:
        files_path.unlink(missing_ok=True)
        stats_path.unlink(missing_ok=True)

    # (header, extractor) tables — same pattern as the module-level COLUMNS
    # table — to keep the CSV header and row values in lockstep. Adding,
    # removing, or reordering a column here updates both at once.
    # Path.suffix returns ".ext" (or "" for no extension or for dotfiles like
    # ".env"); we strip the leading dot to display "go" rather than ".go".
    file_columns: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
        (
            "chunkMatches.content",
            lambda m: "\n".join(
                str(c.get("content") or "") for c in (m.get("chunkMatches") or [])
            ),
        ),
        (
            "file.byteSize",
            lambda m: (
                int(bs)
                if (bs := (m.get("file") or {}).get("byteSize")) is not None
                else ""
            ),
        ),
        (
            "file.extension",
            lambda m: Path(
                str((m.get("file") or {}).get("path") or ""),
            ).suffix.lstrip("."),
        ),
        (
            "file_url",
            lambda m: file_url(
                endpoint,
                str((m.get("repository") or {}).get("name") or ""),
                rev,
                str((m.get("file") or {}).get("path") or ""),
            ),
        ),
    ]
    stats_columns: list[tuple[str, Callable[[tuple[str, int]], Any]]] = [
        ("reason", lambda r: r[0]),
        ("count", lambda r: r[1]),
    ]

    matches = fetch_skipped_file_matches(endpoint, token, name, rev)

    reason_counts: collections.Counter[str] = collections.Counter()
    rows: list[list[Any]] = []
    for match in matches:
        rows.append([extract(match) for _, extract in file_columns])
        for chunk in match.get("chunkMatches") or []:
            reason_match = re.search(
                r"NOT-INDEXED:\s*(.+)",
                str(chunk.get("content") or ""),
            )
            if reason_match:
                reason_counts[reason_match.group(1).strip()] += 1

    # Sort by chunkMatches.content so files with the same NOT-INDEXED reason
    # are grouped together; ties broken by byteSize, extension, then file_url.
    # Coerce byteSize to int (treating missing values as -1) so an int/str
    # union can't blow up the comparator.
    rows.sort(
        key=lambda r: (r[0], r[1] if isinstance(r[1], int) else -1, r[2], r[3]),
    )

    with files_path.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow([n for n, _ in file_columns])
        writer.writerows(rows)
    files_written = len(rows)

    with stats_path.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow([n for n, _ in stats_columns])
        for record in reason_counts.most_common():
            writer.writerow([extract(record) for _, extract in stats_columns])

    logger.info(
        "Wrote %d skipped-file match(es) to %s",
        files_written,
        files_path.name,
    )
    logger.info(
        "Wrote %d NOT-INDEXED reason categor(ies) to %s",
        len(reason_counts),
        stats_path.name,
    )


# --- Repo CSV pipeline --------------------------------------------------------


class LazyCSVWriter:
    """csv.writer wrapper that opens its file on the first writerow() call.

    For optional outputs that may end up empty (no cloning/indexing/skipped
    rows): if no rows are written, no file is created at all — eliminating
    the create-then-delete-if-empty dance. Memory cost is constant; rows
    pass straight through to csv.writer just like before, only the open()
    and header-write are deferred.
    """

    def __init__(self, path: Path, columns: list[str]) -> None:
        self.path = path
        self.columns = columns
        self.count = 0
        self._file: TextIO | None = None
        self._writer: Any = None

    def writerow(self, row: list[Any]) -> None:
        if self._writer is None:
            self._file = self.path.open("w", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(self.columns)
        self._writer.writerow(row)
        self.count += 1

    def __enter__(self) -> LazyCSVWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        if self._file is not None:
            self._file.close()


def fetch_repos(
    endpoint: str,
    token: str,
    max_repos: int | None = None,
    *,
    count_commits: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield repository nodes by paginating through the GraphQL API.

    Logs "Fetching X of Y total repositories..." once, after the first page
    returns its totalCount, so the user sees the target before per-page
    progress lines start. Avoids a separate count-only round-trip.

    When count_commits is True, the listing query is augmented with a
    defaultBranch.target.commit.ancestors.totalCount sub-tree (computed
    server-side per repo) and a smaller page size is used because that
    field is significantly more expensive than the rest of the query.
    """
    query = build_repos_query(count_commits=count_commits)
    base_page_size = PAGE_SIZE_WITH_COMMIT_COUNT if count_commits else PAGE_SIZE
    cursor: str | None = None
    total_fetched = 0
    first_page = True
    while True:
        page_size = base_page_size
        if max_repos is not None:
            page_size = min(page_size, max_repos - total_fetched)
        data = graphql_request(
            endpoint,
            token,
            query,
            {"first": page_size, "after": cursor},
        )
        connection = data["repositories"]
        total_count = connection["totalCount"]
        target = min(max_repos, total_count) if max_repos is not None else total_count
        if first_page:
            logger.info(
                "Fetching %d of %d total repositories...",
                target,
                total_count,
            )
            first_page = False

        yield from connection["nodes"]

        total_fetched += len(connection["nodes"])
        logger.info("Fetched %d/%d repositories...", total_fetched, target)

        if not connection["pageInfo"]["hasNextPage"]:
            break
        if max_repos is not None and total_fetched >= max_repos:
            break
        cursor = connection["pageInfo"]["endCursor"]


def build_row(repo: dict[str, Any], endpoint: str) -> list[Any]:
    """Build a single CSV row by running every COLUMNS extractor against the repo.

    The 'url' column is stored relative in GraphQL (e.g. '/github.com/foo/bar');
    we rewrite it to an absolute URL here so the CSV is directly clickable.

    The commit-count column is intentionally NOT added here — callers append
    it after any per-CSV extra columns so it always lands in the rightmost
    position, matching the header produced by csv_columns_for().
    """
    base = endpoint.rstrip("/")
    row = [extract(repo) for _, extract in COLUMNS]
    if row[URL_COLUMN_INDEX]:
        row[URL_COLUMN_INDEX] = base + row[URL_COLUMN_INDEX]
    return row


def append_commit_count(
    row: list[Any],
    repo: dict[str, Any],
    *,
    count_commits: bool,
) -> list[Any]:
    """Return row with the commit-count value appended when count_commits is True."""
    if count_commits:
        return [*row, COMMIT_COUNT_COLUMN[1](repo)]
    return row


def csv_columns_for(base_columns: list[str], *, count_commits: bool) -> list[str]:
    """Return the CSV header list, with the commit-count column appended if requested."""
    if count_commits:
        return [*base_columns, COMMIT_COUNT_COLUMN[0]]
    return list(base_columns)


def write_csv(
    out: TextIO,
    cloning_writer: LazyCSVWriter,
    indexing_writer: LazyCSVWriter,
    skipped_writer: LazyCSVWriter | None,
    endpoint: str,
    token: str,
    max_repos: int | None = None,
    *,
    reclone: bool = False,
    reindex: bool = False,
    count_commits: bool = False,
) -> tuple[int, int, int]:
    """Stream repos directly to CSV rather than collecting them first.

    Repos with cloning/mirror errors are written to cloning_writer (with
    extra mirror-error detail columns appended); repos that are cloned but
    missing a search index are written to indexing_writer; repos whose
    index has at least one skipped file (zoekt SkipReason) are written to
    skipped_writer with per-ref counts and the search query that lists the
    skipped files. If reclone is set, the recloneRepository mutation is sent
    for each cloning-error repo; if reindex is set, the reindexRepository
    mutation is sent for each indexing-error repo. Skipped-file reporting has
    no remediation mutation — fixes are configuration-level (search.largeFiles
    or .sourcegraph/ignore).

    All three extra writers are LazyCSVWriter instances that defer file
    creation until the first matching row, so empty error/skipped CSVs are
    never created. cloning_writer and indexing_writer are always passed in;
    skipped_writer is None when --skipped-files was not requested. Per-
    category row counts are tracked on each writer's `.count`.

    Memory stays constant regardless of how many repos are fetched.

    Returns (total, reclone_total, reindex_total).
    """
    writer = csv.writer(out)
    writer.writerow(csv_columns_for(CSV_COLUMNS, count_commits=count_commits))

    total = 0
    reclone_total = 0
    reindex_total = 0
    for repo in fetch_repos(endpoint, token, max_repos, count_commits=count_commits):
        row = build_row(repo, endpoint)
        writer.writerow(append_commit_count(row, repo, count_commits=count_commits))
        total += 1
        if count_commits:
            commit_count = default_branch_commit_count(repo)
            if commit_count is None:
                # Common for empty / not-yet-cloned repos. Log so users
                # grepping the log can spot which repos returned no count
                # without it being a noisy WARNING.
                logger.info(
                    "No commit count for %s (no default branch or unresolved HEAD)",
                    repo.get("url") or repo.get("id"),
                )
            else:
                logger.info(
                    "Commit count for %s: %d",
                    repo.get("url") or repo.get("id"),
                    commit_count,
                )
        if has_cloning_error(repo):
            cloning_writer.writerow(
                append_commit_count(
                    row + [extract(repo) for _, extract in CLONING_ERROR_EXTRA_COLUMNS],
                    repo,
                    count_commits=count_commits,
                ),
            )
            if reclone and trigger_reclone(endpoint, token, repo["id"]):
                reclone_total += 1
        if has_indexing_error(repo):
            indexing_writer.writerow(
                append_commit_count(row, repo, count_commits=count_commits),
            )
            if reindex and trigger_reindex(endpoint, token, repo["id"]):
                reindex_total += 1
        if skipped_writer is not None and has_skipped_files(repo):
            skipped_writer.writerow(
                append_commit_count(
                    row + [extract(repo) for _, extract in SKIPPED_FILES_EXTRA_COLUMNS],
                    repo,
                    count_commits=count_commits,
                ),
            )
    return (total, reclone_total, reindex_total)


def log_http_error(exc: HTTPRequestError) -> None:
    """Log status, headers, body, and traceback of a non-2xx HTTP response."""
    logger.error("HTTP %s %s", exc.status, exc.reason)
    logger.error("URL: %s", exc.url)
    for header, value in exc.headers:
        logger.error("  %s: %s", header, value)
    body = exc.body.decode(errors="replace")
    if body:
        logger.error("Response body:\n%s", body)
    logger.error("HTTP request failed", exc_info=exc)


def load_dotenv() -> None:
    """Populate SRC_ENDPOINT and SRC_ACCESS_TOKEN from `.env` if not already set.

    Real environment variables always take precedence over the `.env` file —
    this is the standard order of precedence used by python-dotenv et al.
    """
    env_file = Path(".env")
    if not env_file.is_file():
        return
    for lineno, raw in enumerate(
        env_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw.strip()
        # Blank lines and comments are normal `.env` content; skip silently.
        if not line or line.startswith("#"):
            continue
        # Anything else without an '=' is malformed (e.g. "SRC_ENDPOINT https://…"
        # missing the '='). Warn so the user gets a clearer hint than the
        # downstream "set SRC_ENDPOINT and SRC_ACCESS_TOKEN" error. We log the
        # line number only — never the line content, since a malformed line
        # could be carrying a secret like SRC_ACCESS_TOKEN.
        if "=" not in line:
            logger.warning(
                ".env line %d is malformed (missing '='); skipping.",
                lineno,
            )
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("SRC_ENDPOINT", "SRC_ACCESS_TOKEN"):
            # setdefault: real env wins over .env
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def die(message: str) -> NoReturn:
    """Log a one-line error and exit with status 1. Never returns."""
    logger.error("Error: %s", message)
    sys.exit(1)


def validate_endpoint(endpoint: str) -> None:
    """Reject obviously-bad SRC_ENDPOINT values with a friendly message."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("https", "http"):
        die(
            f"SRC_ENDPOINT must start with https:// or http:// (got {endpoint!r}).",
        )
    if not parsed.hostname:
        die(f"SRC_ENDPOINT is missing a hostname (got {endpoint!r}).")


def validate_token(token: str) -> None:
    """Reject obviously-bad SRC_ACCESS_TOKEN values with a friendly message."""
    if not token.startswith("sgp_"):
        # Don't log any of the token bytes — even a 5-char prefix can leak
        # info about the source/format. Length alone is enough to confirm
        # something was set without echoing secret material.
        die(
            f"SRC_ACCESS_TOKEN must be a Sourcegraph access token starting "
            f"with 'sgp_' (got a {len(token)}-character value).",
        )


def require_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Return (endpoint, token), exiting with a friendly message on any problem.

    Resolution order: --src-endpoint / --src-access-token CLI args win over
    shell env vars, which win over `.env` file values (the latter two come
    pre-merged in os.environ via load_dotenv).
    """
    endpoint = args.src_endpoint or os.environ.get("SRC_ENDPOINT", "")
    token = args.src_access_token or os.environ.get("SRC_ACCESS_TOKEN", "")
    if not endpoint or not token:
        die(
            "set SRC_ENDPOINT and SRC_ACCESS_TOKEN (via --src-endpoint / "
            "--src-access-token, environment variables, or a .env file).",
        )
    validate_endpoint(endpoint)
    validate_token(token)
    return endpoint, token


class BlankLineHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """RawDescriptionHelpFormatter with two extras:

    1. A blank line is inserted between option entries.
    2. Explicit '\\n' inside an option's `help=` string is honored as a real
       line break, while long lines are still word-wrapped to terminal width.
    """

    def _format_action(self, action: argparse.Action) -> str:
        return super()._format_action(action) + "\n"

    def _split_lines(self, text: str, width: int) -> list[str]:
        lines: list[str] = []
        for raw in text.splitlines():
            if not raw.strip():
                lines.append("")
                continue
            # Preserve any leading whitespace as the wrap indent so spaces
            # used for nesting/example lines aren't collapsed.
            leading = raw[: len(raw) - len(raw.lstrip())]
            wrapped = textwrap.wrap(
                raw.lstrip(),
                width=width,
                initial_indent=leading,
                subsequent_indent=leading,
            )
            lines.extend(wrapped or [leading])
        return lines


def positive_int(value: str) -> int:
    """argparse type for ints >= 1.

    Used by --limit. The Sourcegraph GraphQL `repositories(first:)` field
    panics on negative values, so we reject them at the CLI boundary with a
    friendly message instead of letting a server-side panic surface.
    """
    try:
        n = int(value)
    except ValueError:
        msg = f"must be an integer, got {value!r}"
        raise argparse.ArgumentTypeError(msg) from None
    if n < 1:
        msg = f"must be a positive integer (>=1), got {n}"
        raise argparse.ArgumentTypeError(msg)
    return n


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments into a Namespace."""
    parser = argparse.ArgumentParser(
        description=(
            "List all repositories on a Sourcegraph instance to a CSV file, "
            "with metadata for repo clone and index statuses\n"
            "\n"
            "Requires SRC_ENDPOINT and SRC_ACCESS_TOKEN, "
            "configured via either environment variables or args"
            "\n"
        ),
        epilog=("Source: https://github.com/sourcegraph/professional-services-public"),
        formatter_class=lambda prog: BlankLineHelpFormatter(
            prog,
            max_help_position=36,
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="int",
        help="Fetch at most <int> repositories (must be >=1)",
    )
    parser.add_argument(
        "--skipped-files",
        action="store_true",
        help=(
            "Write a CSV file listing repos where the Zoekt search indexer skipped files\n"
            f"Output file: ENDPOINT-{DEFAULT_SKIPPED_FILES_FILE}"
        ),
    )
    parser.add_argument(
        "--skipped-files-reason",
        metavar="REPO@REV",
        default=None,
        help=(
            "Write a CSV file listing the files which Zoekt has skipped, and the skip reason, for a specified repo\n"
            "Output file: ENDPOINT-REPO-REV-skipped-files.csv\n"
            "and a CSV file counting the number of files skipped per reason\n"
            "Output file: ENDPOINT-REPO-REV-skipped-stats.csv\n"
            "Examples: \n"
            "    github.com/org/repo     [use repo's default branch]\n"
            "    github.com/org/repo@dev [use a non-default, but still indexed branch]\n"
        ),
    )
    parser.add_argument(
        "--reclone",
        action="store_true",
        help=(
            "Force reclone all repos with a cloning error "
            "(corrupted, errored, or not cloned)"
        ),
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help=("Force reindex all repos with an indexing error"),
    )
    parser.add_argument(
        "--count-commits",
        action="store_true",
        help=(
            "Add a 'defaultBranch.target.commit.ancestors.totalCount' column to all\n"
            "output CSVs containing the total number of commits on each repo's\n"
            "default branch. This issues a more expensive GraphQL query and uses a\n"
            "smaller page size to avoid timeouts, so only enable when needed."
        ),
    )
    parser.add_argument(
        "--src-endpoint",
        default=None,
        metavar="URL",
        help="Sourcegraph endpoint URL (e.g. https://sourcegraph.example.com)",
    )
    parser.add_argument(
        "--src-access-token",
        default=None,
        metavar="TOKEN",
        help=("Sourcegraph access token (must start with 'sgp_')"),
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, endpoint: str, token: str) -> None:
    """Confirm the connection, then stream every repo to the CSV file."""
    if args.count_commits:
        # Counting commits server-side can take a long time on monorepos. Bump
        # the per-request HTTP timeout so we wait long enough for these queries
        # to return before retrying. open_connection() reads this on each call,
        # so a global update is sufficient.
        global REQUEST_TIMEOUT_SECONDS  # noqa: PLW0603
        REQUEST_TIMEOUT_SECONDS = REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT
        logger.info(
            "--count-commits enabled: page size=%d, request timeout=%ds",
            PAGE_SIZE_WITH_COMMIT_COUNT,
            REQUEST_TIMEOUT_SECONDS,
        )
    username = fetch_current_username(endpoint, token)
    logger.info("Connected to: %s as: %s", endpoint, username)

    # When the user only wants the per-repo SkippedFileReasons report, skip the
    # full repo iteration — that query is targeted and doesn't need the listing.
    if args.skipped_files_reason:
        # The other flags only affect the full-repo iteration path. Warn about
        # any that are set so the user isn't surprised when they have no
        # effect (we can't enforce this with argparse mutual_exclusive_group
        # because --skipped-files-reason is exclusive with multiple unrelated
        # flags rather than with one specific other flag).
        ignored = [
            flag
            for flag, set_ in (
                ("--reclone", args.reclone),
                ("--reindex", args.reindex),
                ("--limit", args.limit is not None),
                ("--skipped-files", args.skipped_files),
                ("--count-commits", args.count_commits),
            )
            if set_
        ]
        if ignored:
            logger.warning(
                "Ignoring %s: --skipped-files-reason runs a single targeted "
                "query and does not iterate the repo list.",
                ", ".join(ignored),
            )
        write_skipped_files_reason(endpoint, token, args.skipped_files_reason)
        return

    # Prefix per-instance outputs with the sanitized endpoint so a customer
    # comparing results across multiple Sourcegraph instances doesn't overwrite
    # outputs from other runs.
    endpoint_sanitized = sanitize_endpoint_for_filename(endpoint)
    output_path = Path(f"{endpoint_sanitized}-{DEFAULT_OUTPUT_FILE}")
    cloning_errors_path = Path(f"{endpoint_sanitized}-{DEFAULT_CLONING_ERRORS_FILE}")
    indexing_errors_path = Path(f"{endpoint_sanitized}-{DEFAULT_INDEXING_ERRORS_FILE}")
    skipped_files_path = (
        Path(f"{endpoint_sanitized}-{DEFAULT_SKIPPED_FILES_FILE}")
        if args.skipped_files
        else None
    )
    # Clear any stale outputs from a previous run; LazyCSVWriter will only
    # recreate these files if matching rows are encountered this time.
    cloning_errors_path.unlink(missing_ok=True)
    indexing_errors_path.unlink(missing_ok=True)
    if skipped_files_path is not None:
        skipped_files_path.unlink(missing_ok=True)

    cloning_writer = LazyCSVWriter(
        cloning_errors_path,
        csv_columns_for(CLONING_ERROR_CSV_COLUMNS, count_commits=args.count_commits),
    )
    indexing_writer = LazyCSVWriter(
        indexing_errors_path,
        csv_columns_for(CSV_COLUMNS, count_commits=args.count_commits),
    )
    skipped_writer = (
        LazyCSVWriter(
            skipped_files_path,
            csv_columns_for(
                SKIPPED_FILES_CSV_COLUMNS,
                count_commits=args.count_commits,
            ),
        )
        if skipped_files_path is not None
        else None
    )
    # The skipped-files writer is optional (only when --skipped-files is set),
    # but it has to participate in the same `with` block as the always-on
    # writers. Use contextlib.nullcontext() as a no-op stand-in when disabled.
    skipped_cm = (
        skipped_writer if skipped_writer is not None else contextlib.nullcontext()
    )
    with (
        output_path.open("w", newline="") as out,
        cloning_writer,
        indexing_writer,
        skipped_cm,
    ):
        total, reclone_total, reindex_total = write_csv(
            out,
            cloning_writer,
            indexing_writer,
            skipped_writer,
            endpoint,
            token,
            args.limit,
            reclone=args.reclone,
            reindex=args.reindex,
            count_commits=args.count_commits,
        )

    logger.info("Wrote %d repos to %s", total, output_path.name)
    if cloning_writer.count:
        logger.info(
            "Wrote %d repos with cloning errors to %s",
            cloning_writer.count,
            cloning_errors_path.name,
        )
    if indexing_writer.count:
        logger.info(
            "Wrote %d repos with indexing errors to %s",
            indexing_writer.count,
            indexing_errors_path.name,
        )
    if skipped_writer is not None and skipped_writer.count:
        logger.info(
            "Wrote %d repos with skipped files to %s",
            skipped_writer.count,
            skipped_writer.path.name,
        )
    if args.reclone:
        logger.info("Triggered recloneRepository for %d repo(s)", reclone_total)
    if args.reindex:
        logger.info("Triggered reindexRepository for %d repo(s)", reindex_total)


def configure_logging(log_path: Path) -> None:
    """Send INFO-level logs to both stderr (live feedback) and log_path."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear existing handlers (e.g. on re-entry from tests).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(stderr_handler)

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"),
    )
    root.addHandler(file_handler)


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: Any,
) -> None:
    """sys.excepthook that routes uncaught exceptions through the logger.

    Without this, Python's default hook writes the traceback to stderr only,
    so repos.log would miss it. Logging via logger.error(exc_info=...) sends
    the full traceback to both handlers (stderr + repos.log).

    KeyboardInterrupt (Ctrl-C) is treated as a user-initiated graceful exit:
    a one-line message is logged with no traceback, since a stack dump for an
    intentional abort is just noise.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        logger.warning("Interrupted by user (Ctrl-C); exiting.")
        return
    logger.error(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def main() -> None:
    """Entry point: configure logging, load env, parse args, run, handle errors."""
    configure_logging(Path(DEFAULT_LOG_FILE))
    # Anything that escapes the try/except below (or is raised before it, e.g.
    # in parse_args / load_dotenv / require_credentials) lands in repos.log
    # with a full traceback via this hook.
    sys.excepthook = _log_uncaught_exception

    args = parse_args(sys.argv[1:])
    load_dotenv()
    endpoint, token = require_credentials(args)

    try:
        run(args, endpoint, token)
    except HTTPRequestError as exc:
        log_http_error(exc)
        sys.exit(1)
    except OSError:
        logger.exception(
            "Could not connect to the server. Check your network and SRC_ENDPOINT.",
        )
        sys.exit(1)
    except ValueError as exc:
        die(str(exc))
    except GraphQLError:
        logger.exception("GraphQL request failed")
        sys.exit(1)


if __name__ == "__main__":
    main()

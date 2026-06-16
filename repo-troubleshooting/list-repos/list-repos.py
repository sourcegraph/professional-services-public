#!/usr/bin/env python3

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
import shlex
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO, cast
from urllib.parse import ParseResult, urlparse, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)


# --- Tune-ables -----------------------------------------------------------------

PAGE_SIZE = 500
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 60
# Counting commits server-side can be slow on big monorepos. The COMMIT_COUNT
# call site passes this longer timeout explicitly via graphql_request(timeout=)
# so we don't fail on long-but-still-progressing requests
REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT = 600
DEFAULT_OUTPUT_FILE = "repos.csv"
DEFAULT_CLONING_ERRORS_FILE = "repos-with-cloning-errors.csv"
DEFAULT_INDEXING_ERRORS_FILE = "repos-with-indexing-errors.csv"
DEFAULT_SKIPPED_FILES_FILE = "repos-with-skipped-files.csv"
DEFAULT_STATS_FILE_PREFIX = "stats"
DEFAULT_LOG_FILE = "list-repos.log"
DEFAULT_CSV_SCHEMA_FILE = "CSV_SCHEMA.md"


# --- GraphQL queries ----------------------------------------------------------

# Shared fields for full-listing and single-repo queries
REPO_NODE_FRAGMENT = """
fragment RepoNodeFields on Repository {
  name
  id
  url
  isFork
  isArchived
  isPrivate
  createdAt
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
      contentFilesCount
      contentByteSize
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
  externalServices(first: 100) @include(if: $includeExternalServices) {
    nodes {
      displayName
    }
  }
}
"""

# Non-admin tokens set $includeExternalServices=false to skip admin-only fields
GRAPHQL_QUERY = (
    REPO_NODE_FRAGMENT
    + """
query ListRepos($first: Int!, $after: String, $includeExternalServices: Boolean!) {
  repositories(first: $first, after: $after) {
    nodes {
      ...RepoNodeFields
    }
    totalCount
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""
)

# Single-repo lookup used by the scoped variants of --count-commits / --reclone
# / --reindex. Returns the same field set as the listing query (via the shared
# fragment) so the rest of the pipeline (build_row, write_csv, the error/skip
# detectors, etc.) can treat the result identically to a listing-page node
SINGLE_REPO_QUERY = (
    REPO_NODE_FRAGMENT
    + """
query SingleRepo($name: String!, $includeExternalServices: Boolean!) {
  repository(name: $name) {
    ...RepoNodeFields
  }
}
"""
)

# Used once at startup to gate admin-only fields and mutations
CURRENT_USER_QUERY = """
query { currentUser { username siteAdmin } }
"""

# Per-repo query for exact rev count, cleanup metadata, and all-refs proxy
# Omitting ancestors.first asks gitserver for the full reachable commit count
COMMIT_COUNT_QUERY = """
query CommitCount($name: String!, $rev: String!, $allRefsSearch: String!) {
  repository(name: $name) {
    commit(rev: $rev) {
      ancestors {
        totalCount
      }
    }
    mirrorInfo {
      lastCleanedAt
      cleanupSchedule {
        due
        intervalSeconds
      }
      cleanupQueue {
        index
        optimizing
      }
      repositoryStatistics {
        packfiles {
          lastFullRepack
        }
      }
    }
  }
  search(query: $allRefsSearch, version: V3) {
    results {
      matchCount
    }
  }
}
"""

# Approximate all-refs count. Not comparable to the exact rev count
# Repo anchoring, regex escaping, and timeout prevent slow unbounded searches
ALL_REFS_COMMIT_SEARCH_TEMPLATE = (
    "r:^{repo}$ rev:*refs/heads/*:*refs/tags/* type:commit count:all timeout:120s"
)


def build_all_refs_search(repo_name: str) -> str:
    """Build the SG search query that counts commits across all branches+tags"""
    return ALL_REFS_COMMIT_SEARCH_TEMPLATE.format(repo=re.escape(repo_name))


# --- Per-repo arbitrary search (--run-search) ---------------------------------

# Wrap the user's pattern with a repo anchor, count:all, and a server timeout
RUN_SEARCH_QUERY_TEMPLATE = "r:^{repo}$ {pattern} count:all timeout:120s"

RUN_SEARCH_GRAPHQL = """
query RunSearch($query: String!) {
  search(query: $query, version: V3) {
    results {
      matchCount
      limitHit
      alert {
        title
      }
    }
  }
}
"""


def build_run_search_query(repo_name: str, pattern: str) -> str:
    """Build a per-repo --run-search query while leaving pattern syntax verbatim"""
    return RUN_SEARCH_QUERY_TEMPLATE.format(
        repo=re.escape(repo_name),
        pattern=pattern,
    )


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
    """Decode Sourcegraph's base64 repo ID to its integer form"""
    return int(base64.b64decode(base64_id).decode().split(":", 1)[1])


def get_path(repo: dict[str, Any], path: str) -> object | None:
    """Walk a dotted dict path; return None if any step is missing"""
    current: object = repo
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        # cast keeps strict type-checkers happy: isinstance() on `object` only
        # narrows to dict[Unknown, Unknown], so we re-view it concretely
        current_dict = cast("dict[str, object]", current)
        next_value = current_dict.get(key)
        if next_value is None:
            return None
        current = next_value
    return current


def get_path_mb(repo: dict[str, Any], path: str) -> int | None:
    """Like get_path, but convert to megabytes"""
    value = get_path(repo, path)
    if isinstance(value, (int, str)):
        return int(value) // (1024 * 1024)
    return None


def derive_mirror_status(repo: dict[str, Any]) -> str:
    """Summarize the repo's mirror state into a single status string"""
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


def seconds_relative_to_now(timestamp: object, *, future: bool) -> int | None:
    """Return seconds since/until an RFC3339 timestamp, or None if invalid"""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    delta = (ts - now) if future else (now - ts)
    return int(delta.total_seconds())


def derive_index_status(repo: dict[str, Any]) -> str:
    """Summarize the repo's search-index state as 'indexed' or 'not_indexed'"""
    return (
        "indexed"
        if get_path(repo, "textSearchIndex.status") is not None
        else "not_indexed"
    )


def redact_remote_url(repo: dict[str, Any]) -> str | None:
    """Redact mirrorInfo.remoteURL userinfo before it reaches any CSV output"""
    raw = get_path(repo, "mirrorInfo.remoteURL")
    if not isinstance(raw, str):
        return None
    if not raw:
        return raw
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc or "@" not in parts.netloc:
        return raw
    _, _, host_port = parts.netloc.rpartition("@")
    new_netloc = f"REDACTED@{host_port}"
    return urlunsplit(
        (parts.scheme, new_netloc, parts.path, parts.query, parts.fragment),
    )


def join_external_services(repo: dict[str, Any]) -> str:
    """Combine all attached code-host display names into one ';'-separated string"""
    services: dict[str, Any] = repo.get("externalServices") or {}
    nodes: list[dict[str, Any]] = services.get("nodes") or []
    return "; ".join(str(es["displayName"]) for es in nodes)


def join_corruption_logs(repo: dict[str, Any]) -> str:
    """Flatten corruptionLogs into a ';'-separated 'timestamp: reason' string"""
    mirror: dict[str, Any] = repo.get("mirrorInfo") or {}
    logs: list[dict[str, Any]] = mirror.get("corruptionLogs") or []
    return "; ".join(
        f"{log.get('timestamp', '')}: {log.get('reason', '')}" for log in logs
    )


def truncate_sync_output(repo: dict[str, Any]) -> str | None:
    """Return lastSyncOutput truncated to first 5 + last 5 lines"""
    value = get_path(repo, "mirrorInfo.lastSyncOutput")
    if not isinstance(value, str):
        return None
    return truncate_lines(value)


def truncate_lines(value: str, head: int = 5, tail: int = 5) -> str:
    """Truncate a multi-line string to the first `head` + last `tail` lines"""
    lines = value.splitlines()
    if len(lines) <= head + tail:
        return value
    omitted = len(lines) - head - tail
    return "\n".join(
        [*lines[:head], f"... [{omitted} lines truncated] ...", *lines[-tail:]],
    )


def has_cloning_error(repo: dict[str, Any]) -> bool:
    """Return True for errored, corrupted, or not-yet-cloned repos"""
    return derive_mirror_status(repo) in {"errored", "corrupted", "not_cloned"}


def has_indexing_error(repo: dict[str, Any]) -> bool:
    """Return True for cloned repos missing a search index"""
    return (
        derive_mirror_status(repo) == "cloned"
        and get_path(repo, "textSearchIndex.status") is None
    )


def _index_refs(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """Return textSearchIndex.refs (or [] when missing)"""
    index: dict[str, Any] = repo.get("textSearchIndex") or {}
    refs: list[dict[str, Any]] = index.get("refs") or []
    return refs


def total_skipped_files(repo: dict[str, Any]) -> int:
    """Sum skippedIndexed.count across every indexed ref of the repo"""
    total = 0
    for ref in _index_refs(repo):
        skipped: dict[str, Any] = ref.get("skippedIndexed") or {}
        count = skipped.get("count")
        if count is not None:
            total += int(count)
    return total


def refs_with_skips(repo: dict[str, Any]) -> str:
    """Return ';'-joined '<refName>=<count>' for refs with skipped files"""
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
    """Return skippedIndexed.query for HEAD, or the first skipped ref"""
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
    """Return True if zoekt skipped at least one file for this repo"""
    return total_skipped_files(repo) > 0


def fetch_commit_count(
    endpoint: str,
    token: str,
    repo_name: str,
    rev: str = "HEAD",
) -> tuple[int | None, int | None, float, list[Any]]:
    """Return exact rev count, approximate all-refs count, elapsed time, extras"""
    empty_extras: list[Any] = [None] * len(COMMIT_COUNT_OPTIMIZATION_COLUMNS)
    start = time.monotonic()
    try:
        data = graphql_request(
            endpoint,
            token,
            COMMIT_COUNT_QUERY,
            {
                "name": repo_name,
                "rev": rev,
                "allRefsSearch": build_all_refs_search(repo_name),
            },
            timeout=REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT,
        )
    except (GraphQLError, HTTPRequestError) as exc:
        elapsed = time.monotonic() - start
        logger.warning("commit-count query failed for %s: %s", repo_name, exc)
        return None, None, elapsed, empty_extras
    except OSError as exc:
        elapsed = time.monotonic() - start
        logger.warning(
            "commit-count network error for %s: %s",
            repo_name,
            exc,
        )
        return None, None, elapsed, empty_extras
    elapsed = time.monotonic() - start
    repo: dict[str, Any] = data.get("repository") or {}
    commit: dict[str, Any] = repo.get("commit") or {}
    ancestors: dict[str, Any] = commit.get("ancestors") or {}
    default_count_raw = ancestors.get("totalCount")
    default_count: int | None = (
        default_count_raw if isinstance(default_count_raw, int) else None
    )
    search_block: dict[str, Any] = data.get("search") or {}
    search_results: dict[str, Any] = search_block.get("results") or {}
    all_refs_count_raw = search_results.get("matchCount")
    all_refs_count: int | None = (
        all_refs_count_raw if isinstance(all_refs_count_raw, int) else None
    )
    optimization_values = [
        extract(repo) for _, extract, _, _, _ in COMMIT_COUNT_OPTIMIZATION_COLUMNS
    ]
    return default_count, all_refs_count, elapsed, optimization_values


def fetch_run_search(
    endpoint: str,
    token: str,
    repo_name: str,
    pattern: str,
) -> tuple[int | None, float, bool, str | None]:
    """Return --run-search match count, elapsed time, limit flag, and alert"""
    start = time.monotonic()
    query = build_run_search_query(repo_name, pattern)
    try:
        data = graphql_request(endpoint, token, RUN_SEARCH_GRAPHQL, {"query": query})
    except (GraphQLError, HTTPRequestError) as exc:
        elapsed = time.monotonic() - start
        logger.warning("run-search query failed for %s: %s", repo_name, exc)
        return None, elapsed, False, None
    except OSError as exc:
        elapsed = time.monotonic() - start
        logger.warning("run-search network error for %s: %s", repo_name, exc)
        return None, elapsed, False, None
    elapsed = time.monotonic() - start
    search_block: dict[str, Any] = data.get("search") or {}
    results: dict[str, Any] = search_block.get("results") or {}
    raw_count = results.get("matchCount")
    match_count: int | None = raw_count if isinstance(raw_count, int) else None
    limit_hit = bool(results.get("limitHit"))
    alert: dict[str, Any] = results.get("alert") or {}
    alert_title_raw = alert.get("title")
    alert_title: str | None = (
        alert_title_raw if isinstance(alert_title_raw, str) else None
    )
    return match_count, elapsed, limit_hit, alert_title


# --- CSV format -----------------------------------------------------------

# Each entry is (csv_column_name, extractor_function). Keeping the column name
# next to the function that produces its value eliminates the risk of the
# header drifting out of sync with the row data
COLUMNS: list[tuple[str, Callable[[dict[str, Any]], Any], str, bool, str]] = [
    (
        "id",
        lambda r: decode_repo_id(r["id"]),
        "Numeric Sourcegraph database ID for the repository, decoded "
        "locally from the base64 GraphQL global ID; useful when correlating "
        "with the `repo` table or admin URLs",
        False,
        "integer",
    ),
    (
        "url",
        lambda r: r.get("url"),
        "URL to the repository on this Sourcegraph instance",
        False,
        "string",
    ),
    (
        "mirrorInfo.remoteURL",
        redact_remote_url,
        "Clone URL of the upstream repository on the code host",
        True,
        "string",
    ),
    (
        "externalServices",
        join_external_services,
        "Display names of the external service(s) which clone this repository",
        True,
        "string (semicolon-joined)",
    ),
    (
        "mirrorInfo.status",
        derive_mirror_status,
        "Single-word summary of the repo's mirror state, derived locally "
        "from `mirrorInfo`",
        False,
        "enum (corrupted, errored, cloning, cloned, not_cloned)",
    ),
    (
        "isFork",
        lambda r: r.get("isFork"),
        "Whether this repository is a fork",
        False,
        "boolean",
    ),
    (
        "isArchived",
        lambda r: r.get("isArchived"),
        "Whether this repository has been archived on the code host",
        False,
        "boolean",
    ),
    (
        "isPrivate",
        lambda r: r.get("isPrivate"),
        "Whether this repository is private",
        False,
        "boolean",
    ),
    (
        "mirrorInfo.byteSize(MB)",
        lambda r: get_path_mb(r, "mirrorInfo.byteSize"),
        "On-disk size of the bare-cloned repository, in megabytes",
        False,
        "float",
    ),
    (
        "createdAt",
        lambda r: r.get("createdAt"),
        "Timestamp the repo was first cloned to your Sourcegraph instance",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.lastChanged",
        lambda r: get_path(r, "mirrorInfo.lastChanged"),
        "Timestamp of the most recent commit in the repo",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.updatedAt",
        lambda r: get_path(r, "mirrorInfo.updatedAt"),
        "Timestamp of the most recent successful sync of the repo from the code host",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.secondsSinceUpdatedAt",
        lambda r: seconds_relative_to_now(
            get_path(r, "mirrorInfo.updatedAt"),
            future=False,
        ),
        "Integer seconds elapsed between `mirrorInfo.updatedAt` and when the script was run",
        False,
        "integer",
    ),
    (
        "mirrorInfo.nextSyncAt",
        lambda r: get_path(r, "mirrorInfo.nextSyncAt"),
        "Timestamp the repo is next scheduled to be synced from upstream",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.secondsUntilNextSyncAt",
        lambda r: seconds_relative_to_now(
            get_path(r, "mirrorInfo.nextSyncAt"),
            future=True,
        ),
        "Integer seconds remaining until `mirrorInfo.nextSyncAt`",
        False,
        "integer",
    ),
    (
        "mirrorInfo.updateSchedule.intervalSeconds",
        lambda r: get_path(r, "mirrorInfo.updateSchedule.intervalSeconds"),
        "Interval, in seconds, between scheduled mirror updates. Default max is 28800 seconds (8 hours), but is shortened for busy / popular repos",
        False,
        "integer",
    ),
    (
        "mirrorInfo.shard",
        lambda r: get_path(r, "mirrorInfo.shard"),
        "Pod name of the gitserver shard which holds this repo's clone",
        True,
        "string",
    ),
    (
        "textSearchIndex.status",
        derive_index_status,
        "Search-index state, derived locally: "
        "`indexed` if Zoekt has built an index for this repo, "
        "`not_indexed` otherwise",
        False,
        "enum (indexed, not_indexed)",
    ),
    (
        "textSearchIndex.status.updatedAt",
        lambda r: get_path(r, "textSearchIndex.status.updatedAt"),
        "Timestamp the repo was last indexed for fast search. It should be shortly after mirrorInfo.lastChanged, as indexing jobs are scheduled after new commits are fetched",
        False,
        "timestamp",
    ),
    (
        "textSearchIndex.status.contentFilesCount",
        lambda r: get_path(r, "textSearchIndex.status.contentFilesCount"),
        "Number of files included in the index. Note that some files are excluded from indexing, ex. binary files",
        False,
        "integer",
    ),
    (
        "textSearchIndex.status.contentByteSize(MB)",
        lambda r: get_path_mb(r, "textSearchIndex.status.contentByteSize"),
        "Size, in megabytes, of the source content that was indexed. Note that some files are excluded from indexing, ex. binary files",
        False,
        "float",
    ),
    (
        "textSearchIndex.status.indexByteSize(MB)",
        lambda r: get_path_mb(r, "textSearchIndex.status.indexByteSize"),
        "Size of the Zoekt search index for this repo, in megabytes",
        False,
        "float",
    ),
    (
        "textSearchIndex.status.indexShardsCount",
        lambda r: get_path(r, "textSearchIndex.status.indexShardsCount"),
        "Number of Zoekt shards that make up this repo's index",
        False,
        "integer",
    ),
    (
        "textSearchIndex.status.newLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.newLinesCount"),
        "Total number of lines across every indexed branch",
        False,
        "integer",
    ),
    (
        "textSearchIndex.status.defaultBranchNewLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.defaultBranchNewLinesCount"),
        "Number of lines indexed on the repo's default branch",
        False,
        "integer",
    ),
    (
        "textSearchIndex.status.otherBranchesNewLinesCount",
        lambda r: get_path(r, "textSearchIndex.status.otherBranchesNewLinesCount"),
        "Number of lines indexed across non-default branches",
        False,
        "integer",
    ),
    (
        "textSearchIndex.host.name",
        lambda r: get_path(r, "textSearchIndex.host.name"),
        "Pod name of the indexserver shard which holds this repo's index",
        False,
        "string",
    ),
]

CSV_COLUMNS = [name for name, _, _, _, _ in COLUMNS]
URL_COLUMN_INDEX = CSV_COLUMNS.index("url")

# Cleanup metadata appended only when --count-commits runs its per-repo query
# repositoryStatistics may be empty for non-admin tokens or non-cloned repos
COMMIT_COUNT_OPTIMIZATION_COLUMNS: list[
    tuple[str, Callable[[dict[str, Any]], Any], str, bool, str]
] = [
    (
        "mirrorInfo.lastCleanedAt",
        lambda r: get_path(r, "mirrorInfo.lastCleanedAt"),
        "Timestamp of the last successful gitserver cleanup ('gc') of this repo",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.cleanupSchedule.due",
        lambda r: get_path(r, "mirrorInfo.cleanupSchedule.due"),
        "Timestamp the repo is next scheduled to be cleaned up by gitserver",
        False,
        "timestamp",
    ),
    (
        "mirrorInfo.cleanupSchedule.intervalSeconds",
        lambda r: get_path(r, "mirrorInfo.cleanupSchedule.intervalSeconds"),
        "Interval, in seconds, between scheduled cleanup runs",
        False,
        "integer",
    ),
    (
        "mirrorInfo.cleanupQueue.index",
        lambda r: get_path(r, "mirrorInfo.cleanupQueue.index"),
        "Position of the repo in the gitserver cleanup queue",
        False,
        "integer",
    ),
    (
        "mirrorInfo.cleanupQueue.optimizing",
        lambda r: get_path(r, "mirrorInfo.cleanupQueue.optimizing"),
        "Whether gitserver is currently running optimization on this repo",
        False,
        "boolean",
    ),
    (
        "mirrorInfo.repositoryStatistics.packfiles.lastFullRepack",
        lambda r: get_path(
            r,
            "mirrorInfo.repositoryStatistics.packfiles.lastFullRepack",
        ),
        "Timestamp of the most recent full repack of this repo's packfiles",
        True,
        "timestamp",
    ),
]

# Optional --count-commits columns appended to each per-repo CSV
COMMIT_COUNT_COLUMNS: list[tuple[str, str, bool, str]] = [
    (
        "defaultBranch.target.commit.ancestors.totalCount",
        "Number of commits reachable from HEAD on the default branch — "
        "equivalent to `git rev-list --count HEAD`, computed by gitserver",
        False,
        "integer",
    ),
    (
        "allRefs.search.matchCount",
        "Approximate number of commits across every branch, "
        "computed via Sourcegraph's commit-search API",
        False,
        "integer",
    ),
    (
        "commitCount.queryTimeSeconds",
        "Wall-clock seconds the per-repo commit-count GraphQL request "
        "took. Useful for spotting which repos are expensive to count",
        False,
        "float",
    ),
    *(
        (name, desc, admin, vtype)
        for name, _, desc, admin, vtype in COMMIT_COUNT_OPTIMIZATION_COLUMNS
    ),
]

# Optional --run-search columns appended after --count-commits columns
RUN_SEARCH_COLUMNS: list[tuple[str, str, bool, str]] = [
    (
        "runSearch.matchCount",
        "Number of search matches the Sourcegraph search API reported "
        "for the user-supplied `--run-search` pattern, for this repo",
        False,
        "integer",
    ),
    (
        "runSearch.queryTimeSeconds",
        "Wall-clock seconds the per-repo `--run-search` GraphQL request took",
        False,
        "float",
    ),
    (
        "runSearch.limitHit",
        "`True` when the search hit a limit, so the results are incomplete",
        False,
        "boolean",
    ),
    (
        "runSearch.alertTitle",
        "Title of the search-API alert when the server's `timeout:` "
        "budget was exceeded or the query was malformed",
        False,
        "string",
    ),
]

# Extra columns appended only to the cloning-errors CSV
CLONING_ERROR_EXTRA_COLUMNS: list[
    tuple[str, Callable[[dict[str, Any]], Any], str, bool, str]
] = [
    (
        "mirrorInfo.isCorrupted",
        lambda r: get_path(r, "mirrorInfo.isCorrupted"),
        "Whether Sourcegraph has detected the on-disk clone is corrupted",
        False,
        "boolean",
    ),
    (
        "mirrorInfo.lastError",
        lambda r: get_path(r, "mirrorInfo.lastError"),
        "Last error message returned by gitserver while fetching or "
        "cloning this repo, if any",
        False,
        "string",
    ),
    (
        "mirrorInfo.lastSyncOutput",
        truncate_sync_output,
        "Output of the most recent sync attempt, truncated to the first 5 and last 5 lines",
        False,
        "string",
    ),
    (
        "mirrorInfo.corruptionLogs",
        join_corruption_logs,
        "`timestamp: reason` entries for the most recent corruption events",
        False,
        "string (semicolon-joined)",
    ),
]
CLONING_ERROR_CSV_COLUMNS = CSV_COLUMNS + [
    name for name, _, _, _, _ in CLONING_ERROR_EXTRA_COLUMNS
]
# The indexing-errors CSV reuses CSV_COLUMNS verbatim — Sourcegraph's GraphQL
# does not expose any per-repo zoekt error fields beyond textSearchIndex.status

# Extra columns appended only to the skipped-files CSV. The query is the
# Sourcegraph search query produced by the API; running it lists each skipped
# file along with its NOT-INDEXED reason (too-large / binary / too-many-trigrams
# / too-small / blob-missing)
SKIPPED_FILES_EXTRA_COLUMNS: list[
    tuple[str, Callable[[dict[str, Any]], Any], str, bool, str]
] = [
    (
        "skippedIndexed.totalCount",
        total_skipped_files,
        "Count of files Zoekt excluded while indexing this repo",
        False,
        "integer",
    ),
    (
        "skippedIndexed.refsWithSkips",
        refs_with_skips,
        "`<refName>=<count>` entries for every indexed ref which "
        "has at least one excluded file",
        False,
        "string (semicolon-joined)",
    ),
    (
        "skippedIndexed.headQuery",
        head_skipped_query,
        "Sourcegraph search query that lists every excluded file on HEAD. "
        "This search is run when the script is run with the --skipped-files-reason arg",
        False,
        "string",
    ),
]
SKIPPED_FILES_CSV_COLUMNS = CSV_COLUMNS + [
    name for name, _, _, _, _ in SKIPPED_FILES_EXTRA_COLUMNS
]


# --- Statistics ---------------------------------------------------------------

# --statistics buckets repo/content/index sizes and size ratios during listing

# (label, lo_inclusive_mb, hi_exclusive_mb_or_None) — used for the repo and
# indexed-content size distributions, which span many orders of magnitude
SIZE_BUCKETS_MB: list[tuple[str, int, int | None]] = [
    ("0-1 MB", 0, 1),
    ("1 MB - 1 GB", 1, 1024),
    ("1-10 GB", 1024, 10 * 1024),
    ("10-100 GB", 10 * 1024, 100 * 1024),
    (">100 GB", 100 * 1024, None),
]

# Search indexes are typically much smaller than the source they index, so a
# narrower set of buckets is more useful here than reusing SIZE_BUCKETS_MB
INDEX_SIZE_BUCKETS_MB: list[tuple[str, int, int | None]] = [
    ("0-1 MB", 0, 1),
    ("1-10 MB", 1, 10),
    ("10-100 MB", 10, 100),
    (">100 MB", 100, None),
]

# Used for both content/mirror and index/content ratio distributions. The
# >100% bucket isn't a logic bug — content can exceed the bare clone size
# when the bare clone is heavily packed, and the index can briefly exceed
# the content size on small repos due to per-shard overhead
PERCENT_BUCKETS: list[tuple[str, float, float | None]] = [
    ("0-10%", 0, 10),
    ("10-25%", 10, 25),
    ("25-50%", 25, 50),
    ("50-75%", 50, 75),
    ("75-100%", 75, 100),
    ("100-150%", 100, 150),
    (">150%", 150, None),
]


def bucket_label(
    value: float,
    buckets: list[tuple[str, float, float | None]] | list[tuple[str, int, int | None]],
) -> str | None:
    """Return the label of the first bucket that contains `value`, or None"""
    for label, lo, hi in buckets:
        if value >= lo and (hi is None or value < hi):
            return label
    return None


class StatsCollector:
    """Accumulate per-repo size and ratio counts for --statistics"""

    def __init__(self) -> None:
        self.mirror_buckets: collections.Counter[str] = collections.Counter()
        self.content_buckets: collections.Counter[str] = collections.Counter()
        self.index_buckets: collections.Counter[str] = collections.Counter()
        self.content_vs_mirror_buckets: collections.Counter[str] = collections.Counter()
        self.index_vs_content_buckets: collections.Counter[str] = collections.Counter()
        self.cloned_count = 0
        self.cloned_total_mb = 0
        self.content_count = 0
        self.content_total_mb = 0
        self.indexed_count = 0
        self.indexed_total_mb = 0

    def add(self, repo: dict[str, Any]) -> None:
        """Update every counter from a single repo's size fields"""
        mirror_mb = get_path_mb(repo, "mirrorInfo.byteSize")
        content_mb = get_path_mb(repo, "textSearchIndex.status.contentByteSize")
        index_mb = get_path_mb(repo, "textSearchIndex.status.indexByteSize")

        # Restrict the mirror size distribution to repos which actually have
        # a clone on disk; reporting `not_cloned` repos under "0-1 MB" would
        # blur "tiny repo" with "missing clone" in the same bucket
        if mirror_mb is not None and derive_mirror_status(repo) == "cloned":
            self.cloned_count += 1
            self.cloned_total_mb += mirror_mb
            label = bucket_label(mirror_mb, SIZE_BUCKETS_MB)
            if label is not None:
                self.mirror_buckets[label] += 1

        # Both content and index sizes only exist on repos that have a search
        # index, so presence of the underlying field is the right gate
        if content_mb is not None:
            self.content_count += 1
            self.content_total_mb += content_mb
            label = bucket_label(content_mb, SIZE_BUCKETS_MB)
            if label is not None:
                self.content_buckets[label] += 1

        if index_mb is not None:
            self.indexed_count += 1
            self.indexed_total_mb += index_mb
            label = bucket_label(index_mb, INDEX_SIZE_BUCKETS_MB)
            if label is not None:
                self.index_buckets[label] += 1

        # Skip the ratio buckets when either operand is missing or the
        # denominator floored to 0 MB (the result would be undefined / inf)
        if content_mb is not None and mirror_mb is not None and mirror_mb > 0:
            pct = (content_mb / mirror_mb) * 100
            label = bucket_label(pct, PERCENT_BUCKETS)
            if label is not None:
                self.content_vs_mirror_buckets[label] += 1

        if index_mb is not None and content_mb is not None and content_mb > 0:
            pct = (index_mb / content_mb) * 100
            label = bucket_label(pct, PERCENT_BUCKETS)
            if label is not None:
                self.index_vs_content_buckets[label] += 1


# Per-stat output metadata: suffix, description, buckets, counter, summary rows
STATS_FILES: list[
    tuple[
        str,
        str,
        list[tuple[str, int, int | None]] | list[tuple[str, float, float | None]],
        str,
        Callable[[StatsCollector], list[tuple[str, Any]]],
    ]
] = [
    (
        "mirror-byte-size",
        "Distribution of cloned repos by `mirrorInfo.byteSize` (MB)",
        SIZE_BUCKETS_MB,
        "mirror_buckets",
        lambda s: [
            ("TOTAL_CLONED_REPOS", s.cloned_count),
            ("TOTAL_CLONED_SIZE_MB", s.cloned_total_mb),
        ],
    ),
    (
        "content-byte-size",
        "Distribution of indexed repos by `textSearchIndex.status.contentByteSize` (MB)",
        SIZE_BUCKETS_MB,
        "content_buckets",
        lambda s: [
            ("TOTAL_INDEXED_REPOS", s.content_count),
            ("TOTAL_CONTENT_SIZE_MB", s.content_total_mb),
        ],
    ),
    (
        "index-byte-size",
        "Distribution of indexed repos by `textSearchIndex.status.indexByteSize` (MB)",
        INDEX_SIZE_BUCKETS_MB,
        "index_buckets",
        lambda s: [
            ("TOTAL_INDEXED_REPOS", s.indexed_count),
            ("TOTAL_INDEX_SIZE_MB", s.indexed_total_mb),
        ],
    ),
    (
        "content-vs-mirror-pct",
        "Distribution of `contentByteSize / mirrorInfo.byteSize` (as a percentage)",
        PERCENT_BUCKETS,
        "content_vs_mirror_buckets",
        lambda s: [("TOTAL_REPOS", sum(s.content_vs_mirror_buckets.values()))],
    ),
    (
        "index-vs-content-pct",
        "Distribution of `indexByteSize / contentByteSize` (as a percentage)",
        PERCENT_BUCKETS,
        "index_vs_content_buckets",
        lambda s: [("TOTAL_REPOS", sum(s.index_vs_content_buckets.values()))],
    ),
]


def write_stats(prefix: str, stats: StatsCollector) -> list[Path]:
    """Write one bucket/count CSV per stat and return the paths written"""
    written: list[Path] = []
    for suffix, _desc, buckets, attr, summary_builder in STATS_FILES:
        path = Path(f"{prefix}-{DEFAULT_STATS_FILE_PREFIX}-{suffix}.csv")
        counter: collections.Counter[str] = getattr(stats, attr)
        with path.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["bucket", "count"])
            for label, _lo, _hi in buckets:
                writer.writerow([label, counter.get(label, 0)])
            for metric, value in summary_builder(stats):
                writer.writerow([metric, value])
        written.append(path)
    return written


# --- CSV schema generation ----------------------------------------------------

# CSV_SCHEMA.md is generated from the same tuples that define CSV output


def format_columns_list(columns: list[tuple[str, str, bool, str]]) -> str:
    """Render column metadata as a Markdown table"""
    rows = [
        table_row("Column", "Type", "Requires admin", "Description"),
        table_row("---", "---", "---", "---"),
    ]
    for name, desc, requires_admin, value_type in columns:
        admin_cell = "true" if requires_admin else ""
        # Defensive: escape pipes so a description never breaks the table
        desc_cell = desc.replace("|", "\\|")
        rows.append(table_row(f"`{name}`", value_type, admin_cell, desc_cell))
    return "\n".join(rows)


def table_row(*cells: str) -> str:
    """Format a Markdown table row, using `| |` for empty cells"""
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def name_desc(
    columns: list[tuple[str, Callable[[dict[str, Any]], Any], str, bool, str]],
) -> list[tuple[str, str, bool, str]]:
    """Drop extractor functions from column metadata"""
    return [
        (name, desc, requires_admin, value_type)
        for name, _, desc, requires_admin, value_type in columns
    ]


def format_stats_files_list() -> str:
    """Render STATS_FILES as a Markdown table for CSV_SCHEMA.md"""
    rows = [
        table_row("File suffix", "Buckets", "Description"),
        table_row("---", "---", "---"),
    ]
    for suffix, desc, buckets, _attr, _summary in STATS_FILES:
        bucket_cell = ", ".join(label for label, _, _ in buckets)
        # Defensive: escape pipes so a description never breaks the table
        rows.append(
            table_row(
                f"`{DEFAULT_STATS_FILE_PREFIX}-{suffix}.csv`",
                bucket_cell,
                desc.replace("|", "\\|"),
            ),
        )
    return "\n".join(rows)


def write_csv_schema(path: Path) -> None:
    """Write CSV_SCHEMA.md from the in-script column tables"""
    main_list = format_columns_list(name_desc(COLUMNS))
    cloning_list = format_columns_list(name_desc(CLONING_ERROR_EXTRA_COLUMNS))
    skipped_list = format_columns_list(name_desc(SKIPPED_FILES_EXTRA_COLUMNS))
    commit_count_list = format_columns_list(COMMIT_COUNT_COLUMNS)
    run_search_list = format_columns_list(RUN_SEARCH_COLUMNS)
    stats_files_list = format_stats_files_list()

    content = f"""# `list-repos.py` CSV column reference

- This file is generated by `python3 list-repos.py --write-csv-schema`
- It documents every column in each of its CSV output files
- Columns where `Requires admin` is `true` are from GraphQL fields
which require an access token from a site admin user on the instance
  - When you run the script with an access token from a non-admin user,
    these columns will be empty
- Every other column is populated for any authenticated user with read access
to the repository

## Output files

The script prefixes output file names with the sanitized Sourcegraph endpoint
(e.g. `sourcegraph.example.com-repos.csv`),
so the script can run against multiple instances without overwriting files

| File | Written when | Columns |
| --- | --- | --- |
| `<prefix>-{DEFAULT_OUTPUT_FILE}` | always | main columns |
| `<prefix>-{DEFAULT_CLONING_ERRORS_FILE}` | at least one repo has a cloning error | main columns + cloning-error extras |
| `<prefix>-{DEFAULT_INDEXING_ERRORS_FILE}` | at least one repo is cloned but is missing a search index | main columns |
| `<prefix>-{DEFAULT_SKIPPED_FILES_FILE}` | `--skipped-files` is set and the last index excluded some files | main columns + skipped-files extras |
| `<prefix>-{DEFAULT_STATS_FILE_PREFIX}-*.csv` | `--statistics` is set | `bucket,count` (see Statistics section) |

The optional `--count-commits` and `--run-search` flags append extra
columns to *every* CSV listed above (except the `--statistics` files,
which are summaries rather than per-repo rows), in this order: main
columns → per-CSV extras → commit-count columns → run-search columns

## Main columns

These are written to every CSV file

{main_list}

## Cloning-error extras

Appended to `<prefix>-{DEFAULT_CLONING_ERRORS_FILE}`

{cloning_list}

## Skipped-files extras

Appended to `<prefix>-{DEFAULT_SKIPPED_FILES_FILE}`

{skipped_list}

## `--count-commits` columns

Appended to CSV files when `--count-commits` is used

{commit_count_list}

## `--run-search` columns

Appended to CSV files when `--run-search PATTERN` is used

{run_search_list}

## `--statistics` files

- Written when `--statistics` is used
- One CSV file per dimension
- Each file has two columns listing every bucket in declaration
order, followed by per-stat summary rows (totals) appended below the
bucket rows
- Counts come from the same listing pass that produces the
main CSV, so enabling `--statistics` adds no extra GraphQL requests

{stats_files_list}

"""
    path.write_text(content, encoding="utf-8")


# --- HTTP / GraphQL plumbing --------------------------------------------------


class GraphQLError(RuntimeError):
    """Raised when the Sourcegraph GraphQL API returns errors"""


class HTTPRequestError(RuntimeError):
    """Raised when the server returns a definitive 4xx/5xx HTTP response"""

    def __init__(
        self,
        status: int,
        reason: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        """Capture the response status, headers, and body for later logging"""
        super().__init__(f"HTTP {status} {reason}")
        self.status = status
        self.reason = reason
        self.url = url
        self.headers = headers
        self.body = body


def open_connection(
    parsed: ParseResult,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> http.client.HTTPConnection:
    """Open an HTTP(S) connection and reject other URL schemes"""
    if not parsed.hostname:
        msg = f"URL is missing a hostname: {parsed.geturl()!r}"
        raise ValueError(msg)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
        )
    if parsed.scheme == "http":
        return http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
        )
    msg = f"Unsupported URL scheme: {parsed.scheme!r} (expected http or https)"
    raise ValueError(msg)


def send_once(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send one POST. Returns parsed JSON on 2xx, raises HTTPRequestError on 4xx/5xx"""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = open_connection(parsed, timeout=timeout)
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
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | OSError:
    """Send once; return OSError instances so callers can retry them"""
    try:
        return send_once(url, body, headers, timeout=timeout)
    except OSError as exc:
        return exc


def send_with_retry(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Retry socket-level failures; propagate definitive HTTP errors"""
    for attempt in range(1, MAX_RETRIES + 1):
        result = send_or_capture_oserror(url, body, headers, timeout=timeout)
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
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send a GraphQL query to the Sourcegraph API and return the data block"""
    url = endpoint.rstrip("/") + "/.api/graphql"
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
        "User-Agent": "list-repos/0.0.1",
    }
    data = send_with_retry(url, body, headers, timeout=timeout)
    if data.get("errors"):
        # GraphQL can return both `errors` and partial `data`. If we have data,
        # log the errors and keep going; only abort if no data was returned
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


def fetch_current_user(endpoint: str, token: str) -> tuple[str, bool]:
    """Return the authenticated username and site-admin flag"""
    data = graphql_request(endpoint, token, CURRENT_USER_QUERY, {})
    user: dict[str, Any] = data["currentUser"] or {}
    return str(user["username"]), bool(user.get("siteAdmin"))


def fetch_single_repo(
    endpoint: str,
    token: str,
    repo_name: str,
    *,
    is_site_admin: bool,
) -> dict[str, Any]:
    """Fetch one repo node in listing-query shape, respecting admin-only fields"""
    data = graphql_request(
        endpoint,
        token,
        SINGLE_REPO_QUERY,
        {"name": repo_name, "includeExternalServices": is_site_admin},
    )
    repo = data.get("repository")
    if repo is None:
        die(f"repository {repo_name!r} not found on {endpoint}")
    return cast("dict[str, Any]", repo)


def trigger_reclone(endpoint: str, token: str, repo_id: str) -> bool:
    """Send recloneRepository mutation. Returns True on success, False on GraphQL error"""
    try:
        graphql_request(endpoint, token, RECLONE_MUTATION, {"repo": repo_id})
    except (GraphQLError, HTTPRequestError) as exc:
        logger.warning("recloneRepository failed for %s: %s", repo_id, exc)
        return False
    return True


def trigger_reindex(endpoint: str, token: str, repo_id: str) -> bool:
    """Send reindexRepository mutation. Returns True on success, False on GraphQL error"""
    try:
        graphql_request(endpoint, token, REINDEX_MUTATION, {"repository": repo_id})
    except (GraphQLError, HTTPRequestError) as exc:
        logger.warning("reindexRepository failed for %s: %s", repo_id, exc)
        return False
    return True


def sanitize_for_filename(text: str) -> str:
    """Replace non-[A-Za-z0-9._-] chars with '_' so the string is filesystem-safe"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def sanitize_endpoint_for_filename(endpoint: str) -> str:
    """Sanitize an endpoint URL for use in filenames, dropping the http(s) scheme"""
    return sanitize_for_filename(re.sub(r"^https?://", "", endpoint))


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")


def _split_name_rev(repo_rev: str) -> tuple[str, str | None]:
    """Split repo[@rev], URL, or scp-style repo text into name and rev"""
    rev: str | None = None
    if _SCHEME_RE.match(repo_rev):
        u = urlsplit(repo_rev)
        # u.hostname is lower-cased and userinfo-stripped
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
            # scp-style 'user@host:path' — drop the 'user@'
            name = after
        else:
            name, rev = before, after
    return name, rev


def parse_repo_rev(repo_rev: str) -> str:
    """Extract the revision from 'repo[$]@rev'. Returns 'HEAD' if no '@rev' is present"""
    _, rev = _split_name_rev(repo_rev)
    return rev if rev is not None else "HEAD"


def parse_repo_name(repo_rev: str) -> str:
    """Extract a canonical Sourcegraph repo name from repo/URL/SSH-ish input"""
    name, _ = _split_name_rev(repo_rev)
    name = name.removeprefix("^").removesuffix("$")
    return name.rstrip("/")


def verify_repo_rev(endpoint: str, token: str, repo_rev: str) -> str:
    """Require repo/rev to resolve to an indexed commit; return output rev name"""
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
    # the actual default branch name so filenames and URLs read naturally
    if rev == "HEAD":
        default_branch: dict[str, Any] = repository.get("defaultBranch") or {}
        return str(default_branch.get("displayName") or "HEAD")
    return rev


def file_url(endpoint: str, repo_name: str, rev: str, file_path: str) -> str:
    """Build a clickable Sourcegraph URL pointing at a specific file at a revision"""
    base = endpoint.rstrip("/")
    rev_segment = f"@{rev}" if rev and rev != "HEAD" else ""
    return f"{base}/{repo_name}{rev_segment}/-/blob/{file_path}"


def fetch_skipped_file_matches(
    endpoint: str,
    token: str,
    name: str,
    rev: str,
) -> list[dict[str, Any]]:
    """Return NOT-INDEXED matches; omit @rev because Zoekt exposes them at HEAD"""
    _ = rev  # verify_repo_rev already checked it maps to an indexed commit
    repo_filter = f"^{re.escape(name)}$"
    search_query = (
        f"r:{repo_filter} type:file index:only "
        f"patternType:regexp count:all ^NOT-INDEXED:"
    )
    data = graphql_request(
        endpoint,
        token,
        SKIPPED_FILES_REASON_QUERY,
        {"query": search_query},
    )
    raw_results: list[dict[str, Any] | None] = (
        data.get("search", {}).get("results", {}).get("results") or []
    )
    # Non-FileMatch results come back as empty objects; drop them
    return [r for r in raw_results if r and r.get("file")]


def write_skipped_files_reason(
    endpoint: str,
    token: str,
    repo_rev: str,
) -> None:
    """Fetch skipped-file matches for repo_rev and write the per-file and stats CSVs"""
    endpoint_sanitized = sanitize_endpoint_for_filename(endpoint)
    # Remove raw-input outputs before validation so failures cannot leave stale CSVs
    input_name_sanitized = sanitize_for_filename(parse_repo_name(repo_rev))
    input_rev_sanitized = sanitize_for_filename(parse_repo_rev(repo_rev))
    input_prefix = f"{endpoint_sanitized}-{input_name_sanitized}-{input_rev_sanitized}"
    Path(f"{input_prefix}-skipped-files.csv").unlink(missing_ok=True)
    Path(f"{input_prefix}-skipped-stats.csv").unlink(missing_ok=True)

    rev = verify_repo_rev(endpoint, token, repo_rev)
    name = parse_repo_name(repo_rev)
    name_sanitized = sanitize_for_filename(name)
    rev_sanitized = sanitize_for_filename(rev)
    prefix = f"{endpoint_sanitized}-{name_sanitized}-{rev_sanitized}"
    files_path = Path(f"{prefix}-skipped-files.csv")
    stats_path = Path(f"{prefix}-skipped-stats.csv")
    # Also remove resolved-rev outputs when they differ from the raw input names
    if prefix != input_prefix:
        files_path.unlink(missing_ok=True)
        stats_path.unlink(missing_ok=True)

    # Keep each local CSV header beside the extractor that writes its value
    def chunk_matches_content(m: dict[str, Any]) -> str:
        chunks: list[dict[str, Any]] = m.get("chunkMatches") or []
        return "\n".join(str(c.get("content") or "") for c in chunks)

    def match_file_byte_size(m: dict[str, Any]) -> int | str:
        file_obj: dict[str, Any] = m.get("file") or {}
        bs = file_obj.get("byteSize")
        return int(bs) if bs is not None else ""

    def match_file_extension(m: dict[str, Any]) -> str:
        file_obj: dict[str, Any] = m.get("file") or {}
        return Path(str(file_obj.get("path") or "")).suffix.lstrip(".")

    def match_file_url(m: dict[str, Any]) -> str:
        repo_obj: dict[str, Any] = m.get("repository") or {}
        file_obj: dict[str, Any] = m.get("file") or {}
        return file_url(
            endpoint,
            str(repo_obj.get("name") or ""),
            rev,
            str(file_obj.get("path") or ""),
        )

    file_columns: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
        ("chunkMatches.content", chunk_matches_content),
        ("file.byteSize", match_file_byte_size),
        ("file.extension", match_file_extension),
        ("file_url", match_file_url),
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
        chunks: list[dict[str, Any]] = match.get("chunkMatches") or []
        for chunk in chunks:
            reason_match = re.search(
                r"NOT-INDEXED:\s*(.+)",
                str(chunk.get("content") or ""),
            )
            if reason_match:
                reason_counts[reason_match.group(1).strip()] += 1

    # Sort by chunkMatches.content so files with the same NOT-INDEXED reason
    # are grouped together; ties broken by byteSize, extension, then file_url
    # Coerce byteSize to int (treating missing values as -1) so an int/str
    # union can't blow up the comparator
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
    """csv.writer wrapper that creates optional CSVs only when needed"""

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
    scope_repo: str | None = None,
    is_site_admin: bool,
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Yield (index, target, repo) tuples for a scoped repo or paged repo list"""
    if scope_repo is not None:
        repo = fetch_single_repo(
            endpoint,
            token,
            scope_repo,
            is_site_admin=is_site_admin,
        )
        logger.info("Scope: single repository %s", scope_repo)
        yield 1, 1, repo
        logger.info("Fetched 1/1 repositories...")
        return
    cursor: str | None = None
    total_fetched = 0
    first_page = True
    while True:
        page_size = PAGE_SIZE
        if max_repos is not None:
            page_size = min(page_size, max_repos - total_fetched)
        data = graphql_request(
            endpoint,
            token,
            GRAPHQL_QUERY,
            {
                "first": page_size,
                "after": cursor,
                "includeExternalServices": is_site_admin,
            },
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

        for repo in connection["nodes"]:
            total_fetched += 1
            yield total_fetched, target, repo

        logger.info("Fetched %d/%d repositories...", total_fetched, target)

        if not connection["pageInfo"]["hasNextPage"]:
            break
        if max_repos is not None and total_fetched >= max_repos:
            break
        cursor = connection["pageInfo"]["endCursor"]


def build_row(repo: dict[str, Any], endpoint: str) -> list[Any]:
    """Build a base CSV row and absolutize the repo URL"""
    base = endpoint.rstrip("/")
    row = [extract(repo) for _, extract, _, _, _ in COLUMNS]
    if row[URL_COLUMN_INDEX]:
        row[URL_COLUMN_INDEX] = base + row[URL_COLUMN_INDEX]
    return row


def append_commit_count(
    row: list[Any],
    commit_count: int | None,
    all_refs_count: int | None,
    elapsed_seconds: float | None,
    optimization_values: list[Any] | None = None,
    *,
    count_commits: bool,
) -> list[Any]:
    """Append optional commit-count fields in COMMIT_COUNT_COLUMNS order"""
    if not count_commits:
        return row
    elapsed_cell: str | None = (
        f"{elapsed_seconds:.3f}" if elapsed_seconds is not None else None
    )
    extras = (
        optimization_values
        if optimization_values is not None
        else [None] * len(COMMIT_COUNT_OPTIMIZATION_COLUMNS)
    )
    return [*row, commit_count, all_refs_count, elapsed_cell, *extras]


def append_run_search(
    row: list[Any],
    match_count: int | None,
    elapsed_seconds: float | None,
    limit_hit: bool,
    alert_title: str | None,
    *,
    run_search: bool,
) -> list[Any]:
    """Append optional run-search fields in RUN_SEARCH_COLUMNS order"""
    if not run_search:
        return row
    elapsed_cell: str | None = (
        f"{elapsed_seconds:.3f}" if elapsed_seconds is not None else None
    )
    return [*row, match_count, elapsed_cell, limit_hit, alert_title]


def csv_columns_for(
    base_columns: list[str],
    *,
    count_commits: bool,
    run_search: bool = False,
) -> list[str]:
    """Return base columns plus enabled optional column blocks"""
    cols = list(base_columns)
    if count_commits:
        cols.extend(name for name, _, _, _ in COMMIT_COUNT_COLUMNS)
    if run_search:
        cols.extend(name for name, _, _, _ in RUN_SEARCH_COLUMNS)
    return cols


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
    scope_repo: str | None = None,
    count_commits_rev: str = "HEAD",
    run_search_pattern: str | None = None,
    stats: StatsCollector | None = None,
    is_site_admin: bool,
) -> tuple[int, int, int]:
    """Stream repos to CSVs and optionally trigger reclone/reindex mutations"""
    run_search_enabled = run_search_pattern is not None
    writer = csv.writer(out)
    writer.writerow(
        csv_columns_for(
            CSV_COLUMNS,
            count_commits=count_commits,
            run_search=run_search_enabled,
        ),
    )

    total = 0
    reclone_total = 0
    reindex_total = 0
    for index, target, repo in fetch_repos(
        endpoint,
        token,
        max_repos,
        scope_repo=scope_repo,
        is_site_admin=is_site_admin,
    ):
        row = build_row(repo, endpoint)
        commit_count: int | None = None
        all_refs_count: int | None = None
        elapsed_seconds: float | None = None
        optimization_values: list[Any] | None = None
        search_match_count: int | None = None
        search_elapsed_seconds: float | None = None
        search_limit_hit = False
        search_alert_title: str | None = None
        position = f"[{index}/{target}]"
        repo_label = repo.get("name") or repo.get("url") or repo.get("id")
        if count_commits:
            repo_name = str(repo.get("name") or "")
            (
                commit_count,
                all_refs_count,
                elapsed_seconds,
                optimization_values,
            ) = fetch_commit_count(endpoint, token, repo_name, count_commits_rev)
            # Keep progress/count logging compact for long-running runs
            default_str = "?" if commit_count is None else f"{commit_count}"
            all_refs_str = "?" if all_refs_count is None else f"{all_refs_count}"
            if commit_count is None:
                # Empty or not-yet-cloned repos commonly have no count
                logger.info(
                    "%s No commit count for %s (default=%s, allRefs=%s) "
                    "[query took %.3fs]",
                    position,
                    repo_label,
                    default_str,
                    all_refs_str,
                    elapsed_seconds,
                )
            else:
                logger.info(
                    "%s Commit count for %s: default=%s, allRefs=%s [query took %.3fs]",
                    position,
                    repo_label,
                    default_str,
                    all_refs_str,
                    elapsed_seconds,
                )
        if run_search_pattern is not None:
            repo_name = str(repo.get("name") or "")
            (
                search_match_count,
                search_elapsed_seconds,
                search_limit_hit,
                search_alert_title,
            ) = fetch_run_search(endpoint, token, repo_name, run_search_pattern)
            count_str = "?" if search_match_count is None else f"{search_match_count}"
            limit_suffix = " (limit hit)" if search_limit_hit else ""
            alert_suffix = (
                f" alert={search_alert_title!r}" if search_alert_title else ""
            )
            logger.info(
                "%s Search %s in %s: matches=%s%s%s [query took %.3fs]",
                position,
                run_search_pattern,
                repo_label,
                count_str,
                limit_suffix,
                alert_suffix,
                search_elapsed_seconds,
            )

        def _augmented(
            base: list[Any],
            *,
            _commit_count: int | None = commit_count,
            _all_refs_count: int | None = all_refs_count,
            _elapsed_seconds: float | None = elapsed_seconds,
            _optimization_values: list[Any] | None = optimization_values,
            _search_match_count: int | None = search_match_count,
            _search_elapsed_seconds: float | None = search_elapsed_seconds,
            _search_limit_hit: bool = search_limit_hit,
            _search_alert_title: str | None = search_alert_title,
        ) -> list[Any]:
            """Apply the optional commit-count and run-search columns in order"""
            with_commit = append_commit_count(
                base,
                _commit_count,
                _all_refs_count,
                _elapsed_seconds,
                _optimization_values,
                count_commits=count_commits,
            )
            return append_run_search(
                with_commit,
                _search_match_count,
                _search_elapsed_seconds,
                _search_limit_hit,
                _search_alert_title,
                run_search=run_search_enabled,
            )

        writer.writerow(_augmented(row))
        total += 1
        if stats is not None:
            stats.add(repo)
        repo_has_cloning_error = has_cloning_error(repo)
        repo_has_indexing_error = has_indexing_error(repo)
        if repo_has_cloning_error:
            cloning_writer.writerow(
                _augmented(
                    row
                    + [
                        extract(repo)
                        for _, extract, _, _, _ in CLONING_ERROR_EXTRA_COLUMNS
                    ],
                ),
            )
        # In single-repo (scope_repo) mode the user explicitly asked for
        # this repo, so trigger the mutation regardless of error state. In
        # full-repo mode keep the existing "only fix repos with errors"
        # guard so a blanket --reclone doesn't reclone the whole instance
        if reclone and (scope_repo is not None or repo_has_cloning_error):
            if trigger_reclone(endpoint, token, repo["id"]):
                reclone_total += 1
        if repo_has_indexing_error:
            indexing_writer.writerow(_augmented(row))
        if reindex and (scope_repo is not None or repo_has_indexing_error):
            if trigger_reindex(endpoint, token, repo["id"]):
                reindex_total += 1
        if skipped_writer is not None and has_skipped_files(repo):
            skipped_writer.writerow(
                _augmented(
                    row
                    + [
                        extract(repo)
                        for _, extract, _, _, _ in SKIPPED_FILES_EXTRA_COLUMNS
                    ],
                ),
            )
    return (total, reclone_total, reindex_total)


def log_http_error(exc: HTTPRequestError) -> None:
    """Log status, headers, body, and traceback of a non-2xx HTTP response"""
    logger.error("HTTP %s %s", exc.status, exc.reason)
    logger.error("URL: %s", exc.url)
    for header, value in exc.headers:
        logger.error("  %s: %s", header, value)
    body = exc.body.decode(errors="replace")
    if body:
        logger.error("Response body:\n%s", body)
    logger.error("HTTP request failed", exc_info=exc)


def load_dotenv() -> None:
    """Load SRC_ENDPOINT and SRC_ACCESS_TOKEN from `.env` if env vars are unset"""
    env_file = Path(".env")
    if not env_file.is_file():
        return
    for lineno, raw in enumerate(
        env_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw.strip()
        # Blank lines and comments are normal `.env` content; skip silently
        if not line or line.startswith("#"):
            continue
        # Log only the line number; malformed lines can contain secrets
        if "=" not in line:
            logger.warning(
                ".env line %d is malformed (missing '='); skipping",
                lineno,
            )
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("SRC_ENDPOINT", "SRC_ACCESS_TOKEN"):
            # setdefault: real env wins over .env
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def die(message: str) -> NoReturn:
    """Log a one-line error and exit with status 1. Never returns"""
    logger.error("Error: %s", message)
    sys.exit(1)


def validate_endpoint(endpoint: str) -> None:
    """Reject obviously-bad SRC_ENDPOINT values with a friendly message"""
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("https", "http"):
        die(
            f"SRC_ENDPOINT must start with https:// or http:// (got {endpoint!r})",
        )
    if not parsed.hostname:
        die(f"SRC_ENDPOINT is missing a hostname (got {endpoint!r})")


def validate_token(token: str) -> None:
    """Reject obviously-bad SRC_ACCESS_TOKEN values with a friendly message"""
    if not token.startswith("sgp_"):
        # Don't log any of the token bytes — even a 5-char prefix can leak
        # info about the source/format. Length alone is enough to confirm
        # something was set without echoing secret material
        die(
            f"SRC_ACCESS_TOKEN must be a Sourcegraph access token starting "
            f"with 'sgp_' (got a {len(token)}-character value)",
        )


def require_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Return (endpoint, token), with CLI args overriding env and `.env`"""
    endpoint = args.src_endpoint or os.environ.get("SRC_ENDPOINT", "")
    token = args.src_access_token or os.environ.get("SRC_ACCESS_TOKEN", "")
    if not endpoint or not token:
        die(
            "set SRC_ENDPOINT and SRC_ACCESS_TOKEN (via --src-endpoint / "
            "--src-access-token, environment variables, or a .env file)",
        )
    validate_endpoint(endpoint)
    validate_token(token)
    return endpoint, token


class BlankLineHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that spaces options and wraps explicit line breaks"""

    def _format_action(self, action: argparse.Action) -> str:
        return super()._format_action(action) + "\n"

    def _split_lines(self, text: str, width: int) -> list[str]:
        lines: list[str] = []
        for raw in text.splitlines():
            if not raw.strip():
                lines.append("")
                continue
            # Preserve any leading whitespace as the wrap indent so spaces
            # used for nesting/example lines aren't collapsed
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
    """argparse type for integers >= 1"""
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
    """Parse command-line arguments into a Namespace"""
    parser = argparse.ArgumentParser(
        description=(
            "List Sourcegraph repositories to CSVs with clone/index metadata\n"
            "\n"
            "Set SRC_ENDPOINT and SRC_ACCESS_TOKEN via env, .env, or args\n"
            "\n"
            f"Output file and column details are in {DEFAULT_CSV_SCHEMA_FILE}"
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
        help="Fetch at most <int> repos (>=1)",
    )
    parser.add_argument(
        "--skipped-files",
        action="store_true",
        help="Write a CSV file for repos where Zoekt skipped files",
    )
    parser.add_argument(
        "--skipped-files-reason",
        metavar="REPO[@REV]",
        default=None,
        help=(
            "Write skipped-file details and reason counts for one repo"
        ),
    )
    parser.add_argument(
        "--reclone",
        nargs="?",
        const=True,
        default=False,
        metavar="REPO",
        help=(
            "With REPO: reclone only that repository\n"
            "Without REPO: reclone repos with cloning errors"
        ),
    )
    parser.add_argument(
        "--reindex",
        nargs="?",
        const=True,
        default=False,
        metavar="REPO",
        help=(
            "With REPO: reindex only that repository\n"
            "Without REPO: reindex all repos with indexing errors"
        ),
    )
    parser.add_argument(
        "--count-commits",
        nargs="?",
        const=True,
        default=False,
        metavar="REPO[@REV]",
        help=(
            "Append per-repo commit counts and cleanup metadata\n"
            "Optional REPO[@REV] scopes to one repo\n"
            "@REV affects only the exact ancestors count"
        ),
    )
    parser.add_argument(
        "--run-search",
        metavar="PATTERN",
        default=None,
        help=(
            "Run PATTERN once per repo and append result columns"
        ),
    )
    parser.add_argument(
        "--statistics",
        action="store_true",
        help="Write statistics CSV files",
    )
    parser.add_argument(
        "--write-csv-schema",
        action="store_true",
        help=f"Regenerate {DEFAULT_CSV_SCHEMA_FILE} and exit; no network required",
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
        help=(
            "Sourcegraph access token (must start with 'sgp_'); prefer the "
            "SRC_ACCESS_TOKEN environment variable"
        ),
    )
    return parser.parse_args(argv)


def collect_scope(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return a shared single-repo scope for scoped flags, or None"""
    scoped: list[tuple[str, str]] = [
        (flag_name, value)
        for flag_name, value in (
            ("--count-commits", args.count_commits),
            ("--reclone", args.reclone),
            ("--reindex", args.reindex),
        )
        if isinstance(value, str)
    ]
    if not scoped:
        return None
    parsed = [
        (flag_name, parse_repo_name(value), parse_repo_rev(value))
        for flag_name, value in scoped
    ]
    repo_names = {name for _, name, _ in parsed}
    if len(repo_names) > 1:
        details = ", ".join(f"{flag}={name}" for flag, name, _ in parsed)
        die(
            "scoped flags reference different repositories ("
            + details
            + "); pass the same REPO[@REV] to each, or run them in separate "
            "invocations",
        )
    repo_name = next(iter(repo_names))
    # Only --count-commits uses rev; reclone/reindex are repo-level mutations
    rev = "HEAD"
    for flag, _, candidate_rev in parsed:
        if flag == "--count-commits":
            rev = candidate_rev
            break
    return repo_name, rev


def run(args: argparse.Namespace, endpoint: str, token: str) -> None:
    """Confirm the connection, then stream every repo to the CSV file"""
    if args.count_commits:
        # Announce the longer per-repo timeout because this mode can be slow
        logger.info(
            "--count-commits enabled: per-repo commit-count query "
            "(timeout=%ds per request)",
            REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT,
        )
    scope = collect_scope(args)
    if scope is not None:
        scope_repo, scope_rev = scope
        logger.info(
            "Scoped run: repository=%s, rev=%s "
            "(reclone=%s, reindex=%s, count-commits=%s)",
            scope_repo,
            scope_rev,
            bool(args.reclone),
            bool(args.reindex),
            bool(args.count_commits),
        )
    else:
        scope_repo = None
        scope_rev = "HEAD"
    username, is_site_admin = fetch_current_user(endpoint, token)
    logger.info(
        "Connected to: %s as: %s (%s)",
        endpoint,
        username,
        "site admin" if is_site_admin else "non-admin",
    )

    # Refuse admin-only mutations before a run starts emitting per-repo warnings
    if not is_site_admin and (args.reclone or args.reindex):
        flags = ", ".join(
            flag
            for flag, set_ in (
                ("--reclone", bool(args.reclone)),
                ("--reindex", bool(args.reindex)),
            )
            if set_
        )
        die(
            f"site-admin token required for: {flags}. "
            f"{username!r} is not a site admin on {endpoint}",
        )

    if not is_site_admin:
        # Some admin-only fields are skipped or returned as null for non-admins
        logger.warning(
            "Non-admin token: skipping Repository.externalServices selection; "
            "mirrorInfo.remoteURL, mirrorInfo.shard, and "
            "mirrorInfo.repositoryStatistics will be empty in the CSV",
        )

    # This targeted report does not need the full repo listing
    if args.skipped_files_reason:
        # Other flags only affect full-listing mode
        ignored = [
            flag
            for flag, set_ in (
                ("--reclone", args.reclone),
                ("--reindex", args.reindex),
                ("--limit", args.limit is not None),
                ("--skipped-files", args.skipped_files),
                ("--count-commits", args.count_commits),
                ("--run-search", args.run_search is not None),
                ("--statistics", args.statistics),
            )
            if set_
        ]
        if ignored:
            logger.warning(
                "Ignoring %s: --skipped-files-reason runs a single targeted "
                "query and does not iterate the repo list",
                ", ".join(ignored),
            )
        write_skipped_files_reason(endpoint, token, args.skipped_files_reason)
        return

    # Prefix outputs with endpoint, plus scoped repo/rev when applicable
    endpoint_sanitized = sanitize_endpoint_for_filename(endpoint)
    if scope_repo is not None:
        scope_suffix = sanitize_for_filename(scope_repo)
        # Only --count-commits uses rev; reclone/reindex filenames stay repo-only
        if args.count_commits and scope_rev != "HEAD":
            scope_suffix = f"{scope_suffix}-{sanitize_for_filename(scope_rev)}"
        prefix = f"{endpoint_sanitized}-{scope_suffix}"
    else:
        prefix = endpoint_sanitized
    output_path = Path(f"{prefix}-{DEFAULT_OUTPUT_FILE}")
    cloning_errors_path = Path(f"{prefix}-{DEFAULT_CLONING_ERRORS_FILE}")
    indexing_errors_path = Path(f"{prefix}-{DEFAULT_INDEXING_ERRORS_FILE}")
    skipped_files_path = (
        Path(f"{prefix}-{DEFAULT_SKIPPED_FILES_FILE}") if args.skipped_files else None
    )
    # Remove stale optional outputs; LazyCSVWriter recreates only non-empty ones
    cloning_errors_path.unlink(missing_ok=True)
    indexing_errors_path.unlink(missing_ok=True)
    if skipped_files_path is not None:
        skipped_files_path.unlink(missing_ok=True)
    # Clear stale stats outputs even when --statistics is not enabled this run
    for suffix, *_ in STATS_FILES:
        Path(f"{prefix}-{DEFAULT_STATS_FILE_PREFIX}-{suffix}.csv").unlink(
            missing_ok=True,
        )

    stats = StatsCollector() if args.statistics else None
    count_commits_enabled = bool(args.count_commits)
    run_search_pattern: str | None = args.run_search
    run_search_enabled = run_search_pattern is not None
    cloning_writer = LazyCSVWriter(
        cloning_errors_path,
        csv_columns_for(
            CLONING_ERROR_CSV_COLUMNS,
            count_commits=count_commits_enabled,
            run_search=run_search_enabled,
        ),
    )
    indexing_writer = LazyCSVWriter(
        indexing_errors_path,
        csv_columns_for(
            CSV_COLUMNS,
            count_commits=count_commits_enabled,
            run_search=run_search_enabled,
        ),
    )
    skipped_writer = (
        LazyCSVWriter(
            skipped_files_path,
            csv_columns_for(
                SKIPPED_FILES_CSV_COLUMNS,
                count_commits=count_commits_enabled,
                run_search=run_search_enabled,
            ),
        )
        if skipped_files_path is not None
        else None
    )
    # Keep the optional skipped writer in the same context-manager block
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
            reclone=bool(args.reclone),
            reindex=bool(args.reindex),
            count_commits=bool(args.count_commits),
            scope_repo=scope_repo,
            count_commits_rev=scope_rev,
            run_search_pattern=run_search_pattern,
            stats=stats,
            is_site_admin=is_site_admin,
        )

    if stats is not None:
        stats_paths = write_stats(prefix, stats)
        for stats_path in stats_paths:
            logger.info("Wrote statistics to %s", stats_path.name)

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


def redact_argv_for_log(argv: list[str]) -> str:
    """Render argv shell-safely, redacting --src-access-token values"""
    redacted: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            redacted.append("REDACTED")
            skip_next = False
            continue
        if arg == "--src-access-token":
            redacted.append(arg)
            skip_next = True
        elif arg.startswith("--src-access-token="):
            redacted.append("--src-access-token=REDACTED")
        else:
            redacted.append(arg)
    return " ".join(shlex.quote(a) for a in redacted)


def configure_logging(log_path: Path) -> None:
    """Send INFO-level logs to both stderr (live feedback) and log_path"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear existing handlers (e.g. on re-entry from tests)
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
    """Route uncaught exceptions through the logger"""
    if issubclass(exc_type, KeyboardInterrupt):
        logger.warning("Interrupted by user (Ctrl-C); exiting")
        return
    logger.error(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def main() -> None:
    """Entry point: configure logging, load env, parse args, run, handle errors"""
    configure_logging(Path(DEFAULT_LOG_FILE))
    # Include pre-run failures in list-repos.log
    sys.excepthook = _log_uncaught_exception

    args = parse_args(sys.argv[1:])
    # Schema generation is offline and credential-free
    if args.write_csv_schema:
        write_csv_schema(Path(DEFAULT_CSV_SCHEMA_FILE))
        return
    load_dotenv()
    endpoint, token = require_credentials(args)
    logger.info(
        "Running: %s (SRC_ENDPOINT=%s)",
        redact_argv_for_log(sys.argv),
        endpoint,
    )

    try:
        run(args, endpoint, token)
    except HTTPRequestError as exc:
        log_http_error(exc)
        sys.exit(1)
    except OSError:
        logger.exception(
            "Could not connect to the server. Check your network and SRC_ENDPOINT",
        )
        sys.exit(1)
    except ValueError as exc:
        die(str(exc))
    except GraphQLError as exc:
        if ":53: no such host" in str(exc):
            logger.error(
                "There's a problem with your Sourcegraph instance "
                "(DNS lookup failure for an internal service). Please try again"
            )
        else:
            logger.exception("GraphQL request failed")
        sys.exit(1)


if __name__ == "__main__":
    main()

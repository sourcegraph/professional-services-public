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
import shlex
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
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 60
# Counting commits server-side can be slow on big monorepos. The COMMIT_COUNT
# call site passes this longer timeout explicitly via graphql_request(timeout=)
# so we don't fail on long-but-still-progressing requests.
REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT = 600
DEFAULT_OUTPUT_FILE = "repos.csv"
DEFAULT_CLONING_ERRORS_FILE = "repos-with-cloning-errors.csv"
DEFAULT_INDEXING_ERRORS_FILE = "repos-with-indexing-errors.csv"
DEFAULT_SKIPPED_FILES_FILE = "repos-with-skipped-files.csv"
DEFAULT_LOG_FILE = "list-repos.log"
DEFAULT_README_FILE = "README.md"

# --- GraphQL queries ----------------------------------------------------------

# Shared field-set used by both the all-repos listing query (GRAPHQL_QUERY)
# and the single-repo lookup query (SINGLE_REPO_QUERY) so a change to the
# CSV row schema can't drift between the two paths. Keeping it as a GraphQL
# fragment in the same document is a standard pattern that the Sourcegraph
# server supports.
REPO_NODE_FRAGMENT = """
fragment RepoNodeFields on Repository {
  id
  name
  url
  createdAt
  isFork
  isArchived
  isPrivate
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
  externalServices(first: 100) @include(if: $includeExternalServices) {
    nodes {
      displayName
    }
  }
}
"""

# `$includeExternalServices` is wired into the GRAPHQL_QUERY / SINGLE_REPO_QUERY
# operations below and threaded through @include on Repository.externalServices
# in REPO_NODE_FRAGMENT. We always send the same query string regardless of
# token type; we just flip the variable to false when the authenticated user
# is not a site admin so the server skips that resolver entirely (it would
# otherwise return "must be site admin" — see admin-permissions.md).
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
# detectors, etc.) can treat the result identically to a listing-page node.
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


# `siteAdmin` lets us decide once, at startup, whether to ask the server for
# admin-only fields (currently just Repository.externalServices via the
# @include directive on REPO_NODE_FRAGMENT) and whether to allow --reclone /
# --reindex (which the server hard-blocks for non-admins regardless).
CURRENT_USER_QUERY = """
query { currentUser { username siteAdmin } }
"""

# Per-repo commit count query. Run once per repo (only when --count-commits is
# set) so we can time each query individually and surface per-repo costs in
# the CSV alongside the count itself. ancestors.totalCount is null when a
# `first:` argument is provided, so we deliberately omit it to ask Sourcegraph
# for the full count of commits reachable from HEAD on the repo's default
# branch.
#
# This query also fetches repo-cleanup ("optimization") metadata so the
# --count-commits CSV exposes when each repo was last cleaned, when its
# next cleanup is scheduled, and the most recent full-repack timestamp.
# repositoryStatistics is admin-only and returns null for non-cloned repos
# or non-admin tokens; the COMMIT_COUNT_OPTIMIZATION_COLUMNS extractors
# tolerate that with empty cells.
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

# Search query template used to count commits across every branch and tag,
# returned alongside the default-branch ancestors count in the same request.
# Sourcegraph's GraphQL has no field for `git rev-list --all --count` and
# the obvious GraphQL alternatives don't work:
#   - commit(rev:"--all") is rejected as an invalid revspec
#   - summing ancestors.totalCount over every gitRefs node massively
#     overcounts shared history (e.g. github.com/curl/curl explodes from
#     ~38k on default to ~4.4M when summed across 231 tags + 19 branches)
# The search-API count is internally consistent across repos and lets users
# spot which repos have meaningful activity beyond their default branch.
# IMPORTANT: this counts commits *as seen by Sourcegraph's commit search*
# and is therefore NOT directly comparable to the
# defaultBranch.target.commit.ancestors.totalCount column above (which is
# an exact git rev-list count from gitserver). The two columns use
# different methodologies — see the --count-commits help text for details.
#
# Only refs/heads/* and refs/tags/* are included; refs/pull/N/head and
# similar internal refs would otherwise inflate the count by tens of
# thousands of GitHub-PR refs. The repo name is regex-anchored and
# escaped so a name with regex specials (a real possibility for code-host
# slugs) can't break the query.
# `timeout:` is a Sourcegraph search-side limiter: if the search engine can't
# complete in that wall-clock budget, the API returns whatever it has so far
# (often with an `alert`) instead of holding the HTTP connection open until
# the gateway or our REQUEST_TIMEOUT_SECONDS_WITH_COMMIT_COUNT fires. Without
# this, repos like github.com/chromium/chromium (1.7M+ commits, thousands of
# refs) would block the run indefinitely and burn all our HTTP retries.
ALL_REFS_COMMIT_SEARCH_TEMPLATE = (
    "r:^{repo}$ rev:*refs/heads/*:*refs/tags/* type:commit count:all timeout:120s"
)


def build_all_refs_search(repo_name: str) -> str:
    """Build the SG search query that counts commits across all branches+tags."""
    return ALL_REFS_COMMIT_SEARCH_TEMPLATE.format(repo=re.escape(repo_name))


# --- Per-repo arbitrary search (--run-search) ---------------------------------
#
# Per-repo execution of a user-supplied search pattern. The pattern is wrapped
# with `r:^{repo}$ ... count:all timeout:120s` so each per-repo query is
# scoped to exactly one repository, returns the full match count rather than
# a paginated subset, and bounds server-side execution time so a pathological
# pattern can't block the run for a whole monorepo.
#
# count:all -> matchCount reflects the true number of matches up to the
#              SG search-engine's internal hard cap (limitHit reports the cap).
# timeout:  -> server-side wall-clock budget; on overrun the API returns
#              partial results plus an `alert`, never an HTTP timeout.
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
    """Build the SG search query for --run-search, scoped to one repo.

    The user's `pattern` is concatenated verbatim — they pass it through
    --run-search as a quoted string and are responsible for any necessary
    Sourcegraph search syntax (patternType:, lang:, file:, etc.). The
    `r:^{repo}$` filter is added by us so the query runs against exactly
    one repository, even if the user's pattern already contains a `r:`
    filter (Sourcegraph treats multiple `r:` filters as additive AND).
    """
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


def fetch_commit_count(
    endpoint: str,
    token: str,
    repo_name: str,
    rev: str = "HEAD",
) -> tuple[int | None, int | None, float, list[Any]]:
    """Run the per-repo COMMIT_COUNT_QUERY and return per-repo commit metrics.

    `rev` defaults to "HEAD" (the repo's default branch) and can be set to
    any revspec (branch name, tag, commit SHA) when --count-commits was
    invoked in scoped mode with REPO@REV. The all-refs search count is not
    affected by `rev` — it always counts across every branch and tag.

    Returns (default_branch_count, all_refs_count, elapsed_seconds,
    optimization_values).

    - default_branch_count: exact git rev-list count of commits reachable from
      `rev` (the default branch HEAD when not overridden).
    - all_refs_count: search-based count of commits reachable across every
      branch and tag (refs/heads/* + refs/tags/*). This is a Sourcegraph
      *search* count, not a git rev-list count, and is therefore NOT
      directly comparable to default_branch_count — see
      ALL_REFS_COMMIT_SEARCH_TEMPLATE for why we cannot get an exact
      rev-list-style count via GraphQL.
    - elapsed_seconds: wall-clock time of the GraphQL request (ALWAYS
      returned, even on failure, so the CSV always has a timing value).
    - optimization_values: aligned with COMMIT_COUNT_OPTIMIZATION_COLUMNS so
      callers can append it directly to a CSV row.

    Returns (None, None, elapsed, [None, ...]) when the GraphQL request
    errors out at the transport level. Either count may be None
    independently of the other (e.g. an empty repo has no HEAD but the
    search may still return 0 across refs, or vice versa).
    """
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
    search_results: dict[str, Any] = (data.get("search") or {}).get("results") or {}
    all_refs_count_raw = search_results.get("matchCount")
    all_refs_count: int | None = (
        all_refs_count_raw if isinstance(all_refs_count_raw, int) else None
    )
    optimization_values = [
        extract(repo) for _, extract in COMMIT_COUNT_OPTIMIZATION_COLUMNS
    ]
    return default_count, all_refs_count, elapsed, optimization_values


def fetch_run_search(
    endpoint: str,
    token: str,
    repo_name: str,
    pattern: str,
) -> tuple[int | None, float, bool, str | None]:
    """Run --run-search's pattern against a single repo and return the result tuple.

    Returns (match_count, elapsed_seconds, limit_hit, alert_title).

    - match_count: number of search matches reported by the SG API. None if
      the API did not return a numeric matchCount (e.g. transport-level
      failure, or the search engine returned only an alert with no results
      block).
    - elapsed_seconds: wall-clock time of the GraphQL request, ALWAYS
      returned (so the CSV always has a timing value, even on failure).
    - limit_hit: True if the SG search engine truncated results before
      reaching the natural end. Surfaced so the user can tell when a
      matchCount is a floor rather than the actual total.
    - alert_title: when the server-side `timeout:` budget is exceeded (or
      the query is otherwise malformed), Sourcegraph returns the partial
      result plus an `alert` describing why. The title is propagated to
      the caller for logging; the row is still written.
    """
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
    results: dict[str, Any] = (data.get("search") or {}).get("results") or {}
    raw_count = results.get("matchCount")
    match_count: int | None = raw_count if isinstance(raw_count, int) else None
    limit_hit = bool(results.get("limitHit"))
    alert: dict[str, Any] = results.get("alert") or {}
    alert_title = alert.get("title") if isinstance(alert, dict) else None
    return match_count, elapsed, limit_hit, alert_title


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

# Repo-cleanup ("optimization") columns piggybacked on the same per-repo
# GraphQL request the commit-count flow already makes. Each entry is
# (csv_column_name, extractor_function); the extractor receives the
# `repository` object returned by COMMIT_COUNT_QUERY (NOT the bulk-listing
# repo node), so it can read the mirrorInfo/repositoryStatistics fields
# requested in that query. Kept separate from COLUMNS so the default CSV
# format is unchanged for users who don't pass --count-commits.
#
# The schema does NOT expose before/after snapshots of an optimization job,
# so we surface only what is queryable: when each repo was last cleaned, the
# next scheduled cleanup time / interval, the repo's current cleanup-queue
# position and whether it is being optimized right now, and (when allowed)
# the most recent full-repack timestamp from packfile stats.
#
# repositoryStatistics is admin-only and returns null for non-cloned repos
# or non-admin tokens — get_path() returns None in that case, which writes
# an empty cell.
COMMIT_COUNT_OPTIMIZATION_COLUMNS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("mirrorInfo.lastCleanedAt", lambda r: get_path(r, "mirrorInfo.lastCleanedAt")),
    (
        "mirrorInfo.cleanupSchedule.due",
        lambda r: get_path(r, "mirrorInfo.cleanupSchedule.due"),
    ),
    (
        "mirrorInfo.cleanupSchedule.intervalSeconds",
        lambda r: get_path(r, "mirrorInfo.cleanupSchedule.intervalSeconds"),
    ),
    (
        "mirrorInfo.cleanupQueue.index",
        lambda r: get_path(r, "mirrorInfo.cleanupQueue.index"),
    ),
    (
        "mirrorInfo.cleanupQueue.optimizing",
        lambda r: get_path(r, "mirrorInfo.cleanupQueue.optimizing"),
    ),
    (
        "mirrorInfo.repositoryStatistics.packfiles.lastFullRepack",
        lambda r: get_path(
            r,
            "mirrorInfo.repositoryStatistics.packfiles.lastFullRepack",
        ),
    ),
]

# Optional columns appended to every CSV (main, cloning-errors, indexing-errors,
# skipped-files) when --count-commits is set. Kept separate from COLUMNS so the
# default CSV format is unchanged for users who don't pass the flag.
#
# The second column (queryTimeSeconds) is the wall-clock time taken by the
# per-repo COMMIT_COUNT_QUERY GraphQL request, so users can spot which repos
# are expensive to count on the Sourcegraph instance. Subsequent columns are
# the optimization metadata fetched in the same request.
COMMIT_COUNT_COLUMNS: list[str] = [
    "defaultBranch.target.commit.ancestors.totalCount",
    # Side-by-side with the default-branch count for easy visual comparison.
    # See ALL_REFS_COMMIT_SEARCH_TEMPLATE for the methodology and caveats.
    "allRefs.search.matchCount",
    "commitCount.queryTimeSeconds",
    *(name for name, _ in COMMIT_COUNT_OPTIMIZATION_COLUMNS),
]

# Optional columns appended to every CSV (main, cloning-errors, indexing-errors,
# skipped-files) when --run-search is set. Placed AFTER COMMIT_COUNT_COLUMNS so
# the existing column ordering for --count-commits is preserved when both flags
# are used together.
#
# `runSearch.matchCount` is the API's matchCount; `runSearch.limitHit` reports
# whether the SG search engine truncated results (so the matchCount is a floor
# rather than a true total); `runSearch.alertTitle` is non-empty when the
# server-side `timeout:` budget was exceeded or the query was malformed.
RUN_SEARCH_COLUMNS: list[str] = [
    "runSearch.matchCount",
    "runSearch.queryTimeSeconds",
    "runSearch.limitHit",
    "runSearch.alertTitle",
]

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


# --- README generation --------------------------------------------------------

# Per-column human-readable descriptions, used by --write-readme to generate
# a README.md alongside the CSV outputs. Wording is derived from the
# Sourcegraph GraphQL schema docstrings (schema.graphql) but expanded to
# clarify units, admin-only fields, and the script's local derivations.
# Keep entries one-per-line and keep prose short; the README is meant to be
# scanned, not read top-to-bottom.
COLUMN_DESCRIPTIONS: dict[str, str] = {
    # --- Main CSV columns -----------------------------------------------------
    "id": (
        "Numeric Sourcegraph database ID for the repository, decoded "
        "locally from the base64 GraphQL global ID. Useful when correlating "
        "with the `repo` table or admin URLs."
    ),
    "url": (
        "Full URL to the repository on this Sourcegraph instance "
        "(`<endpoint>` + `Repository.url`)."
    ),
    "mirrorInfo.remoteURL": (
        "Clone URL of the upstream repository on the code host (may include "
        "embedded credentials). **Site-admin only — empty cell for "
        "non-admin tokens.**"
    ),
    "externalServices": (
        "Semicolon-joined display names of every external service "
        "(code-host connection) that yields this repository. **Site-admin "
        "only — empty cell for non-admin tokens; the script omits the "
        "underlying GraphQL selection in that case.**"
    ),
    "mirrorInfo.status": (
        "Single-word summary of the repo's mirror state, derived locally "
        "from `mirrorInfo`. One of `corrupted`, `errored`, `cloning`, "
        "`cloned`, `not_cloned`, in priority order (so `corrupted` wins "
        "over `errored`, etc.)."
    ),
    "isFork": "Whether this repository is a fork (`True`/`False`).",
    "isArchived": (
        "Whether this repository has been archived on the code host (`True`/`False`)."
    ),
    "isPrivate": "Whether this repository is private (`True`/`False`).",
    "mirrorInfo.byteSize(MB)": (
        "On-disk size of the cloned repository in megabytes "
        "(1 MB = 1024×1024 bytes), converted locally from "
        "`mirrorInfo.byteSize`."
    ),
    "createdAt": ("Timestamp the repo was first added to Sourcegraph (RFC 3339)."),
    "mirrorInfo.lastChanged": (
        "Timestamp of the last time the mirror's content actually changed "
        "(i.e. when commits were last added). May be empty."
    ),
    "mirrorInfo.updatedAt": (
        "Timestamp of the most recent successful sync from the upstream "
        "remote. May be empty."
    ),
    "mirrorInfo.nextSyncAt": (
        "Timestamp the repo is next scheduled to be synced from upstream. May be empty."
    ),
    "mirrorInfo.updateSchedule.intervalSeconds": (
        "Interval, in seconds, between scheduled mirror updates."
    ),
    "mirrorInfo.shard": (
        "Hostname of the gitserver shard that holds this repo's clone. "
        "**Site-admin only — empty cell for non-admin tokens.**"
    ),
    "textSearchIndex.status": (
        "Single-word summary of the search-index state, derived locally: "
        "`indexed` if Zoekt has built an index for this repo, "
        "`not_indexed` otherwise."
    ),
    "textSearchIndex.status.updatedAt": (
        "Timestamp the Zoekt index was last refreshed."
    ),
    "textSearchIndex.status.contentByteSize(MB)": (
        "Size, in megabytes, of the source content that was indexed."
    ),
    "textSearchIndex.status.contentFilesCount": (
        "Number of files included in the index."
    ),
    "textSearchIndex.status.indexByteSize(MB)": (
        "Size, in megabytes, of the on-disk Zoekt index for this repo."
    ),
    "textSearchIndex.status.indexShardsCount": (
        "Number of Zoekt shards that make up this repo's index."
    ),
    "textSearchIndex.status.newLinesCount": (
        "Total number of newlines across every indexed branch (experimental field)."
    ),
    "textSearchIndex.status.defaultBranchNewLinesCount": (
        "Number of newlines indexed on the repo's default branch (experimental field)."
    ),
    "textSearchIndex.status.otherBranchesNewLinesCount": (
        "Number of newlines indexed across non-default branches (experimental field)."
    ),
    "textSearchIndex.host.name": (
        "Hostname of the indexserver responsible for this repo's index."
    ),
    # --- Cloning-error CSV extras --------------------------------------------
    "mirrorInfo.isCorrupted": (
        "Whether Sourcegraph has detected the on-disk clone is corrupted "
        "(`True`/`False`)."
    ),
    "mirrorInfo.lastError": (
        "Last error message returned by gitserver while fetching or "
        "cloning this repo, if any."
    ),
    "mirrorInfo.lastSyncOutput": (
        "Output of the most recent sync attempt. The script truncates to "
        "the first 5 + last 5 lines (with `... [N lines truncated] ...` "
        "between them) when the output is more than 10 lines."
    ),
    "mirrorInfo.corruptionLogs": (
        "Semicolon-joined `timestamp: reason` entries for the most recent "
        "corruption events. The server caps the log at 10 entries, "
        "ordered newest-first."
    ),
    # --- Skipped-files CSV extras --------------------------------------------
    "skippedIndexed.totalCount": (
        "Sum of `skippedIndexed.count` across every indexed ref of this "
        "repo — i.e. how many files Zoekt skipped while indexing."
    ),
    "skippedIndexed.refsWithSkips": (
        "Semicolon-joined `<refName>=<count>` entries for every ref that "
        "has at least one skipped file."
    ),
    "skippedIndexed.headQuery": (
        "Sourcegraph search query that lists every skipped file on HEAD "
        "(or the first ref with skips, when HEAD has none). Paste it into "
        "the search bar to enumerate the skipped files and their "
        "NOT-INDEXED reasons (too-large, binary, too-many-trigrams, "
        "too-small, blob-missing)."
    ),
    # --- --count-commits columns ---------------------------------------------
    "defaultBranch.target.commit.ancestors.totalCount": (
        "Number of commits reachable from HEAD on the default branch — "
        "equivalent to `git rev-list --count HEAD`. Computed by gitserver, "
        "so the value is exact."
    ),
    "allRefs.search.matchCount": (
        "Approximate number of commits across every branch and tag, "
        "computed via Sourcegraph's commit-search API. **Not directly "
        "comparable** to the default-branch count above — see "
        "`--count-commits --help` for the methodology and caveats "
        "(server-side `timeout:` may truncate the result)."
    ),
    "commitCount.queryTimeSeconds": (
        "Wall-clock seconds the per-repo commit-count GraphQL request "
        "took. Useful for spotting which repos are expensive to count."
    ),
    "mirrorInfo.lastCleanedAt": (
        "Timestamp of the last successful gitserver cleanup ('gc') of "
        "this repo. May be empty."
    ),
    "mirrorInfo.cleanupSchedule.due": (
        "Timestamp the repo is next scheduled to be cleaned up by gitserver."
    ),
    "mirrorInfo.cleanupSchedule.intervalSeconds": (
        "Interval, in seconds, between scheduled cleanup runs."
    ),
    "mirrorInfo.cleanupQueue.index": (
        "Position of the repo in the gitserver cleanup queue. "
        "Currently-optimizing repos are pushed to the end of the queue, "
        "so prefer reading this column together with `cleanupQueue."
        "optimizing`."
    ),
    "mirrorInfo.cleanupQueue.optimizing": (
        "Whether gitserver is currently running optimization on this "
        "repo (`True`/`False`)."
    ),
    "mirrorInfo.repositoryStatistics.packfiles.lastFullRepack": (
        "Timestamp of the most recent full repack of this repo's "
        "packfiles. **Site-admin only — empty cell for non-admin tokens, "
        "and also empty when the repo is not yet cloned.**"
    ),
    # --- --run-search columns ------------------------------------------------
    "runSearch.matchCount": (
        "Number of search matches the Sourcegraph search API reported "
        "for the user-supplied `--run-search` pattern, scoped to this "
        "single repo."
    ),
    "runSearch.queryTimeSeconds": (
        "Wall-clock seconds the per-repo `--run-search` GraphQL request took."
    ),
    "runSearch.limitHit": (
        "`True` when the search engine truncated results before reaching "
        "the natural end (so `runSearch.matchCount` is a floor, not the "
        "actual total)."
    ),
    "runSearch.alertTitle": (
        "Title of the search-API alert when the server's `timeout:` "
        "budget was exceeded or the query was malformed; empty cell "
        "otherwise."
    ),
}


def _format_columns_table(columns: list[str]) -> str:
    """Render a column → description Markdown table for `columns`.

    Falls back to a `(no description)` cell when a column is missing
    from COLUMN_DESCRIPTIONS — that should never happen in production,
    but it keeps the README generator honest if a new column is added
    without updating the dict.
    """
    lines = ["| Column | Description |", "|---|---|"]
    for name in columns:
        desc = COLUMN_DESCRIPTIONS.get(name, "(no description)")
        # Escape any pipe characters so they don't break the table.
        safe_desc = desc.replace("|", "\\|")
        lines.append(f"| `{name}` | {safe_desc} |")
    return "\n".join(lines)


def write_readme(path: Path) -> None:
    """Write a README.md describing every CSV file the script can produce.

    Generated from CSV_COLUMNS / CLONING_ERROR_CSV_COLUMNS / etc. and
    COLUMN_DESCRIPTIONS, so this never drifts from the actual CSV layout.
    """
    main_table = _format_columns_table(CSV_COLUMNS)
    cloning_table = _format_columns_table(
        [name for name, _ in CLONING_ERROR_EXTRA_COLUMNS],
    )
    skipped_table = _format_columns_table(
        [name for name, _ in SKIPPED_FILES_EXTRA_COLUMNS],
    )
    commit_count_table = _format_columns_table(COMMIT_COUNT_COLUMNS)
    run_search_table = _format_columns_table(RUN_SEARCH_COLUMNS)

    content = f"""# `list-repos.py` CSV column reference

This file is generated by `python3 list-repos.py --write-readme`. It
documents every column the script can emit across each of its output
CSV files. Column descriptions are derived from the Sourcegraph
GraphQL schema (`schema.graphql`) but rewritten for human readers —
units, admin-only fields, and locally-derived columns are called out
explicitly.

## Output files

The script always writes its outputs prefixed with the sanitized
Sourcegraph endpoint (e.g. `sourcegraph.example.com-repos.csv`):

| File | Written when | Columns |
|---|---|---|
| `<prefix>-{DEFAULT_OUTPUT_FILE}` | Always | Main columns (below) |
| `<prefix>-{DEFAULT_CLONING_ERRORS_FILE}` | At least one repo has a cloning error | Main columns + cloning-error extras |
| `<prefix>-{DEFAULT_INDEXING_ERRORS_FILE}` | At least one repo is cloned but missing a search index | Main columns |
| `<prefix>-{DEFAULT_SKIPPED_FILES_FILE}` | `--skipped-files` is set and at least one repo had Zoekt skip files | Main columns + skipped-files extras |

The optional `--count-commits` and `--run-search` flags append extra
columns to *every* CSV listed above, in this order: main columns →
per-CSV extras → commit-count columns → run-search columns.

## Main columns

These are written to every CSV file.

{main_table}

## Cloning-error extras

Appended only to `<prefix>-{DEFAULT_CLONING_ERRORS_FILE}`.

{cloning_table}

## Skipped-files extras

Appended only to `<prefix>-{DEFAULT_SKIPPED_FILES_FILE}`.

{skipped_table}

## `--count-commits` columns

Appended to every CSV when `--count-commits` is passed.

{commit_count_table}

## `--run-search` columns

Appended to every CSV when `--run-search PATTERN` is passed.

{run_search_table}
"""
    path.write_text(content, encoding="utf-8")
    logger.info("Wrote %s", path)


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


def open_connection(
    parsed: ParseResult,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> http.client.HTTPConnection:
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
    """Send one POST. Returns parsed JSON on 2xx, raises HTTPRequestError on 4xx/5xx."""
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
    """Send once. Re-raises HTTPRequestError; returns OSError instances for retry.

    Pulling the try/except out of the retry loop keeps the loop body simple and
    avoids the per-iteration exception-handler setup cost.
    """
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
    """Execute an HTTP request, retrying transient network errors only.

    HTTPRequestError (4xx/5xx) propagates straight through — the server gave us
    a definitive answer and retrying won't change it. Only socket-level OSError
    cases (DNS failure, connection refused, timeout, etc.) get retried.
    """
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
    """Send a GraphQL query to the Sourcegraph API and return the data block."""
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


def fetch_current_user(endpoint: str, token: str) -> tuple[str, bool]:
    """Return (username, is_site_admin) for the authenticated user.

    All GraphQL queries used by this script require authentication, so an
    invalid/anonymous token is rejected by validate_token (or by the server
    via HTTPRequestError) before this is ever called.

    `is_site_admin` is used to (a) drop the admin-only
    Repository.externalServices selection from the listing/single-repo
    queries via the $includeExternalServices @include directive, and
    (b) refuse --reclone / --reindex up front rather than letting the
    server return a "must be site admin" mutation error mid-run.
    """
    data = graphql_request(endpoint, token, CURRENT_USER_QUERY, {})
    user = data["currentUser"] or {}
    return str(user["username"]), bool(user.get("siteAdmin"))


def fetch_single_repo(
    endpoint: str,
    token: str,
    repo_name: str,
    *,
    is_site_admin: bool,
) -> dict[str, Any]:
    """Return a single repo node in the same shape as the listing query.

    Used by the scoped (`--count-commits REPO[@REV]`, `--reclone REPO[@REV]`,
    `--reindex REPO[@REV]`) modes so the rest of the pipeline (build_row,
    write_csv, has_cloning_error, etc.) can treat the result identically to
    a node from the all-repos listing.

    `is_site_admin` is passed through as the `$includeExternalServices`
    GraphQL variable so non-admin tokens skip the admin-only externalServices
    field instead of triggering a "must be site admin" error.

    Exits via die() when the repository name is unknown to the instance.
    """
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
    scope_repo: str | None = None,
    is_site_admin: bool,
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Yield (index, target, repo) tuples by paginating through the GraphQL API.

    `index` is the 1-based position of `repo` in the run; `target` is the
    total number of repos in scope (min(max_repos, totalCount) when --limit
    is set, else totalCount). Both are surfaced so per-repo log lines (e.g.
    the --count-commits commit-count messages) can show "[N/Total]" without
    the caller having to re-derive the target.

    Logs "Fetching X of Y total repositories..." once, after the first page
    returns its totalCount, so the user sees the target before per-page
    progress lines start. Avoids a separate count-only round-trip.

    When `scope_repo` is set (single-repo mode used by the scoped variants
    of --count-commits / --reclone / --reindex), only that one repository is
    fetched (via fetch_single_repo) and yielded as a one-element iterator.
    `max_repos` is ignored in that case because the result is always one
    repo.
    """
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
    commit_count: int | None,
    all_refs_count: int | None,
    elapsed_seconds: float | None,
    optimization_values: list[Any] | None = None,
    *,
    count_commits: bool,
) -> list[Any]:
    """Append commit-count fields to row when count_commits is True.

    Order matches COMMIT_COUNT_COLUMNS:
      1. commit_count        (defaultBranch.target.commit.ancestors.totalCount)
      2. all_refs_count      (allRefs.search.matchCount)
      3. elapsed_seconds     (commitCount.queryTimeSeconds)
      4. *optimization_values (mirrorInfo.* cleanup/repack metadata)

    elapsed_seconds is rendered to 3 decimal places (millisecond resolution)
    when present so spreadsheets sort it numerically; None becomes an empty
    cell — matching csv.writer's default formatting for None.

    optimization_values must be aligned with COMMIT_COUNT_OPTIMIZATION_COLUMNS.
    Pass None when the per-repo query was skipped (e.g. for a row built
    before fetch_commit_count was attempted) to fill the columns with empty
    cells, keeping every row the same width.
    """
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
    """Append --run-search fields to row when run_search is True.

    Order matches RUN_SEARCH_COLUMNS:
      1. match_count        (runSearch.matchCount)
      2. elapsed_seconds    (runSearch.queryTimeSeconds, 3-decimal seconds)
      3. limit_hit          (runSearch.limitHit, written as the literal True/False)
      4. alert_title        (runSearch.alertTitle, empty cell when no alert)

    Like append_commit_count, this is a no-op when the flag is off so
    every row stays the same width regardless of which optional flags
    were used.
    """
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
    """Return the CSV header list with optional column blocks appended.

    Column block order (when both flags are set, matches the order rows
    are built by append_commit_count() then append_run_search()):
      1. base_columns       (always)
      2. COMMIT_COUNT_COLUMNS  (only when count_commits)
      3. RUN_SEARCH_COLUMNS    (only when run_search)
    """
    cols = list(base_columns)
    if count_commits:
        cols.extend(COMMIT_COUNT_COLUMNS)
    if run_search:
        cols.extend(RUN_SEARCH_COLUMNS)
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
    is_site_admin: bool,
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

    When `scope_repo` is set (single-repo mode triggered by passing a
    REPO[@REV] argument to --count-commits / --reclone / --reindex), only
    that one repository is fetched. Reclone/reindex mutations are then
    applied unconditionally (the user explicitly requested them for that
    repo), bypassing the has_cloning_error / has_indexing_error guard used
    in full-repo iteration mode.

    `count_commits_rev` overrides the rev used for the default-branch
    ancestors count when --count-commits was scoped with REPO@REV; it has
    no effect on the all-refs search count (which always covers every ref).

    Returns (total, reclone_total, reindex_total).
    """
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
            # The "[N/Total]" prefix lets users tail list-repos.log and see how
            # far through the run we are without scrolling back to the
            # per-page "Fetched N/Total" line.
            # Render counts as "?" rather than "None" in the log so the line
            # stays compact when one or both counts are missing.
            default_str = "?" if commit_count is None else f"{commit_count}"
            all_refs_str = "?" if all_refs_count is None else f"{all_refs_count}"
            if commit_count is None:
                # Common for empty / not-yet-cloned repos. Log so users
                # grepping the log can spot which repos returned no count
                # without it being a noisy WARNING.
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
        if run_search_enabled and run_search_pattern is not None:
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
            """Apply the optional commit-count and run-search columns in order."""
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
        repo_has_cloning_error = has_cloning_error(repo)
        repo_has_indexing_error = has_indexing_error(repo)
        if repo_has_cloning_error:
            cloning_writer.writerow(
                _augmented(
                    row + [extract(repo) for _, extract in CLONING_ERROR_EXTRA_COLUMNS],
                ),
            )
        # In single-repo (scope_repo) mode the user explicitly asked for
        # this repo, so trigger the mutation regardless of error state. In
        # full-repo mode keep the existing "only fix repos with errors"
        # guard so a blanket --reclone doesn't reclone the whole instance.
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
                    row + [extract(repo) for _, extract in SKIPPED_FILES_EXTRA_COLUMNS],
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
        nargs="?",
        const=True,
        default=False,
        metavar="REPO[@REV]",
        help=(
            "Without an argument: force reclone every repo with a cloning error\n"
            "(corrupted, errored, or not cloned).\n"
            "\n"
            "With a REPO[@REV] argument (same format as --skipped-files-reason):\n"
            "scope the reclone to that single repository, regardless of whether\n"
            "it is currently in an error state. The @REV portion is accepted\n"
            "for symmetry with --count-commits but is ignored — recloneRepository\n"
            "operates on the whole repository.\n"
            "Example: --reclone github.com/org/repo"
        ),
    )
    parser.add_argument(
        "--reindex",
        nargs="?",
        const=True,
        default=False,
        metavar="REPO[@REV]",
        help=(
            "Without an argument: force reindex every repo with an indexing\n"
            "error.\n"
            "\n"
            "With a REPO[@REV] argument: scope the reindex to that single\n"
            "repository, regardless of whether it is currently in an error\n"
            "state. The @REV portion is accepted but ignored —\n"
            "reindexRepository operates on the whole repository.\n"
            "Example: --reindex github.com/org/repo"
        ),
    )
    parser.add_argument(
        "--count-commits",
        nargs="?",
        const=True,
        default=False,
        metavar="REPO[@REV]",
        help=(
            "With a REPO[@REV] argument (same format as --skipped-files-reason):\n"
            "scope the commit-count GraphQL queries to that single repository.\n"
            "The optional @REV controls which revision the default-branch\n"
            "ancestors count is computed from (defaults to HEAD); the all-refs\n"
            "search count is unaffected by @REV because it always counts across\n"
            "every branch and tag.\n"
            "Example: --count-commits github.com/org/repo@develop\n"
            "\n"
            "Without an argument: append the following columns to all output\n"
            "CSVs (one row per repo in the full listing):\n"
            "  defaultBranch.target.commit.ancestors.totalCount\n"
            "    Exact git rev-list count of commits reachable from HEAD on\n"
            "    each repo's default branch (computed by gitserver).\n"
            "  allRefs.search.matchCount\n"
            "    Sourcegraph search-API count of commits reachable across all\n"
            "    branches and tags (refs/heads/* + refs/tags/*). Useful for\n"
            "    spotting repos whose work happens off the default branch.\n"
            "    Note: NOT directly comparable to the column above —\n"
            "    Sourcegraph has no GraphQL field exposing\n"
            "    `git rev-list --all --count`, and this is the\n"
            "    closest available proxy. It may differ in absolute value\n"
            "    because it counts what Sourcegraph's commit search sees.\n"
            "  commitCount.queryTimeSeconds\n"
            "    The wall-clock time of the per-repo commit-count GraphQL\n"
            "    query (which fetches both counts above plus the\n"
            "    optimization metadata below in a single round-trip).\n"
            "  mirrorInfo.lastCleanedAt\n"
            "    When the repo was last successfully cleaned (optimized).\n"
            "  mirrorInfo.cleanupSchedule.due\n"
            "    When the repo is next due to be enqueued for cleanup.\n"
            "  mirrorInfo.cleanupSchedule.intervalSeconds\n"
            "    Scheduling interval (seconds) used for the next due time.\n"
            "  mirrorInfo.cleanupQueue.index\n"
            "    Position of the repo in the cleanup queue (if queued).\n"
            "  mirrorInfo.cleanupQueue.optimizing\n"
            "    True if the repo is being optimized right now.\n"
            "  mirrorInfo.repositoryStatistics.packfiles.lastFullRepack\n"
            "    Timestamp of the most recent full repack of the repo's\n"
            "    packfiles (admin-only; empty for non-admin tokens or for\n"
            "    repos that are not currently cloned).\n"
            "Each repo gets its own GraphQL request to keep the per-repo timing\n"
            "accurate; this can be slow on big monorepos, so only enable when\n"
            "needed. The per-request HTTP timeout is bumped automatically when\n"
            "this flag is set. The schema does NOT expose before/after stats\n"
            "from a specific optimization run; only the current/last values\n"
            "above are available."
        ),
    )
    parser.add_argument(
        "--run-search",
        metavar="PATTERN",
        default=None,
        help=(
            "Run an arbitrary Sourcegraph search PATTERN once per repository\n"
            "and append per-repo result columns to all output CSVs:\n"
            "  runSearch.matchCount\n"
            "    Number of matches reported by the search API for the\n"
            "    pattern, scoped to that repository (count:all).\n"
            "  runSearch.queryTimeSeconds\n"
            "    Wall-clock time of the per-repo GraphQL search request.\n"
            "  runSearch.limitHit\n"
            "    True when the SG search engine truncated results, meaning\n"
            "    matchCount is a floor rather than the actual total.\n"
            "  runSearch.alertTitle\n"
            "    Non-empty when the server-side timeout: budget was exceeded\n"
            "    or the query was malformed; the row is still written.\n"
            "\n"
            "PATTERN is concatenated verbatim into the search query, so any\n"
            "Sourcegraph search syntax is the user's responsibility (e.g.\n"
            "patternType:regexp, lang:go, file:^src/, etc.). The script\n"
            "wraps it with `r:^REPO$ PATTERN count:all timeout:120s` so it\n"
            "is scoped to one repo, returns the full match count, and is\n"
            "bounded server-side so a pathological pattern can't block the\n"
            "run on a monorepo.\n"
            "Example: --run-search 'TODO patternType:literal'"
        ),
    )
    parser.add_argument(
        "--write-readme",
        action="store_true",
        help=(
            "Write a README.md documenting every CSV column this script can\n"
            "emit (main, cloning-errors, indexing-errors, skipped-files, plus\n"
            "the optional --count-commits / --run-search columns), then exit\n"
            "without contacting the Sourcegraph instance.\n"
            f"Output file: {DEFAULT_README_FILE}"
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


def collect_scope(args: argparse.Namespace) -> tuple[str, str] | None:
    """Determine the single-repo scope from --count-commits / --reclone / --reindex.

    Each of those args is either:
      - False (flag not given)
      - True  (flag given without an argument — full-repo iteration)
      - str   (flag given with REPO[@REV] — scoped to that one repository)

    Returns:
      None when no scoped value is set (full-repo iteration mode).
      (repo_name, rev) tuple otherwise. `rev` defaults to "HEAD" when none of
      the scoped args specified one. The rev only affects --count-commits;
      --reclone and --reindex operate on the whole repo regardless.

    Exits via die() if multiple scoped flags reference different repos —
    we deliberately don't try to run two single-repo operations on
    different repos in one invocation, so the user can re-run if needed.
    """
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
            "invocations.",
        )
    repo_name = next(iter(repo_names))
    # The rev only matters for --count-commits; if it was scoped, take its
    # rev. Otherwise default to "HEAD". For --reclone and --reindex the rev
    # is ignored (the recloneRepository / reindexRepository mutations are
    # repo-level), so we don't bother checking that scoped revs match.
    rev = "HEAD"
    for flag, _, candidate_rev in parsed:
        if flag == "--count-commits":
            rev = candidate_rev
            break
    return repo_name, rev


def run(args: argparse.Namespace, endpoint: str, token: str) -> None:
    """Confirm the connection, then stream every repo to the CSV file."""
    if args.count_commits:
        # The per-repo COMMIT_COUNT_QUERY call site passes its own longer
        # timeout via graphql_request(timeout=); just announce it here so the
        # log makes the slower behaviour visible.
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

    # --reclone / --reindex hit recloneRepository / reindexRepository GraphQL
    # mutations that the server hard-blocks for non-admins (see
    # admin-permissions.md). Refuse up front rather than letting the run get
    # part-way through and then start emitting per-repo "must be site admin"
    # warnings from trigger_reclone / trigger_reindex.
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
            f"{username!r} is not a site admin on {endpoint}.",
        )

    if not is_site_admin:
        # The listing/single-repo queries skip Repository.externalServices via
        # the @include directive, but mirrorInfo.{remoteURL,shard} and
        # mirrorInfo.repositoryStatistics still silently return null for
        # non-admins. Surface that once so users aren't surprised by the
        # blank CSV columns.
        logger.warning(
            "Non-admin token: skipping Repository.externalServices selection; "
            "mirrorInfo.remoteURL, mirrorInfo.shard, and "
            "mirrorInfo.repositoryStatistics will be empty in the CSV.",
        )

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
                ("--run-search", args.run_search is not None),
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
    # outputs from other runs. When scoped to a single repo, also include the
    # sanitized repo name (and rev when --count-commits used REPO@REV) in the
    # prefix so a single-repo run doesn't clobber the full-listing CSVs.
    endpoint_sanitized = sanitize_endpoint_for_filename(endpoint)
    if scope_repo is not None:
        scope_suffix = sanitize_for_filename(scope_repo)
        # Only embed the rev in the filename when --count-commits actually
        # specified one (rev != "HEAD"); --reclone/--reindex ignore rev so
        # adding it would just clutter the filename for those modes.
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
    # Clear any stale outputs from a previous run; LazyCSVWriter will only
    # recreate these files if matching rows are encountered this time.
    cloning_errors_path.unlink(missing_ok=True)
    indexing_errors_path.unlink(missing_ok=True)
    if skipped_files_path is not None:
        skipped_files_path.unlink(missing_ok=True)

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
            reclone=bool(args.reclone),
            reindex=bool(args.reindex),
            count_commits=bool(args.count_commits),
            scope_repo=scope_repo,
            count_commits_rev=scope_rev,
            run_search_pattern=run_search_pattern,
            is_site_admin=is_site_admin,
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


def redact_argv_for_log(argv: list[str]) -> str:
    """Render argv as a shell-safe string with --src-access-token values redacted.

    Handles both `--src-access-token VALUE` and `--src-access-token=VALUE`. Any
    other arguments (including --src-endpoint) are passed through unchanged so
    the log line still records exactly how the script was invoked.
    """
    redacted: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            redacted.append("***REDACTED***")
            skip_next = False
            continue
        if arg == "--src-access-token":
            redacted.append(arg)
            skip_next = True
        elif arg.startswith("--src-access-token="):
            redacted.append("--src-access-token=***REDACTED***")
        else:
            redacted.append(arg)
    return " ".join(shlex.quote(a) for a in redacted)


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
    so list-repos.log would miss it. Logging via logger.error(exc_info=...) sends
    the full traceback to both handlers (stderr + list-repos.log).

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
    # in parse_args / load_dotenv / require_credentials) lands in list-repos.log
    # with a full traceback via this hook.
    sys.excepthook = _log_uncaught_exception

    args = parse_args(sys.argv[1:])
    # --write-readme generates README.md from the in-script column tables;
    # it does NOT need credentials or any network access, so handle it before
    # require_credentials() so users can generate the doc on a fresh checkout.
    if args.write_readme:
        write_readme(Path(DEFAULT_README_FILE))
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
            "Could not connect to the server. Check your network and SRC_ENDPOINT.",
        )
        sys.exit(1)
    except ValueError as exc:
        die(str(exc))
    except GraphQLError as exc:
        if ":53: no such host" in str(exc):
            logger.error(
                "There's a problem with your Sourcegraph instance "
                "(DNS lookup failure for an internal service). Please try again."
            )
        else:
            logger.exception("GraphQL request failed")
        sys.exit(1)


if __name__ == "__main__":
    main()

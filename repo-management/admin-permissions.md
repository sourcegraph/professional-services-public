# Site Admin vs. Authenticated User: GraphQL Permissions for `list-repos.py`

This document records the verified site-admin requirements for each
GraphQL operation and field used by
[list-repos.py](./list-repos.py), based on a resolver-level audit of
`github.com/sourcegraph/sourcegraph` (commit `f3190525`).

Sources of truth:
- [Deep Search thread `0a78e1a1-ee21-438e-9f8d-51afdf6fb54a`](https://sourcegraph.sourcegraph.com/deepsearch/0a78e1a1-ee21-438e-9f8d-51afdf6fb54a) — original resolver-level audit.
- [Deep Search thread `c1e097d7-ef6d-4380-a45b-0ac41124c9fd`](https://sourcegraph.sourcegraph.com/deepsearch/c1e097d7-ef6d-4380-a45b-0ac41124c9fd) — RBAC follow-up: is there a "read-only site admin" role?

## Conclusions

1. **The script's bulk listing path works for any authenticated user.**
   The top-level `repositories`, `repository`, `currentUser`, and
   `search` resolvers have *no* `auth.CheckCurrentUserIsSiteAdmin`
   gate. Results from `repositories` / `repository` / `search` are
   filtered per-repo by the standard authz layer, so a non-admin only
   sees repos they're allowed to read — but the queries themselves
   succeed.

2. **A non-admin token causes "silent" data loss in three specific
   subfields**, all under `Repository.mirrorInfo`:
   - `mirrorInfo.remoteURL`
   - `mirrorInfo.shard`
   - `mirrorInfo.repositoryStatistics` (used by `--count-commits` for
     `lastFullRepack`)

   These resolvers run `CheckCurrentUserIsSiteAdmin` and on failure
   return `nil, nil` instead of `nil, err`. The GraphQL response
   contains `null` for those fields with **no error and no warning**.
   Downstream CSV columns will simply be empty.

3. **A non-admin token causes hard GraphQL errors on three operations**
   the script uses:
   - `Repository.externalServices(first: 100)` — the script's main
     listing query selects this in `REPO_NODE_FRAGMENT`, so the
     **entire `repositories` query fails** for a non-admin token.
   - `recloneRepository(repo: ID!)` — used by `--reclone`.
   - `reindexRepository(repository: ID!)` — used by `--reindex`. The
     resolver enforces admin even though the schema docstring does not.

4. **Practical impact for `list-repos.py`:**
   - Running the script with a non-admin token will fail at the first
     page of `repositories(...)` because of the
     `externalServices(first: 100)` selection inside
     `REPO_NODE_FRAGMENT` ([list-repos.py L135–139](./list-repos.py#L135-L139)).
     To make the bulk listing usable for non-admins, that selection
     would have to be removed (or the field guarded behind an `@include`
     directive driven by an admin check).
   - Even after dropping `externalServices`, three CSV columns derived
     from `mirrorInfo.remoteURL` / `mirrorInfo.shard` / `repositoryStatistics`
     would be silently blank for non-admin tokens.
   - `--reclone` and `--reindex` cannot work without site admin — there
     is no non-admin equivalent.

5. **`textSearchIndex` and the rest of `mirrorInfo` are open** to any
   authenticated user with repo access, so all of `cloned`,
   `cloneInProgress`, `isCorrupted`, `lastError`, `lastSyncOutput`,
   `corruptionLogs`, `byteSize`, `lastChanged`, `updatedAt`,
   `nextSyncAt`, `updateSchedule.intervalSeconds`, and the entire
   `textSearchIndex` subtree continue to work.

6. **Sourcegraph's RBAC does not provide a "read-only site admin" role
   that would unblock this script.** A non-admin token — even with
   every available RBAC permission granted — still hits the same hard
   `CheckCurrentUserIsSiteAdmin` gates documented above. See the
   [RBAC](#rbac-no-read-only-site-admin-role) section below for
   specifics.

## RBAC: no "read-only site admin" role

Sourcegraph's RBAC ships with exactly four built-in system roles
(seeded in
[`internal/tenant/reconciler/frontend_data.sql`](https://sourcegraph.com/github.com/sourcegraph/sourcegraph/-/blob/internal/tenant/reconciler/frontend_data.sql)):

- `USER`
- `SITE_ADMINISTRATOR`
- `WORKSPACE_ADMINISTRATOR`
- `SERVICE_ACCOUNT`

There is no `READ_ONLY_SITE_ADMIN` (or equivalent) role, and the
permission schema in
[`internal/rbac/schema.yaml`](https://sourcegraph.com/github.com/sourcegraph/sourcegraph/-/blob/internal/rbac/schema.yaml)
does not define a permission that collectively grants read access to
the admin-only fields this script depends on. Custom roles can be
created with namespaces such as `REPO_MANAGEMENT#READ`,
`INTEGRATION_MANAGEMENT#READ`, or `ADVANCED_CONFIG#READ`, but they
are inspected by *specific* resolvers only.

Of the seven admin-gated elements this script touches, RBAC
permissions are honored on **zero** of them — every one is enforced
via a direct `auth.CheckCurrentUserIsSiteAdmin` call with no RBAC
fallback:

| Element used by `list-repos.py` | Honors RBAC permission? |
|---|---|
| `Repository.externalServices(first:)` (per-repo, in `REPO_NODE_FRAGMENT`) | No — hard site-admin check ([`repository_external.go` L54-60](https://sourcegraph.com/github.com/sourcegraph/sourcegraph/-/blob/cmd/frontend/graphqlbackend/repository_external.go?L54-60)) |
| `mirrorInfo.remoteURL` | No |
| `mirrorInfo.shard` | No |
| `mirrorInfo.repositoryStatistics` | No |
| `recloneRepository` mutation | No |
| `reindexRepository` mutation | No |
| Listing all *private* repos via `repositories(first, after)` | No (requires admin or unrestricted authz, not an RBAC permission) |

For reference, the *top-level* `externalServices` connection
([`external_services.go` L350-358](https://sourcegraph.com/github.com/sourcegraph/sourcegraph/-/blob/cmd/frontend/graphqlbackend/external_services.go?L350-358))
and `repositoryStats`
([`repository_stats.go` L126-137](https://sourcegraph.com/github.com/sourcegraph/sourcegraph/-/blob/cmd/frontend/graphqlbackend/repository_stats.go?L126-137))
*do* accept `REPO_MANAGEMENT#READ` — but `list-repos.py` does not
use either of those queries; it uses the per-repo
`Repository.externalServices` field, which does not.

**Bottom line:** there is no RBAC-granted user that can run
`list-repos.py` as-is. The script still requires a site-admin token.
A custom role with `REPO_MANAGEMENT#READ` would only help if the
script were changed to (a) drop the per-repo `externalServices`
selection, (b) accept silent `null`s on the three admin-gated
`mirrorInfo` subfields, and (c) skip `--reclone` / `--reindex`.

## Permission Table

Resolver paths are under `cmd/frontend/graphqlbackend/` in
`github.com/sourcegraph/sourcegraph` unless otherwise noted.

| # | Operation / Field | Used by `list-repos.py` for | Admin Required? | Failure Mode for Non-Admin | Resolver |
|---|---|---|---|---|---|
| 1 | `repositories(first, after)` | Bulk repo listing (`GRAPHQL_QUERY`) | No (top-level) | — (succeeds) | `repositories.go` L93–123 |
| 2 | `repository(name:)` | `SINGLE_REPO_QUERY`, `COMMIT_COUNT_QUERY` | No | — (returns `null` if user can't see repo) | `graphqlbackend.go` L831–864 |
| 3 | `currentUser` | Auth/connectivity check (`CURRENT_USER_QUERY`) | No | — (returns `null` if unauthenticated) | `graphqlbackend.go` L1036–1038 |
| 4 | `search(query, version)` | All-refs commit count, `--run-search` | No | — (results authz-filtered) | `search.go` L39–41 |
| 5 | `recloneRepository(repo:)` | `--reclone` | **Yes** | **Hard GraphQL error** | `graphqlbackend.go` L870–897 |
| 6 | `reindexRepository(repository:)` | `--reindex` | **Yes** | **Hard GraphQL error** | `repository_reindex.go` L14–20 |
| 7a | `Repository.mirrorInfo` (object) | All mirror fields | No | — | `repository_mirror.go` L23–25 |
| 7b | `mirrorInfo.remoteURL` | CSV column | **Yes** | **Silent `null`** (no error) | `repository_mirror.go` L116–121 |
| 7c | `mirrorInfo.shard` | CSV column | **Yes** | **Silent `null`** (no error) | `repository_mirror.go` L304–310 |
| 7d | `mirrorInfo.repositoryStatistics` | `--count-commits` (`lastFullRepack`) | **Yes** | **Silent `null`** (no error) | `git_repository_statistics.go` L23–27 |
| 7e | `mirrorInfo.lastError` | Cloning-error CSV | No | — | `repository_mirror.go` L198–205 |
| 7f | `mirrorInfo.lastSyncOutput` | Cloning-error CSV | No | — | `repository_mirror.go` L207–217 |
| 7g | `mirrorInfo.corruptionLogs` | CSV column | No | — | `repository_mirror.go` L269–281 |
| 7h | `mirrorInfo.lastCleanedAt` | `--count-commits` | No | — | `repository_mirror.go` L99–110 |
| 7i | `mirrorInfo.cleanupSchedule` | `--count-commits` | No | — | `repository_mirror.go` L61–74 |
| 7j | `mirrorInfo.cleanupQueue` | `--count-commits` | No | — | `repository_mirror.go` L76–85 |
| 7k | `mirrorInfo.byteSize` | CSV column | No | — | `repository_mirror.go` L295–302 |
| 7l | `mirrorInfo.cloned` | CSV column | No | — | `repository_mirror.go` L162–169 |
| 7m | `mirrorInfo.cloneInProgress` | CSV column | No | — | `repository_mirror.go` L171–178 |
| 7n | `mirrorInfo.isCorrupted` | CSV column | No | — | `repository_mirror.go` L257–267 |
| 7o | `mirrorInfo.updatedAt` | CSV column | No | — | `repository_mirror.go` L219–230 |
| 7p | `mirrorInfo.lastChanged` | CSV column | No | — | `repository_mirror.go` L232–243 |
| 7q | `mirrorInfo.nextSyncAt` | CSV column | No | — | `repository_mirror.go` L245–255 |
| 7r | `mirrorInfo.updateSchedule.intervalSeconds` | CSV column | No | — | `repository_mirror.go` L323–331 |
| 8 | `Repository.externalServices(first:)` | Selected in `REPO_NODE_FRAGMENT` (every list/single query) | **Yes** | **Hard GraphQL error** — breaks the whole listing query | `repository_external.go` L54–60 |
| 9 | `Repository.textSearchIndex` (+ `status.*`, `host.name`, `refs.*`, `skippedIndexed.*`) | Indexing-error / skipped-files CSV | No | — | `repository_text_search_index.go` L20–270 |

## Queries that fail without site admin

These are the operations from `list-repos.py` that **cannot succeed**
for a non-site-admin token (i.e. produce a GraphQL error rather than
returning data):

| Script feature | Failing GraphQL element | Why |
|---|---|---|
| Default run (any invocation that fetches repos) | `Repository.externalServices(first: 100)` inside `REPO_NODE_FRAGMENT` | Resolver returns an error to non-admins because external services contain credentials. Because this is selected by both `GRAPHQL_QUERY` and `SINGLE_REPO_QUERY`, **every listing or single-repo fetch errors out**. |
| `--reclone` (full instance or `REPO[@REV]`) | `recloneRepository(repo: ID!)` mutation | Resolver explicitly enforces site admin. |
| `--reindex` (full instance or `REPO[@REV]`) | `reindexRepository(repository: ID!)` mutation | Resolver explicitly enforces site admin (despite schema docstring not mentioning it). |

## Queries that "work" but return less data without site admin

These succeed without an error but silently return `null` for non-admin
callers; their corresponding CSV columns will be empty:

| CSV-affecting field | Fallback for non-admins |
|---|---|
| `mirrorInfo.remoteURL` | `null` |
| `mirrorInfo.shard` | `null` |
| `mirrorInfo.repositoryStatistics.packfiles.lastFullRepack` (only with `--count-commits`) | `null` |

# Entitlement sync

Reconcile Sourcegraph `DEEP_SEARCH` entitlement grants to match `user-entitlements.csv`.

The CSV is the source of truth: the tool makes the instance match it. Each `email` value must be a verified Sourcegraph user email address. The tool resolves users through `POST /api/users.v1.Service/GetUser` with `name: "users/<email>"`, extracts the numeric user ID from the returned `users/<id>` resource name, and uses that ID in entitlement mutations. Each `entitlement` value is matched to an existing entitlement `name`. Grants that are no longer in the CSV are **removed**, so users are granted, moved, or revoked through the GraphQL mutations:

```graphql
createEntitlementGrants(entitlementID: ID!, userIDs: [ID!]!)
deleteEntitlementGrants(entitlementID: ID!, userIDs: [ID!]!)
```

## How reconciliation works

A user may hold at most **one** `DEEP_SEARCH` entitlement. On each run the tool compares the desired state (the CSV) to the current grants on the instance and, for all non-default `DEEP_SEARCH` entitlements, computes the changes needed to converge:

- **Grant** – a CSV user who does not yet hold the entitlement the CSV assigns them.
- **Move** – a CSV user who holds a different entitlement than the CSV assigns them. Because only one grant is allowed per type, the old grant is revoked and the new one created.
- **Revoke** – a user who currently holds any non-default entitlement but is no longer in the CSV. Their grant is removed and they fall back to the default entitlement.
- **No-op** – a CSV user who already holds exactly the entitlement the CSV assigns them.

All non-default entitlements are reconciled, even if the CSV no longer mentions a specific entitlement. Runs are idempotent: applying the same CSV twice makes no changes the second time.

Current entitlement state is read from each non-default entitlement's grant list. The users API is used only for user identity lookup, because `GetUser` does not include entitlement grants.

### The default entitlement is never modified

Every instance has one default `DEEP_SEARCH` entitlement that automatically applies to all users without an explicit grant. Its grant list therefore contains *every* user, so it cannot be reconciled from a CSV. The tool never lists, grants, or revokes the default entitlement. Any CSV row that assigns a user to the default entitlement is reported, but it still means the user should have no explicit non-default grant.

## Worked example

Given the sample `user-entitlements.csv` (assume `Tier 1` is the default entitlement, and `Tier 2` / `Tier 3` are not):

```csv
email,entitlement
loadtest001@example.com,Tier 3
loadtest002@example.com,Tier 3
loadtest003@example.com,Tier 3
loadtest004@example.com,Tier 2
loadtest005@example.com,Tier 2
loadtest006@example.com,Tier 2
```

The desired end state is:

| Entitlement (`entitlement`) | Users |
| --------------------------- | ----- |
| `Tier 3`                    | `loadtest001`, `loadtest002`, `loadtest003` |
| `Tier 2`                    | `loadtest004`, `loadtest005`, `loadtest006` |

### First run – starting from a clean instance

If none of these users have an explicit grant yet, all six are granted.

Dry-run (default) reports the plan without changing anything:

```
$ uv run entitlement-sync
Would grant 6 and revoke 0 entitlement grant(s).
  + grant  loadtest001 -> Tier 3
  + grant  loadtest002 -> Tier 3
  + grant  loadtest003 -> Tier 3
  + grant  loadtest004 -> Tier 2
  + grant  loadtest005 -> Tier 2
  + grant  loadtest006 -> Tier 2
```

Apply performs the grants:

```
$ uv run entitlement-sync --apply
Granted 6 and revoked 0 entitlement grant(s).
```

### Second run – moving and revoking

Now edit the CSV to move `loadtest003` from `Tier 3` to `Tier 2` and drop `loadtest006` entirely:

```csv
email,entitlement
loadtest001@example.com,Tier 3
loadtest002@example.com,Tier 3
loadtest003@example.com,Tier 2
loadtest004@example.com,Tier 2
loadtest005@example.com,Tier 2
```

Dry-run shows exactly the changes needed to converge:

```
$ uv run entitlement-sync
Would grant 1 and revoke 2 entitlement grant(s).
  + grant  loadtest003 -> Tier 2
  - revoke loadtest003 from Tier 3
  - revoke loadtest006 from Tier 2
```

Apply revokes first (so the move is not skipped), then creates:

```
$ uv run entitlement-sync --apply
Granted 1 and revoked 2 entitlement grant(s).
```

Afterwards `loadtest003` holds `Tier 2`, `Tier 3` holds only `loadtest001` and `loadtest002`, and `loadtest006` has no explicit grant (it falls back to the default `Tier 1`). Running again is a no-op:

```
$ uv run entitlement-sync
Would grant 0 and revoke 0 entitlement grant(s).
```

### Skipped and unresolved rows

Rows are only acted on when both the user and a non-default entitlement resolve. The command exits with status `1` when any rows are skipped, while still applying every resolved change:

```
$ uv run entitlement-sync
Default entitlement(s) cannot be assigned per user and were skipped: Tier 1
Missing entitlement(s): No Such Tier
Missing user(s): nosuchuser@example.com
Would grant 3 and revoke 0 entitlement grant(s).
  ...
```

When applying, users returned as `skippedUsers` by `createEntitlementGrants` (because they already hold an entitlement of this type) are subtracted from the granted count and listed under `Skipped existing grants:`.

## Usage

The CSV must have these columns:

```csv
email,entitlement
loadtest001@example.com,Tier 2
```

Dry run, using the default `user-entitlements.csv`:

```sh
SRC_ENDPOINT=https://sourcegraph.example.com \
SRC_ACCESS_TOKEN=sgp_... \
uv run entitlement-sync
```

Apply grants:

```sh
SRC_ENDPOINT=https://sourcegraph.example.com \
SRC_ACCESS_TOKEN=sgp_... \
uv run entitlement-sync --apply
```

Use a different CSV:

```sh
uv run entitlement-sync path/to/user-entitlements.csv --sourcegraph-url https://sourcegraph.example.com --token sgp_... --apply
```


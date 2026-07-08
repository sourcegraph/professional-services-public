# entitlement-sync — agent notes

Python CLI that reconciles Sourcegraph `DEEP_SEARCH` entitlement grants to match `user-entitlements.csv`. The CSV is `email,entitlement`; users are resolved by verified email address through the users API. Read `README.md` for the full reconciliation semantics (grant/move/revoke/no-op, default-entitlement handling). This file is only for the non-obvious dev workflow.

## Layout
- Single package under `src/entitlement_sync/` (uv src layout). Effectively all logic lives in `src/entitlement_sync/main.py`; there are no submodules.
- Tests in `tests/` use stdlib `unittest` (not pytest), mostly against a `Mock(spec=SourcegraphClient)`.
- Runtime is intentionally dependency-light: only `truststore` (for system trust store TLS) plus stdlib `urllib`. Do not add HTTP libraries like `requests`/`httpx` without reason.

## Commands (run from this directory)
- Run tests: `uv run python -m unittest discover tests`
- Run the CLI in dev: use `envchain` with the `LOCAL` namespace to inject `SRC_ENDPOINT` / `SRC_ACCESS_TOKEN` instead of exporting secrets:
  - Dry-run (default): `envchain LOCAL uv run entitlement-sync`
  - Apply: `envchain LOCAL uv run entitlement-sync --apply`
  - `envchain LOCAL` prefixes every dev invocation of `uv run` so the token never lands in shell history or env files.
- The tool defaults to dry-run; `--apply` is required to mutate the instance. Credentials come from `SRC_ENDPOINT`/`SRC_ACCESS_TOKEN` env vars or `--sourcegraph-url`/`--token` flags.

## Gotchas
- `.python-version` pins `3.14` but `pyproject.toml` requires `>=3.12`; uv resolves against the pinned interpreter.
- No linter/formatter is configured despite `.ruff_cache` being gitignored — do not assume `ruff` runs in CI.
- The CLI exits `1` when any CSV rows are skipped (missing user/entitlement, or a default entitlement) while still applying resolved changes. Non-zero exit does not mean nothing happened.
- CSV users are looked up with `POST /api/users.v1.Service/GetUser` using `name: "users/<email>"`; the numeric ID is extracted from the returned `users/<id>` resource name for entitlement mutations.
- `GetUser` does not return entitlement grant state. Current explicit grants are derived from each non-default entitlement's GraphQL `userGrants` list, using `databaseID` so IDs match the users API numeric IDs.
- The default `DEEP_SEARCH` entitlement is never listed or revoked. Preserve this invariant — it is covered by `test_sync_never_lists_or_revokes_default_entitlement`.
- On apply, revokes run before grants so a user moving between entitlements isn't blocked by the one-grant-per-type rule.
- For local testing with the checked-in CSV, make sure each test user has a verified `<username>@example.com` email. Use `envchain LOCAL src users get -username=<username>` to inspect, and GraphQL mutations `addUserEmail` plus `setUserEmailVerified` via `envchain LOCAL src api` to add missing addresses.

## Repo classification
This is a public PS repo — all content must meet the PUBLIC data classification. Never commit real endpoints, tokens, or customer data (including in `user-entitlements.csv` examples).

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import truststore


ENTITLEMENTS_QUERY = """
query Entitlements($first: Int!) {
  entitlements(type: DEEP_SEARCH, first: $first) {
    nodes {
      id
      name
      isDefault
    }
  }
}
"""

ENTITLEMENT_GRANTS_QUERY = """
query EntitlementGrants($entitlementID: ID!, $first: Int!, $after: String) {
  node(id: $entitlementID) {
    ... on Entitlement {
      userGrants(first: $first, after: $after) {
        nodes {
          databaseID
          username
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

CREATE_ENTITLEMENT_GRANTS_MUTATION = """
mutation CreateEntitlementGrants($entitlementID: ID!, $userIDs: [ID!]!) {
  createEntitlementGrants(entitlementID: $entitlementID, userIDs: $userIDs) {
    skippedUsers {
      username
    }
  }
}
"""

DELETE_ENTITLEMENT_GRANTS_MUTATION = """
mutation DeleteEntitlementGrants($entitlementID: ID!, $userIDs: [ID!]!) {
  deleteEntitlementGrants(entitlementID: $entitlementID, userIDs: $userIDs) {
    alwaysNil
  }
}
"""


@dataclass(frozen=True)
class Entitlement:
    """A DEEP_SEARCH entitlement on the instance."""

    id: str
    name: str
    # Default entitlements apply to every user without an explicit grant, so their
    # membership cannot be reconciled from a CSV and they are never revoked.
    is_default: bool = False


@dataclass(frozen=True)
class UserInfo:
    """A Sourcegraph user resolved from the users.v1 API."""

    id: str
    username: str


@dataclass(frozen=True)
class GrantChange:
    """A single (entitlement, user) pairing to add or revoke, keyed by name for display."""

    entitlement: str
    username: str


@dataclass(frozen=True)
class SyncResult:
    # Pairings present in the CSV that are not yet granted on the instance.
    to_grant: list[GrantChange] = field(default_factory=list)
    # Pairings granted on the instance for a CSV-listed entitlement, but no longer
    # in the CSV (or the user moved to a different entitlement).
    to_revoke: list[GrantChange] = field(default_factory=list)
    granted_count: int = 0
    revoked_count: int = 0
    skipped_users: list[str] = field(default_factory=list)
    missing_entitlements: set[str] = field(default_factory=set)
    missing_users: set[str] = field(default_factory=set)
    # CSV entitlements that name a default entitlement and were therefore skipped.
    default_entitlements: set[str] = field(default_factory=set)

    @property
    def planned_grant_count(self) -> int:
        return len(self.to_grant)

    @property
    def planned_revoke_count(self) -> int:
        return len(self.to_revoke)


def user_id_from_resource_name(name: str) -> str:
    prefix = "users/"
    if not name.startswith(prefix) or len(name) == len(prefix):
        raise RuntimeError(f"User API returned invalid user name: {name!r}")
    return name[len(prefix) :]


class SourcegraphAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"API request failed with HTTP {status_code}: {detail}")


class SourcegraphClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = urllib.request.Request(
            f"{self.url}/.api/graphql",
            data=body,
            headers={
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, context=self.ssl_context) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"GraphQL request failed with HTTP {error.code}: {detail}") from error

        if payload.get("errors"):
            raise RuntimeError(f"GraphQL request failed: {payload['errors']}")
        return payload["data"]

    def api_post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(data).encode()
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            headers={
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, context=self.ssl_context) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise SourcegraphAPIError(error.code, detail) from error

    def entitlements(self) -> dict[str, Entitlement]:
        data = self.graphql(ENTITLEMENTS_QUERY, {"first": 100})
        return {
            node["name"]: Entitlement(
                id=node["id"], name=node["name"], is_default=node["isDefault"]
            )
            for node in data["entitlements"]["nodes"]
        }

    def users(self, user_identifiers: set[str]) -> dict[str, UserInfo]:
        users: dict[str, UserInfo] = {}
        for user_identifier in sorted(user_identifiers):
            try:
                user = self.api_post(
                    "/api/users.v1.Service/GetUser",
                    {"name": f"users/{user_identifier}"},
                )
            except SourcegraphAPIError as error:
                if error.status_code == 404:
                    continue
                raise

            user_id = user_id_from_resource_name(user["name"])
            resolved_username = user.get("username") or user_identifier
            if not resolved_username:
                continue
            users[user_identifier] = UserInfo(id=user_id, username=resolved_username)
        return users

    def entitlement_grants(self, entitlement_id: str) -> dict[str, str]:
        """Return the users currently granted an entitlement, keyed by username -> user ID."""
        grants: dict[str, str] = {}
        after: str | None = None
        while True:
            data = self.graphql(
                ENTITLEMENT_GRANTS_QUERY,
                {"entitlementID": entitlement_id, "first": 100, "after": after},
            )
            node = data["node"]
            if node is None:
                break
            connection = node["userGrants"]
            for user in connection["nodes"]:
                grants[user["username"]] = str(user["databaseID"])
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]
        return grants

    def create_entitlement_grants(self, entitlement_id: str, user_ids: list[str]) -> list[str]:
        data = self.graphql(
            CREATE_ENTITLEMENT_GRANTS_MUTATION,
            {"entitlementID": entitlement_id, "userIDs": user_ids},
        )
        return [user["username"] for user in data["createEntitlementGrants"]["skippedUsers"]]

    def delete_entitlement_grants(self, entitlement_id: str, user_ids: list[str]) -> None:
        self.graphql(
            DELETE_ENTITLEMENT_GRANTS_MUTATION,
            {"entitlementID": entitlement_id, "userIDs": user_ids},
        )


def read_user_entitlements(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        missing_columns = {"email", "entitlement"} - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing_columns))}")

        rows: list[tuple[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            email = (row["email"] or "").strip()
            entitlement_name = (row["entitlement"] or "").strip()
            if not email or not entitlement_name:
                raise ValueError(f"CSV row {line_number} must include email and entitlement")
            rows.append((email, entitlement_name))
        return rows


@dataclass(frozen=True)
class ResolvedDesire:
    # user ID -> desired entitlement ID, or None when the CSV explicitly assigns
    # the user to the default entitlement (meaning no explicit grant).
    desired: dict[str, str | None] = field(default_factory=dict)
    missing_entitlements: set[str] = field(default_factory=set)
    missing_users: set[str] = field(default_factory=set)
    # CSV entitlements that name a default entitlement, which cannot be assigned per-user.
    default_entitlements: set[str] = field(default_factory=set)


def resolve_desired_grants(
    rows: list[tuple[str, str]],
    entitlements: dict[str, Entitlement],
    users: dict[str, UserInfo],
) -> ResolvedDesire:
    """Resolve the CSV into a desired mapping of user ID -> entitlement ID.

    Rows are skipped (and reported) when the entitlement does not match an existing
    entitlement or when the email address does not match an existing user. When the
    entitlement names the default entitlement, the user is resolved to no explicit
    desired grant: this revokes any current non-default grant while avoiding any
    per-user default grant operation. If an email appears more than once, the
    last usable row wins (a user can hold only one DEEP_SEARCH entitlement).
    """
    result = ResolvedDesire()

    for user_identifier, entitlement_name in rows:
        entitlement = entitlements.get(entitlement_name)
        user = users.get(user_identifier)
        if entitlement is None:
            result.missing_entitlements.add(entitlement_name)
        elif entitlement.is_default:
            result.default_entitlements.add(entitlement_name)
        if user is None:
            result.missing_users.add(user_identifier)
        if entitlement is not None and user is not None:
            result.desired[user.id] = None if entitlement.is_default else entitlement.id

    return result


@dataclass(frozen=True)
class ReconcilePlan:
    """The concrete grant/revoke operations needed to converge to the CSV.

    All non-default entitlements are reconciled; the default entitlement is never
    listed or modified.
    """

    # entitlement ID -> user IDs to grant
    grants: dict[str, list[str]] = field(default_factory=dict)
    # entitlement ID -> user IDs to revoke
    revocations: dict[str, list[str]] = field(default_factory=dict)
    # Human-readable pairings, for dry-run reporting.
    grant_changes: list[GrantChange] = field(default_factory=list)
    revoke_changes: list[GrantChange] = field(default_factory=list)


def plan_reconciliation(
    client: SourcegraphClient,
    desired_by_user: dict[str, str | None],
    entitlements: dict[str, Entitlement],
    users: dict[str, UserInfo],
) -> ReconcilePlan:
    """Diff the desired CSV state against the current grants on the instance.

    Two sources of change are considered across all non-default entitlements:

    1. Each CSV user whose current explicit DEEP_SEARCH grant differs from the
       entitlement the CSV assigns them. Because a user may hold only one
       entitlement per type, a non-default change means revoking the old grant
       (if any) and creating the new one; a default assignment only revokes the
       old explicit grant.
    2. Users currently granted any non-default entitlement who are absent from
       the CSV entirely. Their grant is revoked (falling back to the default) so
       the instance matches the CSV.

    The default entitlement is never listed or revoked: its grant list contains
    every user without an explicit grant, so it cannot be reconciled from a CSV.
    """
    entitlement_by_id = {ent.id: ent for ent in entitlements.values()}
    # Username lookup for display, seeded from the CSV users and extended with
    # usernames discovered while listing current grants (users not in the CSV).
    id_to_username = {user.id: user.username for user in users.values()}
    reconcilable_entitlement_ids = {
        entitlement.id for entitlement in entitlements.values() if not entitlement.is_default
    }

    grants: dict[str, list[str]] = {}
    revocations: dict[str, list[str]] = {}
    current_by_user: dict[str, str] = {}

    def username_for(user_id: str) -> str:
        return id_to_username.get(user_id, user_id)

    def entitlement_name_for(entitlement_id: str) -> str:
        entitlement = entitlement_by_id.get(entitlement_id)
        return entitlement.name if entitlement is not None else entitlement_id

    def is_default(entitlement_id: str) -> bool:
        entitlement = entitlement_by_id.get(entitlement_id)
        return entitlement is not None and entitlement.is_default

    # Current grants come from the entitlement API because users.v1.GetUser only
    # returns user identity data.
    for entitlement_id in reconcilable_entitlement_ids:
        for username, current_user_id in client.entitlement_grants(entitlement_id).items():
            id_to_username.setdefault(current_user_id, username)
            current_by_user[current_user_id] = entitlement_id

    # (1) Users listed in the CSV: grant the desired entitlement, revoking any
    # existing (different, non-default) grant so the create is not skipped.
    for user_id, desired_entitlement_id in desired_by_user.items():
        current = current_by_user.get(user_id)
        if current == desired_entitlement_id:
            continue
        # current is only set for explicit non-default grants; a user on the
        # default has current=None and needs only the new grant.
        if current is not None and not is_default(current):
            revocations.setdefault(current, []).append(user_id)
        if desired_entitlement_id is None:
            continue
        grants.setdefault(desired_entitlement_id, []).append(user_id)

    # (2) Users currently holding any non-default entitlement but not in the CSV.
    for current_user_id, entitlement_id in current_by_user.items():
        if current_user_id not in desired_by_user:
            revocations.setdefault(entitlement_id, []).append(current_user_id)

    plan = ReconcilePlan(
        grants={k: sorted(set(v)) for k, v in grants.items()},
        revocations={k: sorted(set(v)) for k, v in revocations.items()},
    )
    for entitlement_id, user_ids in plan.grants.items():
        for user_id in user_ids:
            plan.grant_changes.append(
                GrantChange(entitlement_name_for(entitlement_id), username_for(user_id))
            )
    for entitlement_id, user_ids in plan.revocations.items():
        for user_id in user_ids:
            plan.revoke_changes.append(
                GrantChange(entitlement_name_for(entitlement_id), username_for(user_id))
            )
    return plan


def sync_entitlement_grants(
    client: SourcegraphClient, rows: list[tuple[str, str]], *, dry_run: bool
) -> SyncResult:
    entitlements = client.entitlements()
    users = client.users({user_identifier for user_identifier, _ in rows})
    resolved = resolve_desired_grants(rows, entitlements, users)
    plan = plan_reconciliation(client, resolved.desired, entitlements, users)

    if dry_run:
        return SyncResult(
            to_grant=plan.grant_changes,
            to_revoke=plan.revoke_changes,
            missing_entitlements=resolved.missing_entitlements,
            missing_users=resolved.missing_users,
            default_entitlements=resolved.default_entitlements,
        )

    # Revoke first so that users moving between entitlements are released from
    # their previous entitlement before the new grant is created.
    revoked_count = 0
    for entitlement_id, user_ids in plan.revocations.items():
        client.delete_entitlement_grants(entitlement_id, user_ids)
        revoked_count += len(user_ids)

    skipped_users: list[str] = []
    for entitlement_id, user_ids in plan.grants.items():
        skipped_users.extend(client.create_entitlement_grants(entitlement_id, user_ids))

    return SyncResult(
        to_grant=plan.grant_changes,
        to_revoke=plan.revoke_changes,
        granted_count=len(plan.grant_changes) - len(skipped_users),
        revoked_count=revoked_count,
        skipped_users=skipped_users,
        missing_entitlements=resolved.missing_entitlements,
        missing_users=resolved.missing_users,
        default_entitlements=resolved.default_entitlements,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign Sourcegraph DEEP_SEARCH entitlements from an email/entitlement CSV."
    )
    parser.add_argument("csv", nargs="?", type=Path, default=Path("user-entitlements.csv"))
    parser.add_argument("--sourcegraph-url", default=os.environ.get("SRC_ENDPOINT"))
    parser.add_argument("--token", default=os.environ.get("SRC_ACCESS_TOKEN"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the plan (grant and revoke). Defaults to dry-run mode.",
    )
    args = parser.parse_args()

    if not args.sourcegraph_url:
        parser.error("--sourcegraph-url or SRC_ENDPOINT is required")
    if not args.token:
        parser.error("--token or SRC_ACCESS_TOKEN is required")

    rows = read_user_entitlements(args.csv)
    result = sync_entitlement_grants(
        SourcegraphClient(args.sourcegraph_url, args.token), rows, dry_run=not args.apply
    )

    if not args.apply:
        print(
            f"Would grant {result.planned_grant_count} and revoke "
            f"{result.planned_revoke_count} entitlement grant(s)."
        )
        for change in result.to_grant:
            print(f"  + grant  {change.username} -> {change.entitlement}")
        for change in result.to_revoke:
            print(f"  - revoke {change.username} from {change.entitlement}")
    else:
        print(
            f"Granted {result.granted_count} and revoked "
            f"{result.revoked_count} entitlement grant(s)."
        )
        if result.skipped_users:
            print(f"Skipped existing grants: {', '.join(sorted(result.skipped_users))}")

    if result.default_entitlements:
        print(
            "Default entitlement(s) cannot be assigned per user and were skipped: "
            f"{', '.join(sorted(result.default_entitlements))}",
            file=sys.stderr,
        )
    if result.missing_entitlements:
        print(f"Missing entitlement(s): {', '.join(sorted(result.missing_entitlements))}", file=sys.stderr)
    if result.missing_users:
        print(f"Missing user(s): {', '.join(sorted(result.missing_users))}", file=sys.stderr)
    if result.missing_entitlements or result.missing_users or result.default_entitlements:
        raise SystemExit(1)

import csv
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from entitlement_sync.main import (
    Entitlement,
    SourcegraphClient,
    UserInfo,
    read_user_entitlements,
    resolve_desired_grants,
    sync_entitlement_grants,
)


def ents(*names: str, default: str | None = None) -> dict[str, Entitlement]:
    """Build an entitlement map, marking `default` (if any) as the default entitlement."""
    result: dict[str, Entitlement] = {}
    for i, name in enumerate(names, start=1):
        result[name] = Entitlement(id=f"E{i}", name=name, is_default=name == default)
    return result


class EntitlementSyncTest(unittest.TestCase):
    def test_read_user_entitlements_rejects_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            with path.open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["email"])
                writer.writeheader()
                writer.writerow({"email": "alice@example.com"})

            with self.assertRaisesRegex(ValueError, "entitlement"):
                read_user_entitlements(path)

    def test_resolve_desired_grants_maps_user_to_entitlement(self) -> None:
        entitlements = ents("ds-tier-1", "ds-tier-2")
        users = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
            "bob@example.com": UserInfo(id="U2", username="bob"),
            "charlie@example.com": UserInfo(id="U3", username="charlie"),
        }

        resolved = resolve_desired_grants(
            [
                ("alice@example.com", "ds-tier-1"),
                ("bob@example.com", "ds-tier-2"),
                ("missing@example.com", "ds-tier-2"),
                ("charlie@example.com", "unknown"),
            ],
            entitlements,
            users,
        )

        self.assertEqual(resolved.desired, {"U1": "E1", "U2": "E2"})
        self.assertEqual(resolved.missing_entitlements, {"unknown"})
        self.assertEqual(resolved.missing_users, {"missing@example.com"})
        self.assertEqual(resolved.default_entitlements, set())

    def test_resolve_desired_grants_skips_default_entitlement(self) -> None:
        entitlements = ents("ds-tier-1", "ds-tier-2", default="ds-tier-1")
        users = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
            "bob@example.com": UserInfo(id="U2", username="bob"),
        }

        resolved = resolve_desired_grants(
            [("alice@example.com", "ds-tier-1"), ("bob@example.com", "ds-tier-2")],
            entitlements,
            users,
        )

        # alice targets the default entitlement, so she resolves to no explicit grant.
        self.assertEqual(resolved.desired, {"U1": None, "U2": "E2"})
        self.assertEqual(resolved.default_entitlements, {"ds-tier-1"})

    def test_resolve_desired_grants_last_row_wins(self) -> None:
        entitlements = ents("ds-tier-1", "ds-tier-2")
        users = {"alice@example.com": UserInfo(id="U1", username="alice")}

        resolved = resolve_desired_grants(
            [("alice@example.com", "ds-tier-1"), ("alice@example.com", "ds-tier-2")],
            entitlements,
            users,
        )

        self.assertEqual(resolved.desired, {"U1": "E2"})

    def test_sync_grants_new_users(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-1", "ds-tier-2")
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
            "bob@example.com": UserInfo(id="U2", username="bob"),
        }
        # No user currently holds a listed entitlement.
        client.entitlement_grants.return_value = {}
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-1"), ("bob@example.com", "ds-tier-2")],
            dry_run=False,
        )

        client.create_entitlement_grants.assert_any_call("E1", ["U1"])
        client.create_entitlement_grants.assert_any_call("E2", ["U2"])
        client.delete_entitlement_grants.assert_not_called()
        self.assertEqual(result.granted_count, 2)
        self.assertEqual(result.revoked_count, 0)

    def test_sync_skips_users_already_holding_desired_entitlement(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-1")
        # alice already holds E1; bob has no grant yet.
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
            "bob@example.com": UserInfo(id="U2", username="bob"),
        }
        client.entitlement_grants.return_value = {"alice": "U1"}
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-1"), ("bob@example.com", "ds-tier-1")],
            dry_run=False,
        )

        client.create_entitlement_grants.assert_called_once_with("E1", ["U2"])
        client.delete_entitlement_grants.assert_not_called()
        self.assertEqual(result.granted_count, 1)

    def test_sync_revokes_users_removed_from_csv(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-1")
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
        }
        # carol is granted E1 on the instance but no longer in the CSV -> revoke.
        client.entitlement_grants.return_value = {"alice": "U1", "carol": "U3"}
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-1")],
            dry_run=False,
        )

        client.delete_entitlement_grants.assert_called_once_with("E1", ["U3"])
        client.create_entitlement_grants.assert_not_called()
        self.assertEqual(result.granted_count, 0)
        self.assertEqual(result.revoked_count, 1)
        self.assertEqual(
            [(c.username, c.entitlement) for c in result.to_revoke],
            [("carol", "ds-tier-1")],
        )

    def test_sync_moves_user_between_entitlements(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-1", "ds-tier-2")
        # alice currently holds ds-tier-1 (E1) but the CSV now lists ds-tier-2 (E2).
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
        }
        client.entitlement_grants.side_effect = lambda entitlement_id: {
            "E1": {"alice": "U1"},
            "E2": {},
        }[entitlement_id]
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-2")],
            dry_run=False,
        )

        client.delete_entitlement_grants.assert_called_once_with("E1", ["U1"])
        client.create_entitlement_grants.assert_called_once_with("E2", ["U1"])
        self.assertEqual(result.granted_count, 1)
        self.assertEqual(result.revoked_count, 1)

    def test_sync_revokes_users_removed_from_csv_even_when_entitlement_absent_from_csv(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-2", "ds-tier-3")
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
        }
        client.entitlement_grants.side_effect = lambda entitlement_id: {
            "E1": {"alice": "U1"},
            "E2": {"user009": "U9"},
        }[entitlement_id]
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-2")],
            dry_run=False,
        )

        client.delete_entitlement_grants.assert_called_once_with("E2", ["U9"])
        client.create_entitlement_grants.assert_not_called()
        self.assertEqual(result.granted_count, 0)
        self.assertEqual(result.revoked_count, 1)
        self.assertEqual(
            [(c.username, c.entitlement) for c in result.to_revoke],
            [("user009", "ds-tier-3")],
        )

    def test_sync_never_lists_or_revokes_default_entitlement(self) -> None:
        client = Mock(spec=SourcegraphClient)
        # ds-tier-1 (E1) is the default entitlement; ds-tier-2 (E2) is not.
        client.entitlements.return_value = ents(
            "ds-tier-1", "ds-tier-2", default="ds-tier-1"
        )
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
            "bob@example.com": UserInfo(id="U2", username="bob"),
        }
        client.entitlement_grants.return_value = {}
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-1"), ("bob@example.com", "ds-tier-2")],
            dry_run=False,
        )

        # alice targeting the default entitlement is skipped; only bob is granted.
        client.create_entitlement_grants.assert_called_once_with("E2", ["U2"])
        client.delete_entitlement_grants.assert_not_called()
        # The default entitlement's grant list must never be queried.
        client.entitlement_grants.assert_called_once_with("E2")
        self.assertEqual(result.default_entitlements, {"ds-tier-1"})
        self.assertEqual(result.granted_count, 1)

    def test_sync_revokes_user_moved_to_default_entitlement(self) -> None:
        client = Mock(spec=SourcegraphClient)
        # ds-tier-1 (E1) is the default entitlement; ds-tier-3 (E2) is not.
        client.entitlements.return_value = ents(
            "ds-tier-1", "ds-tier-3", default="ds-tier-1"
        )
        client.users.return_value = {
            "user009@example.com": UserInfo(id="U9", username="user009"),
        }
        client.entitlement_grants.return_value = {"user009": "U9"}
        client.create_entitlement_grants.return_value = []

        result = sync_entitlement_grants(
            client,
            [("user009@example.com", "ds-tier-1")],
            dry_run=False,
        )

        client.delete_entitlement_grants.assert_called_once_with("E2", ["U9"])
        client.create_entitlement_grants.assert_not_called()
        # Only the non-default entitlement's grant list is queried.
        client.entitlement_grants.assert_called_once_with("E2")
        self.assertEqual(result.default_entitlements, {"ds-tier-1"})
        self.assertEqual(result.granted_count, 0)
        self.assertEqual(result.revoked_count, 1)

    def test_dry_run_reports_grants_and_revocations_without_mutating(self) -> None:
        client = Mock(spec=SourcegraphClient)
        client.entitlements.return_value = ents("ds-tier-1")
        client.users.return_value = {
            "alice@example.com": UserInfo(id="U1", username="alice"),
        }
        client.entitlement_grants.return_value = {"carol": "U3"}

        result = sync_entitlement_grants(
            client,
            [("alice@example.com", "ds-tier-1")],
            dry_run=True,
        )

        client.create_entitlement_grants.assert_not_called()
        client.delete_entitlement_grants.assert_not_called()
        self.assertEqual(result.planned_grant_count, 1)
        self.assertEqual(result.planned_revoke_count, 1)
        self.assertEqual(
            [(c.username, c.entitlement) for c in result.to_grant],
            [("alice", "ds-tier-1")],
        )
        self.assertEqual(
            [(c.username, c.entitlement) for c in result.to_revoke],
            [("carol", "ds-tier-1")],
        )

    def test_users_uses_users_api_and_extracts_user_id(self) -> None:
        client = SourcegraphClient("https://sourcegraph.example.com", "token")
        client.api_post = Mock(  # type: ignore[method-assign]
            return_value={
                "name": "users/123",
                "username": "alice",
            }
        )

        users = client.users({"alice@example.com"})

        client.api_post.assert_called_once_with(
            "/api/users.v1.Service/GetUser",
            {"name": "users/alice@example.com"},
        )
        self.assertEqual(users, {"alice@example.com": UserInfo(id="123", username="alice")})

    def test_entitlement_grants_uses_numeric_database_id(self) -> None:
        client = SourcegraphClient("https://sourcegraph.example.com", "token")
        client.graphql = Mock(  # type: ignore[method-assign]
            return_value={
                "node": {
                    "userGrants": {
                        "nodes": [{"databaseID": 123, "username": "alice"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

        grants = client.entitlement_grants("E1")

        self.assertEqual(grants, {"alice": "123"})

    def test_sourcegraph_client_uses_truststore_ssl_context(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"data": {"ok": true}}'

        context = ssl.create_default_context()
        with patch("entitlement_sync.main.truststore.SSLContext", return_value=context) as ssl_context, patch(
            "entitlement_sync.main.urllib.request.urlopen", return_value=response
        ) as urlopen:
            data = SourcegraphClient("https://sourcegraph.example.com", "token").graphql("query Test { ok }")

        ssl_context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        urlopen.assert_called_once()
        self.assertIs(urlopen.call_args.kwargs["context"], context)
        self.assertEqual(data, {"ok": True})


if __name__ == "__main__":
    unittest.main()

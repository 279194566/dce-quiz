from __future__ import annotations

import json
import unittest

import httpx

from quiz_app.dce import DCEError, RealDCEClient


class RealDCEClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memberships: set[tuple[str, str]] = set()
        self.post_count = 0
        self.updated_user_body: dict | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/current-user") and request.method == "GET":
                return httpx.Response(200, json={"username": "alice"})
            if path.endswith("/users") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={"items": [{"id": "user-1", "name": "alice", "enabled": True}]},
                )
            if path.endswith("/users/user-1") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": "user-1",
                        "name": "alice",
                        "email": "alice@example.test",
                        "description": "test user",
                        "firstname": "Alice",
                        "lastname": "Example",
                        "enabled": True,
                    },
                )
            if path.endswith("/users/user-1") and request.method == "PUT":
                self.updated_user_body = json.loads(request.content)
                return httpx.Response(200, json={})
            if path.endswith("/users/user-1/groups") and request.method == "GET":
                items = [
                    {"id": group_id, "name": "quiz-xg-viewer"}
                    for group_id, user_id in self.memberships
                    if user_id == "user-1"
                ]
                return httpx.Response(200, json={"items": items})
            if path.endswith("/groups/group-xg") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "group": {
                            "id": "group-xg",
                            "name": "quiz-xg-viewer",
                            "description": "xg viewer",
                        }
                    },
                )
            if path.endswith("/groups/group-xg/members/user-1") and request.method == "POST":
                self.post_count += 1
                self.memberships.add(("group-xg", "user-1"))
                return httpx.Response(200, json={})
            if path.endswith("/groups/group-xg/members/user-1") and request.method == "DELETE":
                self.memberships.discard(("group-xg", "user-1"))
                return httpx.Response(200, json={})
            if path.endswith("/groups/missing"):
                return httpx.Response(404, json={"message": "not found"})
            if path.endswith("/users/deleted"):
                return httpx.Response(400, json={"code": 3, "message": "404 Not Found: User not found"})
            return httpx.Response(500, json={"message": f"unhandled {request.method} {path}"})

        self.client = RealDCEClient(
            base_url="https://dce.example.test",
            access_token="secret-token",
            transport=httpx.MockTransport(handler),
        )

    def test_normalizes_users_and_group_response(self) -> None:
        users = self.client.list_users()
        self.assertEqual("alice", users[0]["username"])
        self.assertEqual("group-xg", self.client.get_group("group-xg")["id"])
        self.assertIsNone(self.client.get_group("missing"))

    def test_add_membership_is_idempotent(self) -> None:
        self.client.add_user_to_group("group-xg", "user-1")
        self.client.add_user_to_group("group-xg", "user-1")
        self.assertEqual(1, self.post_count)
        self.assertTrue(self.client.is_user_in_group("group-xg", "user-1"))
        self.client.remove_user_from_group("group-xg", "user-1")
        self.client.remove_user_from_group("group-xg", "user-1")
        self.assertFalse(self.client.is_user_in_group("group-xg", "user-1"))

    def test_update_user_enabled_preserves_profile_fields(self) -> None:
        self.client.update_user_enabled("user-1", False)
        self.assertEqual(
            {
                "email": "alice@example.test",
                "description": "test user",
                "firstname": "Alice",
                "lastname": "Example",
                "enabled": False,
            },
            self.updated_user_body,
        )

    def test_server_failure_is_retryable_and_does_not_leak_token(self) -> None:
        with self.assertRaises(DCEError) as raised:
            self.client.get_user("unknown")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret-token", str(raised.exception))

    def test_ghippo_400_not_found_is_normalized(self) -> None:
        self.assertIsNone(self.client.get_user("deleted"))
        self.client.delete_user("deleted")

    def test_current_identity_needs_only_current_user_endpoint(self) -> None:
        identity = self.client.get_current_identity()
        self.assertEqual("alice", identity["username"])
        self.assertEqual("", identity["subject"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from src.collections import CollectionWorkspace, register_collection_routes
from src.infra.http.testing import request_over_uvicorn
from src.permissions import PermissionService, register_permission_routes
from src.platform import PlatformApp
from src.shared import Identifier, Principal, Request, ResourceAccess


NOW = datetime(2026, 8, 27, tzinfo=UTC)
COLLECTION_KEYS = {
    "collectionId", "name", "ownerUserId", "parentId", "visibility", "status"
}
PAGE_KEYS = {
    "content", "page", "size", "totalElements", "totalPages", "first", "last"
}
DIRECT_PERMISSION_KEYS = {
    "permissionId", "permissionKind", "targetType", "userId", "departmentId",
    "roleCode", "expiresAt",
}
USER_SEARCH_KEYS = {
    "userId", "email", "name", "departmentId", "departmentName"
}


class Documents:
    def __init__(self) -> None:
        self.rows: dict[Identifier, ResourceAccess] = {
            1: ResourceAccess(1, 1, "PRIVATE", "INDEXED"),
            2: ResourceAccess(2, 1, "PUBLIC", "INDEXED"),
        }

    def document_access(self, document_id, *, include_deleted=False):
        row = self.rows.get(document_id)
        if row is None or (row.status == "DELETED" and not include_deleted):
            return None
        return row

    def document_ids(self):
        return frozenset(self.rows)


PRINCIPALS = {
    "owner": Principal("owner", frozenset({"USER"}), user_id=1),
    "reader": Principal("reader", frozenset({"USER"}), user_id=2),
    "admin": Principal("admin", frozenset({"USER", "ADMIN"}), user_id=3),
}


class AccessRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = Documents()
        self.permissions = PermissionService(self.documents, clock=lambda: NOW)
        self.collections = CollectionWorkspace(
            access_decider=self.permissions.collection_decider,
            documents=self.documents,
            permission_reader=self.permissions,
            permission_cleanup=self.permissions,
            invalidate_document_cache=self.permissions.invalidate_document_cache,
        )
        self.permissions.collections = self.collections
        self.searches: list[str] = []

        def search(keyword: str):
            self.searches.append(keyword)
            return (
                {
                    "userId": 2,
                    "email": "reader@example.com",
                    "name": "Reader",
                    "departmentId": 7,
                    "departmentName": "Search",
                    "passwordHash": "must-not-leak",
                },
            )

        self.app = PlatformApp(
            principal_resolver=lambda request: PRINCIPALS.get(request.header("x-user") or "")
        )
        register_collection_routes(self.app, self.collections)
        register_permission_routes(self.app, self.permissions, user_search=search)

    def call(self, method: str, path: str, user: str | None = "owner", body=None):
        headers = {"x-user": user} if user else {}
        raw = b"" if body is None else json.dumps(body).encode()
        if raw:
            headers["Content-Type"] = "application/json"
        return request_over_uvicorn(
            self.app.handle, Request(method, path, headers=headers, body=raw)
        )

    @staticmethod
    def data(response):
        return json.loads(response.body)["data"]

    @staticmethod
    def code(response):
        return json.loads(response.body)["code"]

    def create_collection(self, name="root", user="owner", **values):
        response = self.call("POST", "/collections", user, {"name": name, **values})
        self.assertEqual(201, response.status)
        return self.data(response)

    def grant(self, path: str, kind: str, **target):
        return self.call(
            "POST",
            path,
            "owner",
            {"permissionKind": kind, **target},
        )

    def test_AC_COL_005_http_create_get_list_auth_and_validation(self):
        unauthenticated = self.call("POST", "/collections", None, {"name": "Alpha"})
        created = self.create_collection("Alpha")
        listed = self.call("GET", "/collections?keyword=Al&page=0&size=1")
        fetched = self.call("GET", f"/collections/{created['collectionId']}")
        invalid_query = self.call("GET", "/collections?size=101")
        invalid_path = self.call("GET", "/collections/not-an-id")
        invalid_body = self.call("POST", "/collections", body={"name": "x", "extra": 1})

        self.assertEqual((401, "COMMON-007"), (unauthenticated.status, self.code(unauthenticated)))
        self.assertEqual(COLLECTION_KEYS, set(created))
        self.assertEqual("PRIVATE", created["visibility"])
        fetched_data = self.data(fetched)
        listed_data = self.data(listed)
        self.assertEqual(COLLECTION_KEYS, set(fetched_data))
        self.assertEqual("Alpha", fetched_data["name"])
        self.assertEqual(PAGE_KEYS, set(listed_data))
        self.assertEqual(COLLECTION_KEYS, set(listed_data["content"][0]))
        self.assertEqual(1, listed_data["totalElements"])
        self.assertEqual((400, "COMMON-002"), (invalid_query.status, self.code(invalid_query)))
        self.assertEqual((400, "COMMON-002"), (invalid_path.status, self.code(invalid_path)))
        self.assertEqual((400, "COMMON-002"), (invalid_body.status, self.code(invalid_body)))

    def test_AC_COL_004_http_children_are_one_hop(self):
        root = self.create_collection("root")
        child = self.create_collection("child", parentId=root["collectionId"])
        self.create_collection("grandchild", parentId=child["collectionId"])
        response = self.call("GET", f"/collections/{root['collectionId']}/children")
        content = self.data(response)
        self.assertEqual(COLLECTION_KEYS, set(content[0]))
        self.assertEqual([child["collectionId"]], [item["collectionId"] for item in content])

    def test_AC_COL_003_http_document_mapping_and_read_filter(self):
        collection = self.create_collection()
        path = f"/collections/{collection['collectionId']}/documents"
        added = self.call("POST", path, body={"documentId": 1})
        duplicate = self.call("POST", path, body={"documentId": 1})
        listed = self.call("GET", path)
        removed = self.call("DELETE", f"{path}/1")
        missing = self.call("DELETE", f"{path}/1")
        invalid = self.call("POST", path, body={"documentId": True})

        self.assertEqual(201, added.status)
        self.assertEqual({"success", "status", "timestamp"}, set(json.loads(added.body)))
        self.assertEqual((409, "COLLECTION-002"), (duplicate.status, self.code(duplicate)))
        self.assertEqual([{"documentId": 1}], self.data(listed))
        self.assertEqual({"documentId"}, set(self.data(listed)[0]))
        self.assertEqual((204, b""), (removed.status, removed.body))
        self.assertEqual((404, "COLLECTION-003"), (missing.status, self.code(missing)))
        self.assertEqual((400, "COMMON-002"), (invalid.status, self.code(invalid)))

    def test_AC_COL_006_http_visibility_initial_and_mutation_asymmetry(self):
        collection = self.create_collection(visibility="COLLECTION")
        path = f"/collections/{collection['collectionId']}/visibility"
        rejected = self.call("PATCH", path, body={"visibility": "COLLECTION"})
        updated = self.call("PATCH", path, body={"visibility": "PUBLIC"})
        fetched = self.call("GET", f"/collections/{collection['collectionId']}", "reader")
        self.assertEqual((400, "COLLECTION-004"), (rejected.status, self.code(rejected)))
        self.assertEqual((204, b""), (updated.status, updated.body))
        self.assertEqual("PUBLIC", self.data(fetched)["visibility"])

    def test_AC_COL_001_http_delegated_admin_still_cannot_delete(self):
        collection = self.create_collection()
        granted = self.grant(
            f"/permissions/collections/{collection['collectionId']}",
            "ADMIN",
            targetType="USER",
            userId=2,
        )
        denied = self.call(
            "DELETE", f"/collections/{collection['collectionId']}", "reader"
        )
        deleted = self.call("DELETE", f"/collections/{collection['collectionId']}")
        self.assertEqual(201, granted.status)
        self.assertEqual((403, "ROLE-002"), (denied.status, self.code(denied)))
        self.assertEqual((204, b""), (deleted.status, deleted.body))

    def test_AC_COL_002_http_delete_cascades_foreign_owned_child(self):
        root = self.create_collection()
        self.grant(
            f"/permissions/collections/{root['collectionId']}",
            "WRITE",
            targetType="USER",
            userId=2,
        )
        child = self.create_collection("foreign", "reader", parentId=root["collectionId"])
        deleted = self.call("DELETE", f"/collections/{root['collectionId']}")
        listed = self.call("GET", "/collections", "reader")
        child_get = self.call("GET", f"/collections/{child['collectionId']}", "reader")
        self.assertEqual(204, deleted.status)
        self.assertEqual([], self.data(listed)["content"])
        self.assertEqual((404, "COLLECTION-001"), (child_get.status, self.code(child_get)))

    def test_AC_PERM_005_http_owner_effective_permissions(self):
        response = self.call("GET", "/permissions/documents/1/me")
        self.assertEqual(
            {"canRead": True, "canWrite": True, "canAdmin": True, "sources": ["OWNER"]},
            self.data(response),
        )
        self.assertEqual({"canRead", "canWrite", "canAdmin", "sources"}, set(self.data(response)))

    def test_AC_PERM_006_http_public_effective_permissions(self):
        response = self.call("GET", "/permissions/documents/2/me", "reader")
        self.assertEqual(
            {"canRead": True, "canWrite": False, "canAdmin": False, "sources": ["PUBLIC"]},
            self.data(response),
        )
        self.assertEqual({"canRead", "canWrite", "canAdmin", "sources"}, set(self.data(response)))

    def test_AC_PERM_001_http_USER_role_target_rejected(self):
        response = self.grant(
            "/permissions/documents/1",
            "READ",
            targetType="ROLE",
            roleCode="USER",
        )
        self.assertEqual((400, "PERMISSION-004"), (response.status, self.code(response)))

    def test_AC_PERM_002_http_target_body_path_and_keyword_validation(self):
        mismatch = self.grant(
            "/permissions/documents/1",
            "READ",
            targetType="USER",
            userId=2,
            roleCode="READER",
        )
        invalid_path = self.call("GET", "/permissions/documents/nope/me")
        missing_keyword = self.call("GET", "/permissions/documents/1/users")
        long_keyword = self.call(
            "GET", f"/permissions/documents/1/users?keyword={'x' * 101}"
        )
        self.assertEqual((400, "PERMISSION-001"), (mismatch.status, self.code(mismatch)))
        for response in (invalid_path, missing_keyword, long_keyword):
            self.assertEqual((400, "COMMON-002"), (response.status, self.code(response)))

    def test_AC_PERM_003_http_lists_expired_and_mounts_collection_and_user_paths(self):
        collection = self.create_collection()
        expired = self.grant(
            "/permissions/documents/1",
            "READ",
            targetType="USER",
            userId=2,
            expiresAt=(NOW - timedelta(seconds=1)).isoformat(),
        )
        collection_grant = self.grant(
            f"/permissions/collections/{collection['collectionId']}",
            "READ",
            targetType="USER",
            userId=2,
        )
        document_list = self.call("GET", "/permissions/documents/1")
        collection_list = self.call(
            "GET", f"/permissions/collections/{collection['collectionId']}"
        )
        document_users = self.call("GET", "/permissions/documents/1/users?keyword=Read")
        collection_users = self.call(
            "GET", f"/permissions/collections/{collection['collectionId']}/users?keyword=Read"
        )
        denied_users = self.call(
            "GET", "/permissions/documents/1/users?keyword=Read", "reader"
        )

        self.assertEqual(201, expired.status)
        self.assertEqual(DIRECT_PERMISSION_KEYS, set(self.data(expired)))
        self.assertIsNotNone(self.data(document_list)[0]["expiresAt"])
        self.assertEqual(DIRECT_PERMISSION_KEYS, set(self.data(document_list)[0]))
        self.assertEqual(DIRECT_PERMISSION_KEYS, set(self.data(collection_list)[0]))
        self.assertEqual(
            self.data(collection_grant)["permissionId"],
            self.data(collection_list)[0]["permissionId"],
        )
        self.assertEqual(2, self.searches.count("Read"))
        expected_user = {
            "userId": 2,
            "email": "reader@example.com",
            "name": "Reader",
            "departmentId": 7,
            "departmentName": "Search",
        }
        self.assertEqual([expected_user], self.data(document_users))
        self.assertEqual([expected_user], self.data(collection_users))
        self.assertEqual(USER_SEARCH_KEYS, set(self.data(document_users)[0]))
        self.assertEqual(USER_SEARCH_KEYS, set(self.data(collection_users)[0]))
        self.assertNotIn("passwordHash", self.data(document_users)[0])
        self.assertEqual((403, "ROLE-002"), (denied_users.status, self.code(denied_users)))

    def test_AC_PERM_004_http_document_and_collection_revoke_are_204(self):
        collection = self.create_collection()
        document_grant = self.grant(
            "/permissions/documents/1",
            "READ",
            targetType="USER",
            userId=2,
        )
        collection_grant = self.grant(
            f"/permissions/collections/{collection['collectionId']}",
            "READ",
            targetType="USER",
            userId=2,
        )
        self.permissions.readable_document_ids(PRINCIPALS["reader"], [1])
        document_id = self.data(document_grant)["permissionId"]
        collection_id = self.data(collection_grant)["permissionId"]
        revoked_document = self.call(
            "DELETE", f"/permissions/documents/1/{document_id}"
        )
        revoked_collection = self.call(
            "DELETE",
            f"/permissions/collections/{collection['collectionId']}/{collection_id}",
        )
        missing = self.call("DELETE", f"/permissions/documents/1/{document_id}")

        self.assertEqual((204, b""), (revoked_document.status, revoked_document.body))
        self.assertEqual((204, b""), (revoked_collection.status, revoked_collection.body))
        self.assertFalse(self.permissions.can_read_document(PRINCIPALS["reader"], 1))
        self.assertEqual((404, "PERMISSION-003"), (missing.status, self.code(missing)))

    def test_AC_SYS_007_collection_and_permission_report_first_body_field(self):
        collection = self.call(
            "POST",
            "/collections",
            body={"name": 7, "visibility": 8},
        )
        permission = self.call(
            "POST",
            "/permissions/documents/1",
            body={"permissionKind": 7, "targetType": 8},
        )

        collection_body = json.loads(collection.body)
        permission_body = json.loads(permission.body)
        self.assertEqual("name: 필수 문자열이며 공백일 수 없습니다.", collection_body["message"])
        self.assertNotIn("visibility", collection_body["message"])
        self.assertEqual("permissionKind: 필수 문자열이어야 합니다.", permission_body["message"])
        self.assertNotIn("targetType", permission_body["message"])


if __name__ == "__main__":
    unittest.main()

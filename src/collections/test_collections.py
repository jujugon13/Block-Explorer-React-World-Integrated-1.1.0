from __future__ import annotations

import unittest

from src.collections import CollectionWorkspace
from src.permissions import PermissionService
from src.shared import Principal, PublicError, ResourceAccess


def principal(user_id: int, *roles: str) -> Principal:
    return Principal(f"user-{user_id}@example.com", frozenset(roles), user_id=user_id)


class CollectionAcceptanceTests(unittest.TestCase):
    def assert_error(self, code: str, call) -> None:
        with self.assertRaises(PublicError) as raised:
            call()
        self.assertEqual(code, raised.exception.code)

    def test_AC_COL_001_delegated_admin_does_not_replace_owner(self):
        workspace = CollectionWorkspace(access_decider=lambda *_: True)
        collection = workspace.create(principal(1), "owned")
        delegated_admin = principal(2, "ADMIN")
        self.assert_error("ROLE-002", lambda: workspace.delete(delegated_admin, collection.id))
        self.assert_error(
            "ROLE-002",
            lambda: workspace.update_visibility(delegated_admin, collection.id, "PUBLIC"),
        )
        self.assert_error(
            "ROLE-002", lambda: workspace.remove_document(delegated_admin, collection.id, 99)
        )

    def test_AC_COL_002_foreign_owned_descendants_are_deleted(self):
        workspace = CollectionWorkspace(access_decider=lambda *_: True)
        root = workspace.create(principal(1), "root")
        child = workspace.create(principal(2), "child", parent_id=root.id)
        grandchild = workspace.create(principal(3), "grandchild", parent_id=child.id)
        workspace.delete(principal(1), root.id)
        for collection in (root, child, grandchild):
            self.assertEqual("DELETED", collection.status)
            self.assertIsNone(workspace.collection_access(collection.id))
            self.assertEqual(
                "DELETED", workspace.collection_access(collection.id, include_deleted=True).status
            )

    def test_AC_COL_002_cleanup_order_and_mapping_removal(self):
        events: list[str] = []
        holder: dict[str, CollectionWorkspace] = {}

        def invalidate(document_id):
            events.append(f"invalidate:{document_id}")
            self.assertEqual(frozenset({document_id}), holder["workspace"].document_ids_in_collection(1))

        def cleanup(collection_ids):
            events.append(f"permissions:{tuple(collection_ids)}")
            self.assertEqual(frozenset({10}), holder["workspace"].document_ids_in_collection(1))

        workspace = CollectionWorkspace(
            invalidate_document_cache=invalidate,
            cleanup_collection_permissions=cleanup,
        )
        holder["workspace"] = workspace
        collection = workspace.create(principal(1), "root")
        workspace.add_document(principal(1), collection.id, 10)
        events.clear()
        workspace.delete(principal(1), collection.id)
        self.assertEqual(["invalidate:10", "permissions:(1,)"], events)
        self.assertEqual(frozenset(), workspace.document_ids_in_collection(collection.id))

    def test_AC_COL_003_duplicate_and_missing_mapping_errors(self):
        workspace = CollectionWorkspace()
        collection = workspace.create(principal(1), "root")
        workspace.add_document(principal(1), collection.id, "doc")
        self.assert_error(
            "COLLECTION-002",
            lambda: workspace.add_document(principal(1), collection.id, "doc"),
        )
        self.assert_error(
            "COLLECTION-003",
            lambda: workspace.remove_document(principal(1), collection.id, "missing"),
        )

    def test_AC_COL_003_remove_mapping_prevents_cache_repopulation(self):
        class Documents:
            @staticmethod
            def document_access(document_id, *, include_deleted=False):
                if document_id != 10:
                    return None
                return ResourceAccess(10, 1, "PRIVATE", "INDEXED")

            @staticmethod
            def document_ids():
                return frozenset({10})

        documents = Documents()
        workspace = CollectionWorkspace(documents=documents)
        permissions = PermissionService(documents, workspace)
        workspace.bind_permissions(permissions)
        owner, reader = principal(1), principal(2)
        collection = workspace.create(owner, "root")
        workspace.add_document(owner, collection.id, 10)
        permissions.grant(
            owner,
            "COLLECTION",
            collection.id,
            "READ",
            target_type="USER",
            user_id=reader.user_id,
        )
        self.assertEqual(
            frozenset({10}), permissions.readable_document_ids(reader, [10])
        )

        original_invalidate = permissions.invalidate_document_cache
        observed_during_invalidation: list[frozenset[object]] = []

        def invalidate(document_id):
            original_invalidate(document_id)
            observed_during_invalidation.append(
                permissions.readable_document_ids(reader, [document_id])
            )

        workspace._invalidate_document_cache = invalidate
        workspace.remove_document(owner, collection.id, 10)

        self.assertEqual([frozenset()], observed_during_invalidation)
        self.assertEqual(frozenset(), permissions.readable_document_ids(reader, [10]))
        self.assertNotIn((reader.user_id, 10), permissions._user_cache)

    def test_AC_COL_004_children_are_direct_only(self):
        workspace = CollectionWorkspace()
        root = workspace.create(principal(1), "root")
        child = workspace.create(principal(1), "child", parent_id=root.id)
        workspace.create(principal(1), "grandchild", parent_id=child.id)
        self.assertEqual((child,), workspace.children(principal(1), root.id))

    def test_AC_COL_005_default_and_all_initial_visibilities(self):
        workspace = CollectionWorkspace()
        self.assertEqual("PRIVATE", workspace.create(principal(1), "default").visibility)
        for visibility in ("PRIVATE", "COLLECTION", "DEPARTMENT", "PUBLIC"):
            with self.subTest(visibility=visibility):
                self.assertEqual(
                    visibility,
                    workspace.create(principal(1), "same-name", visibility=visibility).visibility,
                )
        self.assertEqual(4, workspace.list(principal(1), keyword="same-name")["totalElements"])

    def test_AC_COL_006_mutation_allows_private_and_public_only(self):
        workspace = CollectionWorkspace()
        collection = workspace.create(principal(1), "root", visibility="COLLECTION")
        for visibility in ("PRIVATE", "PUBLIC"):
            workspace.update_visibility(principal(1), collection.id, visibility)
            self.assertEqual(visibility, collection.visibility)
        for visibility in ("COLLECTION", "DEPARTMENT"):
            with self.subTest(visibility=visibility):
                self.assert_error(
                    "COLLECTION-004",
                    lambda visibility=visibility: workspace.update_visibility(
                        principal(1), collection.id, visibility
                    ),
                )

    def test_AC_COL_003_documents_require_collection_and_document_read(self):
        live_checks: list[int] = []

        class Documents:
            def document_access(self, document_id, *, include_deleted=False):
                return ResourceAccess(document_id, 9, "PRIVATE", "ACTIVE")

            def document_ids(self):
                return frozenset({10, 11})

        class Permissions:
            def readable_document_ids(self, _principal, candidate_ids):
                return frozenset(candidate_ids)

            def live_readable_document_ids(self, _principal, candidate_ids):
                return frozenset(
                    document_id
                    for document_id in candidate_ids
                    if self.can_read_document(_principal, document_id)
                )

            def can_read_document(self, _principal, document_id):
                live_checks.append(document_id)
                return document_id == 11

        workspace = CollectionWorkspace(documents=Documents(), permission_reader=Permissions())
        collection = workspace.create(principal(1), "root")
        workspace.add_document(principal(1), collection.id, 10)
        workspace.add_document(principal(1), collection.id, 11)
        self.assertEqual((11,), workspace.documents(principal(1), collection.id))
        self.assertEqual({10, 11}, set(live_checks))

    def test_AC_COL_004_page_size_must_be_between_one_and_one_hundred(self):
        workspace = CollectionWorkspace()
        for size in (0, 101):
            with self.subTest(size=size):
                self.assert_error(
                    "COMMON-002", lambda size=size: workspace.list(principal(1), size=size)
                )


if __name__ == "__main__":
    unittest.main()

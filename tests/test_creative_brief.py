import copy
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cloud_video_studio.creative_brief import (
    artifact_sha256,
    artifact_path,
    default_brief,
    read_brief,
    save_brief,
)


def sourced_brief(project_id="p0"):
    brief = default_brief(project_id)
    brief.update({"audience": "New editors", "goal": "Explain a topic", "topic": "Example topic"})
    brief["sources"] = [{
        "id": "1" * 32,
        "source_type": "paper",
        "title": "Example source",
        "publisher": "Example publisher",
        "citation": "https://example.invalid/paper",
        "locator": "section 2",
        "notes": "Entered manually for this local fixture.",
    }]
    brief["claims"] = [{"id": "2" * 32, "text": "A cited example claim.", "source_ids": ["1" * 32]}]
    return brief


class CreativeBriefModelTests(unittest.TestCase):
    def test_brief_revision_transaction_waits_for_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            initial = save_brief(path, sourced_brief(), "p0")
            expected = artifact_sha256(initial)
            updated = copy.deepcopy(initial)
            updated["topic"] = "Cross-process writer"
            lock_path = path.with_name(f".{path.name}.lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            started = Path(directory) / "child-started"
            completed = Path(directory) / "child-completed"
            script = """
import json, sys
from pathlib import Path
from cloud_video_studio.creative_brief import save_brief
path, project_id, expected, payload, started, completed = sys.argv[1:]
Path(started).write_text('started')
save_brief(Path(path), json.loads(payload), project_id, expected_sha256=expected)
Path(completed).write_text('saved')
"""
            process = None
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                process = subprocess.Popen([
                    sys.executable, "-c", script, str(path), "p0", expected,
                    json.dumps(updated), str(started), str(completed),
                ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=os.environ.copy())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not started.exists() and process.poll() is None:
                    time.sleep(0.01)
                self.assertTrue(started.exists(), "child process did not reach the save transaction")
                time.sleep(0.1)
                self.assertIsNone(process.poll(), "brief save bypassed the cross-process revision lock")
            finally:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stdout.decode(), stderr.decode()))
            self.assertEqual(completed.read_text(), "saved")
            self.assertEqual(read_brief(path, "p0")["topic"], "Cross-process writer")

    def test_approval_requires_a_complete_brief_and_cited_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = sourced_brief()
            approved = save_brief(path, brief, "p0", approve=True)
            self.assertEqual(approved["review"]["status"], "approved")
            self.assertIsNotNone(approved["review"]["approved_at"])

            uncited = copy.deepcopy(brief)
            uncited["claims"][0]["source_ids"] = []
            with self.assertRaisesRegex(ValueError, "source citation"):
                save_brief(path, uncited, "p0", approve=True)
            self.assertEqual(read_brief(path, "p0")["review"]["status"], "approved")

    def test_unfinished_claim_can_be_saved_as_draft_but_not_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = sourced_brief()
            brief["claims"][0]["source_ids"] = []
            saved = save_brief(path, brief, "p0")
            self.assertEqual(saved["claims"][0]["source_ids"], [])
            self.assertEqual(saved["review"]["status"], "draft")
            with self.assertRaisesRegex(ValueError, "source citation"):
                save_brief(path, brief, "p0", approve=True)

    def test_project_id_limit_matches_project_model(self):
        long_project_id = "p" * 161
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), long_project_id)
            saved = save_brief(path, default_brief(long_project_id), long_project_id)
            self.assertEqual(read_brief(path, long_project_id), saved)

    def test_user_supplied_review_metadata_cannot_self_approve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = sourced_brief()
            brief["review"] = {"status": "approved", "approved_at": "2000-01-01T00:00:00+00:00"}
            saved = save_brief(path, brief, "p0", approve=False)
            self.assertEqual(saved["review"], {"status": "draft", "approved_at": None})

    def test_edit_after_approval_requires_a_new_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            approved = save_brief(path, sourced_brief(), "p0", approve=True)
            edited = sourced_brief()
            edited["topic"] = "Changed topic"
            saved = save_brief(path, edited, "p0")
            self.assertEqual(saved["review"]["status"], "draft")
            self.assertEqual(saved["created_at"], approved["created_at"])
            self.assertNotEqual(saved["updated_at"], approved["updated_at"])

    def test_rejects_claims_with_missing_sources_and_cross_project_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = sourced_brief()
            brief["claims"][0]["source_ids"] = ["3" * 32]
            with self.assertRaisesRegex(ValueError, "missing source"):
                save_brief(path, brief, "p0")
            with self.assertRaisesRegex(ValueError, "does not match"):
                save_brief(path, sourced_brief("another-project"), "p0")

    def test_rejects_unknown_fields_and_invalid_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = sourced_brief()
            brief["execute"] = "not supported"
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                save_brief(path, brief, "p0")
            brief = sourced_brief()
            brief["target_duration_seconds"] = True
            with self.assertRaisesRegex(ValueError, "duration"):
                save_brief(path, brief, "p0")
            brief = sourced_brief()
            brief["schema_version"] = True
            with self.assertRaisesRegex(ValueError, "schema version"):
                save_brief(path, brief, "p0")

    def test_oversized_artifact_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = artifact_path(Path(directory), "p0")
            brief = default_brief("p0")
            brief["sources"] = [{
                "id": f"{index + 1:032x}", "source_type": "other", "title": "Source",
                "publisher": "", "citation": f"reference-{index}", "locator": "", "notes": "n" * 1800,
            } for index in range(50)]
            brief["claims"] = [{
                "id": f"{index + 100:032x}", "text": "c" * 1800,
                "source_ids": ["0" * 30 + "01"],
            } for index in range(100)]
            with self.assertRaisesRegex(ValueError, "size limit"):
                save_brief(path, brief, "p0")
            self.assertFalse(path.exists())

    def test_artifact_path_is_fixed_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            path = artifact_path(root, "../../outside")
            self.assertEqual(path.parent, root)
            sentinel = Path(outside) / "sentinel.json"
            sentinel.write_text("{}", encoding="utf-8")
            path.symlink_to(sentinel)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_brief(path, "../../outside")
            with self.assertRaisesRegex(ValueError, "symlink"):
                save_brief(path, default_brief("../../outside"), "../../outside")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "{}")


class CreativeBriefHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        cls.process = subprocess.Popen(
            [sys.executable, str(root / "tools" / "preview_server.py"), "--media-root", cls.temp.name, "--port", str(cls.port)],
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError("preview server exited before the HTTP acceptance started")
            try:
                status, _ = cls.request("GET", "/creative-brief")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("preview server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()

    @classmethod
    def request(cls, method, path, body=None, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=3)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = origin if origin is not None else cls.origin
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw

    def test_browser_save_reload_approval_and_edit_cycle(self):
        status, initial = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        brief = sourced_brief(initial["brief"]["project_id"])
        brief["brief_id"] = initial["brief"]["brief_id"]

        status, saved = self.request("POST", "/creative-brief", {
            "action": "save", "brief": brief, "expected_sha256": initial["sha256"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(saved["brief"]["review"]["status"], "draft")
        status, loaded = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["brief"]["topic"], "Example topic")
        self.assertEqual(loaded["brief"]["claims"][0]["source_ids"], ["1" * 32])

        status, approved = self.request("POST", "/creative-brief", {
            "action": "approve", "brief": loaded["brief"], "expected_sha256": loaded["sha256"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(approved["brief"]["review"]["status"], "approved")
        status, persisted = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        self.assertEqual(persisted["brief"]["review"], approved["brief"]["review"])

        edited = persisted["brief"]
        edited["topic"] = "Revised topic"
        status, draft = self.request("POST", "/creative-brief", {
            "action": "save", "brief": edited, "expected_sha256": persisted["sha256"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(draft["brief"]["review"]["status"], "draft")
        self.assertNotEqual(draft["brief"]["review"]["approved_at"], approved["brief"]["review"]["approved_at"])

    def test_stale_revision_cannot_overwrite_or_approve_newer_brief(self):
        _, initial = self.request("GET", "/creative-brief")
        brief = sourced_brief(initial["brief"]["project_id"])
        brief["brief_id"] = initial["brief"]["brief_id"]
        status, first = self.request("POST", "/creative-brief", {
            "action": "save", "brief": brief, "expected_sha256": initial["sha256"],
        })
        self.assertEqual(status, 200)

        newer = copy.deepcopy(first["brief"])
        newer["topic"] = "Newest saved topic"
        status, current = self.request("POST", "/creative-brief", {
            "action": "save", "brief": newer, "expected_sha256": first["sha256"],
        })
        self.assertEqual(status, 200)

        stale = copy.deepcopy(first["brief"])
        stale["topic"] = "Stale topic"
        for action in ("save", "approve"):
            status, rejected = self.request("POST", "/creative-brief", {
                "action": action, "brief": stale, "expected_sha256": first["sha256"],
            })
            self.assertEqual(status, 409)
            self.assertEqual(rejected["error"], "BRIEF_REVISION_CONFLICT")
            self.assertEqual(rejected["current_sha256"], current["sha256"])
        _, persisted = self.request("GET", "/creative-brief")
        self.assertEqual(persisted["brief"]["topic"], "Newest saved topic")
        self.assertEqual(persisted["brief"]["review"]["status"], "draft")

    def test_null_or_malformed_revision_digest_is_rejected(self):
        _, initial = self.request("GET", "/creative-brief")
        brief = sourced_brief(initial["brief"]["project_id"])
        brief["brief_id"] = initial["brief"]["brief_id"]
        for digest in (None, True, "a" * 63, "A" * 64):
            with self.subTest(digest=digest):
                status, _ = self.request("POST", "/creative-brief", {
                    "action": "save", "brief": brief, "expected_sha256": digest,
                })
                self.assertEqual(status, 400)
        _, persisted = self.request("GET", "/creative-brief")
        self.assertEqual(persisted["sha256"], initial["sha256"])
        self.assertEqual(persisted["brief"], initial["brief"])

    def test_cross_origin_write_is_rejected_without_changing_saved_brief(self):
        _, initial = self.request("GET", "/creative-brief")
        brief = sourced_brief(initial["brief"]["project_id"])
        brief["brief_id"] = initial["brief"]["brief_id"]
        status, _ = self.request("POST", "/creative-brief", {
            "action": "save", "brief": brief, "expected_sha256": initial["sha256"],
        })
        self.assertEqual(status, 200)
        status, _ = self.request("POST", "/creative-brief", {
            "action": "approve", "brief": brief, "expected_sha256": initial["sha256"],
        }, origin="https://attacker.invalid")
        self.assertEqual(status, 403)
        status, loaded = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["brief"]["review"]["status"], "draft")
        self.assertEqual(loaded["brief"]["topic"], "Example topic")

    def test_bounded_brief_payload_can_exceed_other_local_json_endpoint_limit(self):
        _, initial = self.request("GET", "/creative-brief")
        brief = default_brief(initial["brief"]["project_id"])
        brief["sources"] = [{
            "id": f"{index + 1:032x}", "source_type": "other", "title": f"Source {index}",
            "publisher": "", "citation": f"local-fixture:{index}", "locator": "", "notes": "n" * 1600,
        } for index in range(50)]
        request = {"action": "save", "brief": brief, "expected_sha256": initial["sha256"]}
        self.assertGreater(len(json.dumps(request).encode()), 65536)
        status, saved = self.request("POST", "/creative-brief", request)
        self.assertEqual(status, 200)
        self.assertEqual(len(saved["brief"]["sources"]), 50)
        self.assertEqual(saved["brief"]["review"]["status"], "draft")


if __name__ == "__main__":
    unittest.main()

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

        status, saved = self.request("POST", "/creative-brief", {"action": "save", "brief": brief})
        self.assertEqual(status, 200)
        self.assertEqual(saved["brief"]["review"]["status"], "draft")
        status, loaded = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["brief"]["topic"], "Example topic")
        self.assertEqual(loaded["brief"]["claims"][0]["source_ids"], ["1" * 32])

        status, approved = self.request("POST", "/creative-brief", {"action": "approve", "brief": loaded["brief"]})
        self.assertEqual(status, 200)
        self.assertEqual(approved["brief"]["review"]["status"], "approved")
        status, persisted = self.request("GET", "/creative-brief")
        self.assertEqual(status, 200)
        self.assertEqual(persisted["brief"]["review"], approved["brief"]["review"])

        edited = persisted["brief"]
        edited["topic"] = "Revised topic"
        status, draft = self.request("POST", "/creative-brief", {"action": "save", "brief": edited})
        self.assertEqual(status, 200)
        self.assertEqual(draft["brief"]["review"]["status"], "draft")
        self.assertNotEqual(draft["brief"]["review"]["approved_at"], approved["brief"]["review"]["approved_at"])

    def test_cross_origin_write_is_rejected_without_changing_saved_brief(self):
        _, initial = self.request("GET", "/creative-brief")
        brief = sourced_brief(initial["brief"]["project_id"])
        brief["brief_id"] = initial["brief"]["brief_id"]
        status, _ = self.request("POST", "/creative-brief", {"action": "save", "brief": brief})
        self.assertEqual(status, 200)
        status, _ = self.request("POST", "/creative-brief", {"action": "approve", "brief": brief}, origin="https://attacker.invalid")
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
        self.assertGreater(len(json.dumps({"action": "save", "brief": brief}).encode()), 65536)
        status, saved = self.request("POST", "/creative-brief", {"action": "save", "brief": brief})
        self.assertEqual(status, 200)
        self.assertEqual(len(saved["brief"]["sources"]), 50)
        self.assertEqual(saved["brief"]["review"]["status"], "draft")


if __name__ == "__main__":
    unittest.main()

"""Functional tests for the six plugins added in 0.2.0.

Runs every plugin through the real engine (subprocess + stdio RPC) with the
offline mock model backend.  Deterministic plugins (links.check, forms.fill
profile path, images.dedupe) are asserted on exact behaviour; LLM plugins are
asserted on structure, id alignment, coercion and failure isolation — never on
model quality.  Image bytes come from tests/image_fixtures.py (stdlib only).
"""

import base64
import os
import shutil
import tempfile
import unittest

from hub.config import load_config
from hub.engine import Engine

from tests import image_fixtures


def gradient(width, height):
    return image_fixtures.png_bytes(width, height, lambda x, y: (x * 8 % 256, y * 8 % 256, 128))


def solid(width, height, shade=200):
    return image_fixtures.png_bytes(width, height, lambda x, y: (shade, 30, 30))


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


class NewPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="lah-newplugins-")
        cls._old_data = os.environ.get("LOCAL_AI_HUB_DATA")
        os.environ["LOCAL_AI_HUB_DATA"] = cls._tmp
        cls.engine = Engine(load_config({"ollama": {"mock": True}}))

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        if cls._old_data is None:
            os.environ.pop("LOCAL_AI_HUB_DATA", None)
        else:
            os.environ["LOCAL_AI_HUB_DATA"] = cls._old_data
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def task(self, plugin, payload, **extra):
        message = {"type": "task", "plugin": plugin, "input": payload}
        message.update(extra)
        result = self.engine.handle(message)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["meta"].get("output_schema_errors"), [])
        return result["output"]

    # ------------------------------------------------------------------
    # links.check — static audit, no model, no network
    # ------------------------------------------------------------------
    def test_links_check_kinds_and_external(self):
        out = self.task("links.check", {
            "page_url": "https://board.example.org/thread/1",
            "links": [
                {"id": "a", "href": "/thread/2"},
                {"id": "b", "href": "https://elsewhere.net/page"},
                {"id": "c", "href": "#top"},
                {"id": "d", "href": "mailto:mod@example.org"},
                {"id": "e", "href": "javascript:void(0)"},
                {"id": "f", "href": "javascript:alert(1)"},
            ],
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertEqual(by_id["a"]["kind"], "internal")
        self.assertFalse(by_id["a"]["external"])
        self.assertEqual(by_id["b"]["kind"], "external")
        self.assertTrue(by_id["b"]["external"])
        self.assertEqual(by_id["c"]["kind"], "anchor")
        self.assertEqual(by_id["d"]["kind"], "mailto")
        # javascript:void(0) is inert -> "empty"; real script URLs are flagged.
        self.assertEqual(by_id["e"]["kind"], "empty")
        self.assertEqual(by_id["f"]["kind"], "script")
        self.assertIn("script_url", by_id["f"]["issues"])
        self.assertEqual(by_id["f"]["risk"], 1.0)
        self.assertEqual(out["summary"]["network_requests"], 0)
        self.assertEqual(out["summary"]["total"], 6)

    def test_links_check_tracking_strip_and_dedupe(self):
        out = self.task("links.check", {
            "page_url": "https://board.example.org/",
            "links": [
                {"id": "l1", "href": "https://target.example/post?q=1"},
                {"id": "l2", "href": "https://target.example/post?q=1&utm_source=spam"},
            ],
        })
        first, second = out["results"]
        self.assertIn("tracking_params", second["issues"])
        self.assertEqual(first["normalized"], second["normalized"])
        self.assertEqual(second["duplicate_of"], "l1")
        self.assertIn("duplicate", second["issues"])
        self.assertEqual(out["summary"]["duplicates"], 1)
        self.assertEqual(out["summary"]["unique_targets"], 1)

    def test_links_check_risk_flags(self):
        out = self.task("links.check", {
            "page_url": "https://board.example.org/",
            "links": [
                {"id": "r1", "href": "http://bit.ly/abc", "target": "_blank"},
                {"id": "r2", "href": "https://evil.net/", "text": "go to good.example now"},
                {"id": "r3", "href": "https://user:pw@host.example/"},
            ],
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertIn("insecure_scheme", by_id["r1"]["issues"])
        self.assertIn("mixed_content", by_id["r1"]["issues"])
        self.assertIn("shortener", by_id["r1"]["issues"])
        self.assertIn("missing_noopener", by_id["r1"]["issues"])
        self.assertIn("text_domain_mismatch", by_id["r2"]["issues"])
        self.assertIn("userinfo_in_url", by_id["r3"]["issues"])
        self.assertGreaterEqual(by_id["r1"]["risk"], 0.4)
        self.assertIn("r1", out["summary"]["flagged_ids"])

    def test_links_check_caller_supplied_statuses(self):
        out = self.task("links.check", {
            "links": [
                {"id": "s1", "href": "https://a.example/gone"},
                {"id": "s2", "href": "https://b.example/broken"},
                {"id": "s3", "href": "https://c.example/moved"},
            ],
            "statuses": {"s1": {"status": 404}, "s2": {"status": 503}, "s3": {"status": 301}},
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertEqual(by_id["s1"]["status"], 404)
        self.assertIn("dead_link", by_id["s1"]["issues"])
        self.assertIn("server_error", by_id["s2"]["issues"])
        self.assertIn("redirect", by_id["s3"]["issues"])

    def test_links_check_allowlist(self):
        out = self.task("links.check", {
            "page_url": "https://board.example.org/",
            "allowlist_domains": ["friend.example"],
            "links": [
                {"id": "w1", "href": "https://friend.example/ok"},
                {"id": "w2", "href": "https://stranger.example/no"},
            ],
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertNotIn("not_allowlisted", by_id["w1"]["issues"])
        self.assertIn("not_allowlisted", by_id["w2"]["issues"])

    def test_links_check_rejects_non_array(self):
        result = self.engine.handle({"type": "task", "plugin": "links.check",
                                     "input": {"links": "not-an-array"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")

    # ------------------------------------------------------------------
    # forms.fill — profile matching, blocking, never-submit contract
    # ------------------------------------------------------------------
    def test_forms_fill_profile_match_via_autocomplete(self):
        out = self.task("forms.fill", {
            "fields": [
                {"id": "f1", "type": "email", "autocomplete": "email", "name": "contact"},
                {"id": "f2", "type": "text", "name": "full_name"},
            ],
            "profile": {"email": "me@example.com", "name": "Ada Lovelace"},
        }, options={"use_llm": False})
        by_id = {entry["id"]: entry for entry in out["suggestions"]}
        self.assertEqual(by_id["f1"]["value"], "me@example.com")
        self.assertEqual(by_id["f1"]["source"], "profile")
        self.assertEqual(by_id["f2"]["value"], "Ada Lovelace")
        self.assertEqual(out["summary"]["from_profile"], 2)

    def test_forms_fill_blocks_sensitive_fields(self):
        out = self.task("forms.fill", {
            "fields": [
                {"id": "p1", "type": "password", "name": "login"},
                {"id": "p2", "type": "text", "name": "card_number"},
                {"id": "p3", "type": "hidden", "name": "csrf"},
            ],
            "profile": {"login": "hunter2", "card_number": "4111111111111111"},
        }, options={"use_llm": False})
        self.assertEqual(out["suggestions"], [])
        self.assertEqual({entry["id"] for entry in out["skipped"]}, {"p1", "p2", "p3"})

    def test_forms_fill_preserves_existing_values(self):
        out = self.task("forms.fill", {
            "fields": [{"id": "f1", "type": "text", "autocomplete": "email",
                        "value": "already@set.example"}],
            "profile": {"email": "me@example.com"},
        }, options={"use_llm": False})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("already has a value", out["skipped"][0]["reason"])

    def test_forms_fill_model_path_and_option_validation(self):
        out = self.task("forms.fill", {
            "fields": [
                {"id": "m1", "type": "text", "name": "nickname"},
                {"id": "m2", "type": "select", "name": "country",
                 "options": ["DE", "US"]},
            ],
            "profile": {},
        }, options={"use_llm": True})
        by_id = {entry["id"]: entry for entry in out["suggestions"]}
        self.assertIn("m1", by_id)
        self.assertEqual(by_id["m1"]["source"], "model")
        # The mock's value is not one of the select's options -> rejected.
        self.assertNotIn("m2", by_id)
        reasons = {entry["id"]: entry["reason"] for entry in out["skipped"]}
        self.assertIn("options", reasons.get("m2", ""))

    # ------------------------------------------------------------------
    # images.dedupe — perceptual hashing on real generated pixels
    # ------------------------------------------------------------------
    def test_images_dedupe_exact_and_distinct(self):
        same = b64(gradient(32, 32))
        out = self.task("images.dedupe", {"images": [
            {"id": "i1", "data_base64": same},
            {"id": "i2", "data_base64": same},
            {"id": "i3", "data_base64": b64(solid(32, 32))},
        ]})
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertEqual(by_id["i2"]["duplicate_of"], "i1")
        self.assertEqual(by_id["i2"]["distance"], 0)
        self.assertEqual(by_id["i3"]["duplicate_of"], "")
        self.assertTrue(by_id["i1"]["keeper"])
        self.assertEqual(len(out["groups"]), 1)
        self.assertEqual(out["groups"][0]["keeper"], "i1")
        self.assertEqual(sorted(out["groups"][0]["members"]), ["i1", "i2"])

    def test_images_dedupe_brightness_change_dhash(self):
        out = self.task("images.dedupe", {
            "images": [
                {"id": "d1", "data_base64": b64(solid(32, 32, shade=120))},
                {"id": "d2", "data_base64": b64(solid(32, 32, shade=220))},
            ],
            "algorithm": "dhash",
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertEqual(by_id["d2"]["duplicate_of"], "d1")
        self.assertEqual(by_id["d2"]["distance"], 0)

    def test_images_dedupe_keep_largest(self):
        out = self.task("images.dedupe", {
            "images": [
                {"id": "small", "data_base64": b64(solid(16, 16))},
                {"id": "big", "data_base64": b64(solid(64, 64))},
            ],
            "algorithm": "dhash",
        }, options={"keep": "largest"})
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertTrue(by_id["big"]["keeper"])
        self.assertEqual(by_id["small"]["duplicate_of"], "big")

    def test_images_dedupe_reference_hashes(self):
        first = self.task("images.dedupe", {
            "images": [{"id": "orig", "data_base64": b64(solid(32, 32))}],
            "algorithm": "dhash",
        })
        known_hash = first["results"][0]["hash"]
        second = self.task("images.dedupe", {
            "images": [{"id": "repost", "data_base64": b64(solid(48, 48, shade=180))}],
            "reference_hashes": [{"id": "seen-1", "hash": known_hash, "algorithm": "dhash"}],
            "algorithm": "dhash",
        })
        entry = second["results"][0]
        self.assertTrue(entry.get("reference"))
        self.assertEqual(entry["duplicate_of"], "seen-1")

    def test_images_dedupe_bad_image_is_isolated(self):
        out = self.task("images.dedupe", {"images": [
            {"id": "ok", "data_base64": b64(solid(16, 16))},
            {"id": "bad", "data_base64": "!!!not-base64!!!"},
            {"id": "empty"},
        ]})
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertIn("hash", by_id["ok"])
        self.assertIn("error", by_id["bad"])
        self.assertIn("error", by_id["empty"])

    # ------------------------------------------------------------------
    # page.summarize — mock model, map-reduce chunking
    # ------------------------------------------------------------------
    def test_page_summarize_items_and_highlights(self):
        out = self.task("page.summarize", {
            "title": "A thread",
            "items": [{"id": "c%d" % i, "text": "comment number %d" % i} for i in range(4)],
        })
        self.assertIsInstance(out["summary"], str)
        self.assertTrue(out["summary"])
        self.assertIsInstance(out["bullets"], list)
        self.assertLessEqual(len(out["highlights"]), 3)
        self.assertTrue({h["id"] for h in out["highlights"]} <= {"c0", "c1", "c2", "c3"})
        self.assertGreaterEqual(out["stats"]["chunks"], 1)

    def test_page_summarize_long_text_is_chunked(self):
        out = self.task("page.summarize", {
            "text": "word " * 600,  # ~3000 chars
        }, options={"chunk_chars": 500, "max_chunks": 4})
        self.assertGreaterEqual(out["stats"]["chunks"], 2)
        self.assertTrue(out["summary"])

    def test_page_summarize_empty_input_fails(self):
        result = self.engine.handle({"type": "task", "plugin": "page.summarize", "input": {}})
        self.assertFalse(result["ok"])

    # ------------------------------------------------------------------
    # text.translate — id alignment, placeholder protection
    # ------------------------------------------------------------------
    def test_text_translate_ids_and_summary(self):
        out = self.task("text.translate", {
            "segments": [{"id": "s1", "text": "Hello world"},
                         {"id": "s2", "text": "Good night"}],
            "target_language": "de",
        })
        self.assertEqual(out["target_language"], "de")
        self.assertEqual([entry["id"] for entry in out["results"]], ["s1", "s2"])
        self.assertEqual(out["summary"]["total"], 2)
        for entry in out["results"]:
            self.assertTrue(entry["text"])

    def test_text_translate_protects_placeholders(self):
        out = self.task("text.translate", {
            "segments": [{"id": "p1", "text": "Hi {name}, welcome!"}],
            "target_language": "fr",
        })
        entry = out["results"][0]
        # The mock drops the protected token -> original kept, flagged.
        self.assertTrue(entry["unchanged"])
        self.assertEqual(entry["text"], "Hi {name}, welcome!")
        self.assertIn("protected", entry.get("error", ""))

    def test_text_translate_empty_segment(self):
        out = self.task("text.translate", {
            "segments": [{"id": "e1", "text": "   "}, {"id": "e2", "text": "real text"}],
        })
        by_id = {entry["id"]: entry for entry in out["results"]}
        self.assertTrue(by_id["e1"]["unchanged"])

    # ------------------------------------------------------------------
    # moderation.queue — triage + persistent reviewer ledger
    # ------------------------------------------------------------------
    def test_moderation_queue_mock_triage_structure(self):
        out = self.task("moderation.queue", {
            "items": [{"id": "m%d" % i, "text": "item %d" % i, "reports": i} for i in range(3)],
            "scope": "test/structure",
        })
        self.assertEqual({entry["id"] for entry in out["results"]}, {"m0", "m1", "m2"})
        for entry in out["results"]:
            # Labels must stay inside the allowlist (mock picks the first enum).
            self.assertIn(entry["label"], {"ok", "spam", "toxic", "off-topic", "unknown"})
            self.assertIn(entry["recommendation"], {"approve", "remove", "escalate", "review"})
            self.assertIn(entry.get("decision", ""), ("", "pending"))
        priorities = [entry["priority"] for entry in out["queue"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_moderation_queue_decisions_persist_across_tasks(self):
        items = [{"id": "keep1", "text": "fine"}, {"id": "drop1", "text": "spammy"}]
        first = self.task("moderation.queue", {
            "items": items,
            "scope": "test/persist",
            "decisions": [{"id": "drop1", "decision": "remove", "reviewer": "alice",
                           "note": "obvious spam"}],
        })
        by_id = {entry["id"]: entry for entry in first["results"]}
        self.assertEqual(by_id["drop1"]["decision"], "remove")
        self.assertEqual(by_id["drop1"]["reviewer"], "alice")
        self.assertEqual(by_id["drop1"]["recommendation"], "remove")

        # Second run: no decisions supplied — the ledger must remember.
        second = self.task("moderation.queue", {"items": items, "scope": "test/persist"})
        by_id = {entry["id"]: entry for entry in second["results"]}
        self.assertEqual(by_id["drop1"]["decision"], "remove")
        self.assertEqual(by_id["drop1"]["reviewer"], "alice")
        # A model label must never become a reviewer decision.
        self.assertIn(by_id["keep1"].get("decision", ""), ("", "pending"))

    def test_moderation_queue_scopes_are_isolated(self):
        self.task("moderation.queue", {
            "items": [{"id": "x1", "text": "hello"}],
            "scope": "test/scope-a",
            "decisions": [{"id": "x1", "decision": "remove", "reviewer": "bob"}],
        })
        out = self.task("moderation.queue", {
            "items": [{"id": "x1", "text": "hello"}],
            "scope": "test/scope-b",
        })
        self.assertIn(out["results"][0].get("decision", ""), ("", "pending"))


if __name__ == "__main__":
    unittest.main()

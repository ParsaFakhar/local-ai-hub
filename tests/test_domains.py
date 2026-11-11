import unittest

from hub.config import load_config
from hub.domains import DomainRegistry, domain_candidates, normalize_domain


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.registry = DomainRegistry(load_config())

    def test_normalization(self):
        self.assertEqual(normalize_domain("https://Example-Board.com:443/thread/1"), "example-board.com")
        self.assertEqual(normalize_domain("board.example.co.uk"), "board.example.co.uk")
        self.assertEqual(normalize_domain(None), "")

    def test_candidate_chain_order(self):
        chain = domain_candidates("a.example.com")
        self.assertEqual(chain[0], "*")
        self.assertLess(chain.index("example.com"), chain.index("a.example.com"))

    def test_bundled_example_config(self):
        config = self.registry.get("example-board.com")
        self.assertIn("comments.classify", config["features"])
        self.assertIn("images.tag", config["features"])
        self.assertTrue(config["features"]["comments.classify"]["enabled"])
        self.assertEqual(config["features"]["comments.classify"]["item_selector"], "p.comment")

    def test_subdomain_inherits_parent(self):
        config = self.registry.get("www.example-board.com")
        self.assertIn("comments.classify", config["features"])

    def test_unknown_domain_is_empty(self):
        self.assertEqual(self.registry.get("nothing-configured.test")["features"], {})

    def test_extension_override_wins(self):
        overrides = {"features": {"comments.classify": {"enabled": False, "item_selector": "div.x"}}}
        feature = self.registry.feature("example-board.com", "comments.classify", overrides)
        self.assertFalse(feature["enabled"])
        self.assertEqual(feature["item_selector"], "div.x")
        # untouched keys survive the merge
        self.assertEqual(feature["root_selector"], "div.comments")

    def test_enabled_plugins_listing(self):
        self.assertEqual(
            self.registry.enabled_plugins("example-board.com"), ["comments.classify", "images.tag"]
        )


if __name__ == "__main__":
    unittest.main()

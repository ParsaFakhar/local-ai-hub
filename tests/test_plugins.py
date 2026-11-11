import json
import tempfile
import unittest
from pathlib import Path

from hub.config import load_config
from hub.plugins import ManifestError, PluginManager, load_plugin


class PluginTests(unittest.TestCase):
    def test_bundled_plugins_discovered(self):
        manager = PluginManager(load_config())
        ids = [plugin.id for plugin in manager.list()]
        for expected in ("hello.python", "hello.javascript", "comments.classify", "images.tag"):
            self.assertIn(expected, ids)
        self.assertEqual(manager.errors, [])

    def test_runtime_entries_exist(self):
        for plugin in PluginManager(load_config()).list():
            self.assertTrue(plugin.entry.is_file(), plugin.entry)

    def _write(self, directory: Path, manifest: dict, entry: str = "plugin.py") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / entry).write_text("# stub\n")
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return directory

    def test_rejects_bad_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._write(
                Path(tmp) / "bad",
                {"id": "Bad Plugin!", "version": "1.0", "runtime": {"type": "python", "entry": "plugin.py"}},
            )
            with self.assertRaises(ManifestError):
                load_plugin(directory)

    def test_rejects_unsupported_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._write(
                Path(tmp) / "ruby",
                {"id": "hello.ruby", "version": "1.0", "runtime": {"type": "ruby", "entry": "plugin.py"}},
            )
            with self.assertRaises(ManifestError):
                load_plugin(directory)

    def test_rejects_escaping_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside.py").write_text("# stub\n")
            directory = self._write(
                root / "escape",
                {"id": "hello.escape", "version": "1.0", "runtime": {"type": "python", "entry": "../outside.py"}},
            )
            with self.assertRaises(ManifestError):
                load_plugin(directory)

    def test_rejects_missing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "missing"
            directory.mkdir()
            (directory / "manifest.json").write_text(
                json.dumps({"id": "hello.missing", "version": "1.0", "runtime": {"type": "python", "entry": "nope.py"}})
            )
            with self.assertRaises(ManifestError):
                load_plugin(directory)

    def test_disabled_plugins_are_hidden(self):
        manager = PluginManager(load_config({"plugins": {"disabled": ["hello.python"]}}))
        self.assertNotIn("hello.python", [plugin.id for plugin in manager.list()])


if __name__ == "__main__":
    unittest.main()

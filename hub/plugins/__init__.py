"""Plugin registry."""

from .manager import ManifestError, Plugin, PluginManager, load_plugin

__all__ = ["ManifestError", "Plugin", "PluginManager", "load_plugin"]

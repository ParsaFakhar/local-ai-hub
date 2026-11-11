# Plugin template

```bash
cp -r templates/plugin-template plugins/my.plugin
```

1. Edit `manifest.json`: change `id` (must be unique, lowercase), `name`,
   `description`, and the `input` / `output` schemas.
2. Edit `plugin.py` (or switch `runtime` to `javascript` and edit `plugin.js`).
3. Test it:
   ```bash
   ./bin/laihub plugins
   ./bin/laihub --mock task my.plugin --input '{"items":[{"id":"a","text":"hello"}]}'
   ```
4. Wire it to a site in **Settings → Domains** (Insert feature template) or in
   `config/domains/<domain>.json`.

Rules of the road:

* return `results[].id` matching the ids you were given — that is how the
  interface finds the right DOM node
* validate everything the model returns against your own allowlist
* never fetch page-supplied URLs and never execute model-produced strings
* use `hub.progress()` / `hub.log()` for anything longer than a second

Full reference: [docs/PLUGIN_API.md](../../docs/PLUGIN_API.md).

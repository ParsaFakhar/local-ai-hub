# Domain configuration

Each site gets its own configuration. Several plugins can be active on the same
site, each with its own selectors, options and result actions.

## Where configs live

| Location | Purpose |
| -------- | ------- |
| `config/domains/*.json` | bundled/shared configs (version-controlled) |
| `~/Library/Application Support/LocalAIHub/config/domains/*.json` | your private configs (survive updates) |
| Firefox **Settings → Domains** | per-site overrides stored in the extension |

Merge order (later wins): `*` defaults → parent domain → exact host → extension override.
`www.example-board.com` inherits `example-board.com`; `*.co.uk` style wildcards work too.

Set `"domains": {"allow_extension_overrides": false}` in `hub.json` to make file
configs authoritative.

Overrides are synced to the native host on every connect and after every
save/delete (`domains.set_overrides`, full-map replace). The host keeps them
**in memory only** and applies them to both `features` and `bridge`, so a bridge
opt-in edited here is enforced by the engine exactly like a file opt-in. With
`allow_extension_overrides: false` the host rejects the sync.

## Full example

```json
{
  "domain": "example-board.com",
  "label": "Example imageboard",
  "features": {
    "comments.classify": {
      "enabled": true,
      "auto_run": false,
      "root_selector": "div.comments",
      "item_selector": "p.comment",
      "id_prefix": "c",
      "max_items": 200,
      "fields": {
        "text":   { "attr": "text", "max_length": 2000 },
        "author": { "selector": ".author", "attr": "text" },
        "link":   { "selector": "a.permalink", "attr": "href" }
      },
      "input": { "comments": "@items", "page_title": "@page.title" },
      "options": { "labels": ["ok", "spam", "toxic", "off-topic"], "batch_size": 8 },
      "apply": {
        "results_path": "results",
        "match_by": "id",
        "actions": [
          { "action": "badge", "text": "{label} {confidence}", "title": "{reason}" },
          { "action": "attribute", "name": "data-lah-label", "value": "{label}" },
          { "action": "class", "map": { "spam": "lah-flag-spam", "toxic": "lah-flag-toxic" } },
          { "action": "hide", "when": { "field": "label", "in": ["spam"] } }
        ]
      }
    }
  }
}
```

## Feature keys

| Key | Meaning |
| --- | ------- |
| `enabled` | must be `true` for the feature to run (the engine also enforces this) |
| `auto_run` | run automatically on page load (requires the global `autoRun` setting) |
| `root_selector` | optional container(s); all matches are searched |
| `item_selector` | **required**; one task item per match |
| `id_prefix` | id prefix for `data-lah-id` (default: plugin id's first segment) |
| `max_items` | per-feature cap (default: global `maxItems`, 200) |
| `fields` | what to read from each item |
| `input` | how extracted data maps onto the plugin's input schema |
| `options` | plugin config overrides |
| `apply` | how results are written back to the page |
| `allow_text_replace` | opt-in for the `text` action (default `false`) |

## `fields`

```json
"fields": {
  "text":     { "attr": "text" },
  "author":   { "selector": ".name", "attr": "text" },
  "src":      { "attr": "src" },
  "file":     { "attr": "src", "transform": "basename" },
  "pixels":   { "attr": "src", "as": "image", "max_size": 768, "quality": 0.85 },
  "score":    { "attr": "data-score", "transform": "number" },
  "constant": { "attr": "text", "max_length": 200 }
}
```

* `selector` — query **inside** the item; missing → `""`
* `attr` — `text` (default, whitespace-collapsed `textContent`), `value`, or any
  attribute name. `src`/`href`/`data-src` are resolved to absolute URLs.
* `as: "image"` — the background page fetches the URL, downscales it to
  `max_size` px and returns JPEG base64. Plugins receive pixels, never URLs.
* `transform` — `basename`, `lowercase`, `trim`, `number`, `domain`
* `max_length` — truncation guard
* shorthand: `"author": "text"` is equivalent to `{ "attr": "text" }`
* no `fields` at all → each item gets `{ id, text }`

## `input` mapping

| Value | Meaning |
| ----- | ------- |
| `"@items"` | array of extracted item objects (`{id, …fields}`) |
| `"@count"` | number of items |
| `"@page.title"`, `"@page.url"`, `"@page.domain"`, `"@page.text"`, `"@page.lang"` | page facts (`text` is truncated to 4000 chars) |
| `"@selector:h1"` | text of the first page-level match |
| anything else | literal value passed through |

If `input` is omitted the plugin receives `{ "items": [...] }`.

Example for `images.tag`:

```json
"item_selector": "img.original",
"fields": {
  "data_base64": { "attr": "src", "as": "image", "max_size": 768 },
  "alt": { "attr": "alt" },
  "filename": { "attr": "src", "transform": "basename" }
},
"input": { "images": "@items" }
```

## `apply`

* `results_path` — dotted path to the result array in the plugin output (default `results`)
* `match_by` — result field matched against the item id (default `id`)
* `actions` — executed in order, per matched result

### Action reference (the complete allowlist)

| Action | Shape | Effect |
| ------ | ----- | ------ |
| `badge` | `{text, title?, target?}` | hub-owned `<span class="lah-badge">`, textContent only; re-runs replace it. For `<img>` it is inserted after the image. |
| `class` | `{map, field?}` | adds `map[value]` as a CSS class (validated `[A-Za-z0-9_-]+`) |
| `attribute` | `{name, value}` | sets `data-*`, `title` or `aria-label` only |
| `hide` / `show` | `{when?}` | toggles the `lah-hidden` class |
| `text` | `{selector?, value}` | replaces `textContent` — **requires `allow_text_replace: true`** |

Anything else (`script`, `html`, `eval`, navigation, `onclick`, `src`, …) is
ignored by design.

### Templates

`{label}`, `{confidence}`, `{tags}`, `{description}`, `{reason}` — any result
field, dotted paths allowed (`{safety.nsfw}`). Numbers are rounded to 2 decimals,
arrays are joined with `, `. Values are inserted as **text**, never markup.

### Conditions (`when`)

```json
{ "when": { "field": "label", "in": ["spam", "toxic"] } }
{ "when": { "field": "confidence", "gte": 0.8 } }
{ "when": { "field": "label", "equals": "ok" } }
{ "when": { "field": "nsfw", "truthy": true } }
```

## Userscript bridge (`bridge`)

A domain can expose selected plugins to a userscript (Tampermonkey /
Violentmonkey). This is **off by default** and separate from `features`: enabling
a feature for the panel does not expose it to the page.

```json
{
  "domain": "example-board.com",
  "bridge": {
    "tampermonkey": true,
    "plugins": ["comments.classify"],
    "allow_apply": false,
    "max_requests_per_minute": 30,
    "max_input_bytes": 262144
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `tampermonkey` | `false` | the opt-in; without it the bridge is silent on this site |
| `plugins` | *(empty)* | allowlist of plugin ids; if empty, exactly the site's **enabled features** are exposed |
| `allow_apply` | `false` | may the page ask to apply results (using *your* `apply` spec)? |
| `max_requests_per_minute` | `30` | sliding-window rate limit per page |
| `max_input_bytes` | `262144` | cap on the JSON-encoded page input |

Hub-level `bridge.*` keys in `config/hub.json` (`enabled`,
`require_domain_opt_in`, `allow_apply`, and the two limits) can only tighten this
policy. The bridge section can also be edited in **Settings → Domains**. The page
sends input data only — never options, selectors, actions or config. Protocol,
error codes and a client example: [TAMPERMONKEY.md](TAMPERMONKEY.md).

## Styling

`extension/content/content.css` defines `lah-badge` (with `data-tone` colours for
`ok`/`spam`/`toxic`/`off-topic`/`unknown`), `lah-flag-*` outlines and `lah-hidden`.
Add your own class names via the `class` action and style them with a user style
manager if you like — the hub only adds the class.

## Workflow for a new site

1. Open the site, inspect a comment/image element, find a stable selector.
2. Extension **Settings → Domains → Add** the domain.
3. **Insert feature template** → pick the plugin → adjust selectors.
4. **Validate** → **Save** → reload the tab.
5. Press **Alt+Shift+L**. The panel shows extracted counts, progress and results.
6. **Clear** removes every badge/class/attribute the hub added.

A working real-world example ships in `config/domains/news.ycombinator.com.json`.

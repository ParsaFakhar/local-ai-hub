# Examples

| File | What it shows |
| ---- | ------------- |
| `comments.json` | realistic classifier input (5 comments, mixed intent) |
| `images.json` | tagger input with a 1×1 base64 PNG (works offline) |
| `use_engine.py` | embedding the engine in your own Python program |
| `use_http_api.py` | talking to the local HTTP API from any application |

```bash
# CLI
./bin/laihub --mock task comments.classify --domain example-board.com --input @examples/comments.json
./bin/laihub --mock task images.tag --input @examples/images.json

# Your own program
python3 examples/use_engine.py
python3 examples/use_engine.py --real        # with Ollama running

# HTTP API (opt-in)
./install.sh --api
./bin/laihub-api &
python3 examples/use_http_api.py
```

All of these use the identical universal task protocol that Firefox uses —
the browser is just one more client.

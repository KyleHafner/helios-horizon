# Contributing

## Development setup

```bash
uv sync --frozen --extra test --python 3.11
uv run --frozen --extra test --python 3.11 playwright install chromium
```

## Before submitting a change

```bash
uv run --frozen --extra test --python 3.11 pytest -q
python3 -m compileall -q src ops tests
```

Keep security boundaries explicit:

- request data must not select units, executable paths, sockets, backup roots, or filesystem destinations;
- operational mutations require typed RPC, authentication, a valid session, CSRF validation, and an approved origin;
- secrets belong in protected runtime credential files and must not appear in tests, logs, fixtures, screenshots, or commits;
- preserve bounded queues, timeouts, file-size limits, and fail-closed validation;
- add focused tests for boundary changes.

Use synthetic names, documentation domains such as `example.com`, and reserved addresses from RFC 5737 in examples. Do not submit real server inventories, player names, private addresses, world files, backups, or production deployment evidence.

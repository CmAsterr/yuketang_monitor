# Project guidance

- Python 3.11+, asynchronous HTTP/WebSocket service. No Selenium, OCR, or automatic answer submission.
- All application instances receive explicit config and data paths. No global database, event bus or client singleton.
- Tests use tmp_path / TemporaryDirectory and mock external APIs. Never load root config.toml or real data/monitor.db, scan a real classroom or send real group messages from tests.
- Keep credentials out of logs, test artifacts, screenshots and commits. Only the guarded local POST /api/ai/key and POST /api/qq/secret endpoints intentionally reveal saved credentials to the local eye-toggle UI. General config/status reads remain redacted.
- Preserve user data and upgrade backups. Do not modify historical data in place merely to make tests pass.
- Run python -m pytest -q for behavioral changes. Run python tools/browser_qa.py for frontend changes. QQ and lifecycle changes also require their dedicated browser QA scripts.
- Real classroom/provider behavior is unverified unless an actual scoped test establishes it. Report that boundary accurately.
- Never commit config.toml, data/, logs, QA outputs, personal materials, installers or generated desktop shortcuts.

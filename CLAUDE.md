# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

视频号批量上传工具 — a Windows desktop tool for batch uploading videos to WeChat Channels (视频号). It provides a local web UI (Flask + SocketIO) and uses Playwright to automate the WeChat Channels web platform via persistent Chrome profiles.

## Commands

```bash
# Install dependencies (use a virtual environment)
pip install -r requirements.txt
playwright install chromium

# Run the app
python main.py

# CLI-only mode (no web UI) — login first, then upload
python batch_upload.py --setup          # open browser, scan QR to login
python batch_upload.py --csv batch-config.csv   # run batch upload
python batch_upload.py --csv batch-config.csv --resume  # resume from results.csv
```

No test suite, linter, or build step exists.

## Architecture

```
main.py              # Desktop entry: starts server in thread, opens pywebview or browser
server.py            # Flask + Flask-SocketIO web server, REST API, WebSocket broadcasts
batch_upload.py      # Core: Playwright browser automation for WeChat Channels upload flow
accounts.py          # Account CRUD backed by accounts.json (with .bak fallback)
public/              # Frontend SPA: index.html, app.js, style.css
```

### Key flows

**Login**: The server launches a visible Chrome window (Playwright headless=False) navigated to `channels.weixin.qq.com`. The user scans the QR code directly in this native window. `_login_async()` polls all pages' URLs every 2s — when `is_login(page.url)` returns False, login is complete. The window auto-closes, account status is updated to `ready`, and `account-updated` is broadcast via WebSocket to refresh the frontend.

**Batch Upload**: CSV input with columns `video_path`, `title`, `cover_path`, `description`, `short_drama_name`, `publish_time`. `preflight_records()` runs ffprobe validation. `process_video()` navigates to the WeChat Channels post/create page, uploads the video file via `<input type=file>`, fills metadata (title, cover, description, short drama selector, hide location), and clicks publish.

**State management**: `server.upload_state` dict (`running`/`abort`) controls the upload loop. `server.active_contexts` maps account names to open Playwright browser contexts. The frontend syncs via SocketIO — progress, logs, and upload results are broadcast to all connected clients.

### Key design decisions

- Browser profiles are persistent (stored per-account under `browser-profiles/<name>/`) so login sessions survive restarts.
- `accounts.json` is backed up to `.bak` before each write; on corruption, the backup is restored automatically.
- The server auto-shuts down 30s after the last WebSocket client disconnects (graceful cleanup for desktop use).
- `batch_upload.py` exposes a `__all__` list declaring its public API — functions imported by `server.py` must be in that list.
- Snake_case aliases exist for camelCase account functions (e.g., `load_accounts = loadAccounts`) to support both naming conventions.

## CSV format

The upload CSV uses these headers:
- `video_path` (required) — absolute path to MP4 file
- `title` (optional, min 6 chars if provided)
- `cover_path` (optional) — absolute path to cover image
- `description` (optional)
- `short_drama_name` (optional) — WeChat Channels drama series name
- `publish_time` (optional) — ISO datetime for scheduled publishing

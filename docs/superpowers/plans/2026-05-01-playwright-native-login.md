# Playwright Native Window Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the QR code capture/polling login flow with a Playwright native Chrome window where users scan directly.

**Architecture:** Delete ~180 lines of QR capture/transport/polling code from server.py. Add a single `POST /api/accounts/<name>/login` endpoint that launches Playwright headful Chrome, polls `page.url` in a background thread, auto-closes on login success, and broadcasts `account-updated` via WebSocket. Simplify frontend from 65-line modal+polling to 10-line fetch.

**Tech Stack:** Flask + Flask-SocketIO + Playwright (already in use)

---

### Task 1: Add new login endpoint to server.py

**Files:**
- Modify: `E:\wechat-channels-uploader-py\server.py` (add after line 242, before the old QR section)

- [ ] **Step 1: Add `POST /api/accounts/<name>/login` endpoint**

Insert the following block at server.py line 243 (after the `# ── Login:` comment, before the old QR endpoints):

```python
# ── Login: launch native browser window for QR scan ──


@app.route('/api/accounts/<name>/login', methods=['POST'])
def api_login(name):
    """Launch a visible Chrome window for WeChat QR login.
    Polls page URL every 2s; closes window and updates status on success."""
    try:
        acct = getAccount(name)
        if acct is None:
            return jsonify({'error': 'Account not found'}), 404

        # Close any existing context for this account
        existing = active_contexts.pop(name, None)
        if existing is not None:
            run_async_sync(existing.close())

        run_async_thread(_login_async(name, acct))
        return jsonify({'message': 'login started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


async def _login_async(name, acct):
    """Launch browser, wait for QR scan completion, then close."""
    await unlock_profile(acct['profileDir'])
    ctx = await init_browser(acct['profileDir'], headless=False)

    if len(ctx.pages) == 0:
        await ctx.close()
        logger.error(f'Login browser failed to start for {acct["label"]}')
        return

    active_contexts[name] = ctx
    page = ctx.pages[0]

    try:
        await page.goto('https://channels.weixin.qq.com/',
                        wait_until='networkidle', timeout=30000)

        logger.info(f'Login window opened for {acct["label"]}')

        # Poll URL every 2s until login completes or expires or 5min timeout
        deadline = time.time() + 300
        login_done = False

        while time.time() < deadline:
            await asyncio.sleep(2)

            # Check all pages for redirect away from login
            for p in ctx.pages:
                if not is_login(p.url):
                    login_done = True
                    break

            if login_done:
                break

            # Check for QR expiration on page
            try:
                body_text = await page.text_content('body') or ''
                expired_texts = ['过期', 'expired', '已失效', '重新获取']
                if any(t in body_text for t in expired_texts):
                    logger.info(f'QR code expired for {acct["label"]}')
                    broadcast({
                        'type': 'login-result',
                        'account': name,
                        'result': 'expired'
                    })
                    return
            except Exception:
                pass

            # Check for scanned state
            try:
                body_text = await page.text_content('body') or ''
                if '已扫码' in body_text or '扫描成功' in body_text or '确认登录' in body_text:
                    logger.info(f'QR scanned for {acct["label"]}')
            except Exception:
                pass

        if login_done:
            updateAccountStatus(name, 'ready')
            logger.info(f'Login complete for {acct["label"]}')
            broadcast({
                'type': 'account-updated',
                'account': name,
                'status': 'ready'
            })
        else:
            logger.warn(f'Login timeout for {acct["label"]}')
            broadcast({
                'type': 'login-result',
                'account': name,
                'result': 'timeout'
            })

    except Exception as e:
        logger.error(f'Login error for {acct["label"]}: {e}')
        broadcast({
            'type': 'login-result',
            'account': name,
            'result': 'error',
            'error': str(e)
        })
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        active_contexts.pop(name, None)
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import py_compile; py_compile.compile('E:/wechat-channels-uploader-py/server.py', doraise=True); print('OK')"
```

Expected: `OK`

---

### Task 2: Delete old QR/login endpoints from server.py

**Files:**
- Modify: `E:\wechat-channels-uploader-py\server.py` (delete lines 244-424)

- [ ] **Step 1: Delete old QR section**

Delete the entire block from `# ── Login: capture QR code and show in web UI ──` (line 244) through the end of `api_login_done` (line 440). This removes:
- `_get_qrcode_async()` — QR capture with 8 selectors + iframe search
- `_qrcode_status_async()` — poll status check with the page-before-assignment bug
- `POST /api/accounts/<name>/qrcode`
- `GET /api/accounts/<name>/qrcode/status`
- `POST /api/accounts/<name>/qrcode/cancel`
- `POST /api/accounts/<name>/login/done`

The section comment between this block and verify is `# ── Verify login status ──` — keep that and everything after.

- [ ] **Step 2: Clean up unused imports**

After the deletions, `base64` import (line 9 in server.py) is no longer used. Remove it:

```
import base64
```

- [ ] **Step 3: Verify syntax**

```bash
python -c "import py_compile; py_compile.compile('E:/wechat-channels-uploader-py/server.py', doraise=True); print('OK')"
```

Expected: `OK`

---

### Task 3: Replace frontend loginAccount() in app.js

**Files:**
- Modify: `E:\wechat-channels-uploader-py\public\app.js` (lines 866-929)

- [ ] **Step 1: Replace the loginAccount function**

Replace lines 866-929 (the entire `loginAccount()` function) with:

```javascript
async function loginAccount(name) {
  try {
    const res = await api('/api/accounts/' + name + '/login', { method: 'POST' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast(data.error || '启动登录失败', 'error');
      return;
    }
    toast('登录窗口已打开，请在浏览器窗口中扫码', 'info');
  } catch (e) {
    toast('启动登录失败: ' + e.message, 'error');
  }
}
```

- [ ] **Step 2: Add WebSocket handler for account-updated**

In the `connectWS()` function (app.js line 62-69), add a handler for the `account-updated` message type:

```javascript
ws.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'log') appendLog(d);
      if (d.type === 'progress') onProgress(d);
      if (d.type === 'upload-end') onUploadEnd(d);
      if (d.type === 'login-expired') onLoginExpired(d);
      if (d.type === 'account-updated') { loadAccounts(); toast('登录成功', 'success'); }
      if (d.type === 'login-result') {
        if (d.result === 'expired') toast('二维码已过期，请重试', 'warn');
        else if (d.result === 'timeout') toast('登录超时，请重试', 'warn');
        else if (d.result === 'error') toast('登录出错: ' + (d.error || ''), 'error');
      }
    } catch {}
  };
```

- [ ] **Step 3: Verify app.js syntax**

```bash
node -c "E:/wechat-channels-uploader-py/public/app.js" 2>&1 && echo "OK" || echo "node not available — manual check needed"
```

---

### Task 4: End-to-end verification

- [ ] **Step 1: Start the server with Python 3.11**

```bash
cd "E:/wechat-channels-uploader-py" && "C:/Users/宇杉楠/AppData/Local/Programs/Python/Python311/python.exe" run.py
```

- [ ] **Step 2: Test login flow**

1. Open `http://localhost:3123` in a browser (or wait for pywebview window)
2. Go to Accounts tab, click "扫码登录" on an account
3. Verify: A Chrome window opens showing the WeChat Channels login page
4. Scan the QR code with WeChat
5. Verify: Chrome window auto-closes after login completes
6. Verify: Account status changes to "ready" in the UI

- [ ] **Step 3: Test error cases**

1. Close the Chrome window manually during login → Frontend should not crash
2. Start login for same account twice → Second call should close first browser and start fresh
3. Login for account that doesn't exist → 404 error

---

### Summary of Net Changes

| File | Lines Deleted | Lines Added | Net |
|------|--------------|-------------|-----|
| `server.py` | ~180 | ~90 | -90 |
| `public/app.js` | ~65 | ~20 | -45 |
| **Total** | **~245** | **~110** | **-135** |

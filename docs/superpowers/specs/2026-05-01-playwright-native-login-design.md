# Design: Playwright Native Window Login

**Date:** 2026-05-01  
**Status:** Approved

## Context

当前登录流程通过 Playwright headful 浏览器捕获 QR 码截图，以 base64 格式传输到前端 Web UI 的模态框中展示，然后前端每 3 秒轮询 `/qrcode/status` 检测登录状态。该链路存在以下问题：

- QR 选择器脆弱（8 个 CSS 选择器，微信改版即失效）
- "已扫码"检测有变量顺序 bug（server.py:391 `page` 在使用前未赋值）
- 整条链路复杂：QR 捕获 → base64 编码 → JSON 传输 → 模态渲染 → setInterval 轮询 → 状态检测
- 代码量大：server.py ~180 行 + app.js ~60 行

## Solution

删除 QR 捕获/传输/轮询整条链路。保留 Playwright 启动 Chrome 窗口的机制（已有 headless=False 逻辑），用户在 Playwright 弹出的原生 Chrome 窗口中直接扫码。Playwright 后台轮询 URL 检测登录完成，自动关闭窗口并通知前端。

### User Flow

```
用户点击"扫码登录"
  → 前端 POST /api/accounts/<name>/login
  → 后端启动 Playwright Chrome（headless=False），导航到微信登录页
  → Chrome 窗口显示，用户扫码
  → 扫码完成 → 页面跳转到视频号后台
  → Playwright 后台线程每 2s 检查 page.url，is_login(url)==False 时确认登录成功
  → 关闭 Chrome 窗口，更新账号状态，WebSocket 广播 account-updated
  → 前端收到消息，刷新账号列表
```

### Files Changed

| File | Action | What |
|------|--------|------|
| `server.py` | Edit | 新增 `POST /api/accounts/<name>/login`，删除 `/qrcode`、`/qrcode/status`、`/qrcode/cancel`、`/login/done` 四个端点 + `_get_qrcode_async` + `_qrcode_status_async` |
| `public/app.js` | Edit | 重写 `loginAccount()` 函数，删除模态显示、轮询、stopPolling 逻辑 |
| `public/index.html` | (no change) | Modal 仍被其他功能（删除确认等）使用，保留 |

### New Endpoint: POST /api/accounts/<name>/login

```
1. 获取账号信息 (getAccount)
2. 关闭该账号已有的浏览器 context（如有）
3. 解锁 profile（remove SingletonLock）
4. 启动 Playwright headless=False 浏览器
5. 导航到 channels.weixin.qq.com
6. 后台线程轮询 page.url：
   - 每 2 秒检查一次
   - is_login(url)==False → 登录成功 → 关闭浏览器 → updateAccountStatus('ready') → broadcast('account-updated')
   - 检测到过期关键词（过期/expired/已失效）→ 关闭浏览器 → broadcast 相应消息
   - 超时 5 分钟 → 关闭浏览器 → 标记 expired
7. 返回 {"message": "login started"}
```

### WebSocket Message

新增消息类型：`account-updated`，payload 包含 account name。前端在 ws.onmessage 中处理该类型，调用 `loadAccounts()` 刷新。

### Code to Delete

**server.py:**
- Lines 247-424: 4 个 QR/login 端点
- Lines 265-409: `_get_qrcode_async()` + `_qrcode_status_async()`（约 145 行）
- 删除后 server.py 从 ~830 行减少到 ~630 行

**public/app.js:**
- `loginAccount()` 函数中约 60 行 QR 模态+轮询逻辑
- 替换为 ~10 行的简单 fetch 调用

### Error Handling

- Profile 锁无法解除 → 返回 500，前端 toast 提示
- 浏览器启动失败 → 返回 500
- 5 分钟超时 → 自动关闭窗口，WebSocket 通知前端 "登录超时"
- 用户手动关闭 Chrome 窗口 → Playwright context 异常，捕获后清理

### Verification

1. 点击"扫码登录" → Chrome 窗口弹出 → 扫描微信登录 QR → 登录成功窗口自动关闭 → 前端账号状态变为"已登录"
2. 弹出 Chrome 窗口后自行关闭 → 前端出现错误提示
3. 二维码过期 → 5 分钟内自动检测并关闭窗口
4. 已有登录的账号点击"验证" → 功能不受影响（verify 端点不变）

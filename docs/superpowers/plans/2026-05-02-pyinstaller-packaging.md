# PyInstaller 打包 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 PyInstaller --onedir 将项目打包为可分发的 Windows 文件夹，内含 exe + 依赖 + Chromium

**Architecture:** `run.py` 在 frozen 模式下通过 `sys._MEIPASS`/`sys.executable` 设置 `APP_BASE_DIR`（可写数据）和 `APP_RES_DIR`（只读资源）环境变量。其余模块从环境变量读取路径，`__file__` 作为非 frozen 回退。新增 `build.py` 负责 PyInstaller 构建 + Chromium 捆绑。

**Tech Stack:** PyInstaller 6.x, Python 3.11, pywebview, Playwright

---

## 文件结构总览

| 文件 | 操作 | 职责 |
|------|------|------|
| `run.py` | 修改 | frozen 模式下正确设置 APP_BASE_DIR / APP_RES_DIR / PLAYWRIGHT_BROWSERS_PATH |
| `accounts.py` | 修改 | `_BASE_DIR` 从 `APP_BASE_DIR` 环境变量读取 |
| `batch_upload.py` | 修改 | `_BASE_DIR` 从 `APP_BASE_DIR` 环境变量读取；设置 PLAYWRIGHT_BROWSERS_PATH |
| `server.py` | 修改 | `BASE_DIR` 从 `APP_BASE_DIR` 读取；static files 从 `APP_RES_DIR` 读取 |
| `main.py` | 修改 | `os.chdir()` + `os.makedirs()` 使用 `APP_BASE_DIR` |
| `build.py` | **新建** | PyInstaller 构建脚本：安装依赖、查找 Chromium、执行打包 |

---

### Task 1: 适配 `run.py` — frozen 模式路径 + Playwright 浏览器路径

**Files:**
- Modify: `run.py`

`run.py` 在所有 import 之前运行，是设置环境变量的唯一位置。frozen 模式下 `__file__` 指向 `_MEIPASS` 内的脚本，不可写。

- [ ] **Step 1: 在 run.py 开头添加 frozen 检测**

将现有 `base_dir = ...` 后的环境变量设置替换为：

```python
import sys
import os

base_dir = os.path.dirname(os.path.abspath(__file__))

# 添加项目根目录到 Python 路径
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

# frozen (PyInstaller) vs 开发模式: 区分可写数据目录和只读资源目录
if getattr(sys, 'frozen', False):
    # 可写数据: exe 所在目录（用户解压后的文件夹）
    os.environ['APP_BASE_DIR'] = os.path.dirname(sys.executable)
    # 只读资源: PyInstaller 临时解压目录 (_internal/)
    os.environ['APP_RES_DIR'] = sys._MEIPASS
    # Playwright 浏览器: 捆绑在 _internal/ms-playwright/ 下
    _pw_browsers = os.path.join(sys._MEIPASS, 'ms-playwright')
    if os.path.isdir(_pw_browsers):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _pw_browsers
else:
    os.environ.setdefault('APP_BASE_DIR', base_dir)
    os.environ.setdefault('APP_RES_DIR', base_dir)

# 扫描 site-packages 查找 webview DLL 目录（frozen 模式下 _MEIPASS 内也有）
if getattr(sys, 'frozen', False):
    _webview_lib = os.path.join(sys._MEIPASS, 'webview', 'lib')
    if os.path.isdir(_webview_lib):
        os.environ["PATH"] = _webview_lib + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, 'add_dll_directory'):
            try:
                os.add_dll_directory(_webview_lib)
            except Exception:
                pass
else:
    try:
        import site
        for scheme in site.getsitepackages():
            wv_path = os.path.join(scheme, "webview", "lib")
            if os.path.isdir(wv_path):
                os.environ["PATH"] = wv_path + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, 'add_dll_directory'):
                    try:
                        os.add_dll_directory(wv_path)
                    except Exception:
                        pass
                break
    except Exception:
        pass

# WebView2 稳定性标志
os.environ.setdefault(
    'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
    '--disable-gpu-rasterization --disable-software-rasterizer'
)

# 兼容旧环境变量
os.environ.setdefault('APP_DATA_DIR', os.path.join(os.environ['APP_BASE_DIR'], 'data'))

try:
    import main
    main.main()

except Exception:
    import traceback
    from datetime import datetime
    error_msg = traceback.format_exc()

    # 写入错误日志（到可写目录）
    try:
        log_file = os.path.join(os.environ.get('APP_BASE_DIR', base_dir), "app.log")
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n[{datetime.now()}] 启动失败:\n")
            f.write(error_msg)
            f.write("\n")
    except Exception:
        pass

    print("=" * 60)
    print("启动失败！错误信息：")
    print("=" * 60)
    print(error_msg)
    print("=" * 60)

    if sys.stdin and sys.stdin.isatty():
        input("按回车键退出...")
```

- [ ] **Step 2: 验证开发模式仍可运行**

```bash
python run.py
```
窗口应正常启动（和改之前一样）。

---

### Task 2: 适配 `accounts.py` — 路径从环境变量读取

**Files:**
- Modify: `accounts.py:7-8`

- [ ] **Step 1: 将 `_BASE_DIR` 改为从环境变量读取**

将：
```python
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
```
改为：
```python
_BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
```

- [ ] **Step 2: 验证**

```bash
python -c "from accounts import _BASE_DIR; print(_BASE_DIR)"
```
应输出项目目录路径。

---

### Task 3: 适配 `batch_upload.py` — 路径从环境变量读取 + Playwright 浏览器路径

**Files:**
- Modify: `batch_upload.py:34-38`

- [ ] **Step 1: 将 `_BASE_DIR` 改为从环境变量读取**

将：
```python
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
```
改为：
```python
_BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
```

- [ ] **Step 2: 在 `init_browser` 中设置 PLAYWRIGHT_BROWSERS_PATH**

在 `batch_upload.py` 中找到 `init_browser` 函数（大约第290行附近），在 `await async_playwright().start()` 之前添加环境变量设置。找到函数体开头并插入：

```python
async def init_browser(profile_dir, headless=True):
    # 如果 run.py 设置了 PLAYWRIGHT_BROWSERS_PATH，确保 Playwright 使用捆绑的浏览器
    _pw_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')
    if _pw_path and os.path.isdir(_pw_path):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _pw_path)
    # ... 其余代码不变
```

- [ ] **Step 3: 验证**

```bash
python -c "from batch_upload import _BASE_DIR; print(_BASE_DIR)"
```
应输出项目目录路径。

---

### Task 4: 适配 `server.py` — 静态文件路径 + 数据路径

**Files:**
- Modify: `server.py:43,48-51,734-744`

- [ ] **Step 1: 修改 `BASE_DIR` 和静态文件目录**

将：
```python
app = Flask(__name__, static_folder='public', static_url_path='')
...
PORT = int(os.environ.get('PORT', 3123))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
LAST_BATCH_PATH = os.path.join(BASE_DIR, 'last-batch.csv')
```

改为：
```python
BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.environ.get('APP_RES_DIR', BASE_DIR)

app = Flask(__name__, static_folder=os.path.join(RES_DIR, 'public'), static_url_path='')
...
PORT = int(os.environ.get('PORT', 3123))
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
LAST_BATCH_PATH = os.path.join(BASE_DIR, 'last-batch.csv')
```

- [ ] **Step 2: 修改静态文件路由**

将 `static_files` 函数中的：
```python
@app.route('/<path:path>')
def static_files(path):
    public_path = os.path.join(BASE_DIR, 'public', path)
    if os.path.exists(public_path):
        return send_from_directory('public', path)
    return send_from_directory('public', 'index.html')
```

改为：
```python
@app.route('/<path:path>')
def static_files(path):
    public_dir = os.path.join(RES_DIR, 'public')
    public_path = os.path.join(public_dir, path)
    if os.path.exists(public_path):
        return send_from_directory(public_dir, path)
    return send_from_directory(public_dir, 'index.html')
```

- [ ] **Step 3: 修改 `index()` 路由**

```python
@app.route('/')
def index():
    return send_from_directory(os.path.join(RES_DIR, 'public'), 'index.html')
```

- [ ] **Step 4: 验证开发模式正常**

```bash
python -c "from server import app; print(app.static_folder)"
```
应输出 `<项目目录>\public`。

---

### Task 5: 适配 `main.py` — 工作目录和运行时目录

**Files:**
- Modify: `main.py:10,13-14,52`

- [ ] **Step 1: 修改 `os.chdir` 和目录创建**

将：
```python
import os
import sys
import time
import threading
import webbrowser
import signal

# 确保在项目根目录运行
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 确保必要目录存在
for d in ['uploads', 'screenshots', 'browser-profiles']:
    os.makedirs(d, exist_ok=True)
```

改为：
```python
import os
import sys
import time
import threading
import webbrowser
import signal

# 确保在可写数据目录运行
_BASE = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
os.chdir(_BASE)

# 确保必要目录存在
for d in ['uploads', 'screenshots', 'browser-profiles']:
    os.makedirs(os.path.join(_BASE, d), exist_ok=True)
```

- [ ] **Step 2: 修改 `get_icon_path()` 搜索路径**

将：
```python
candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.ico'),
    os.path.join(os.environ.get('APP_RES_DIR', ''), 'app.ico'),
]
```

改为：
```python
candidates = [
    os.path.join(_BASE, 'app.ico'),
    os.path.join(os.environ.get('APP_RES_DIR', _BASE), 'app.ico'),
]
```

- [ ] **Step 3: 修改 `configure_webview()` 中的 DLL 扫描**

frozen 模式下，DLL 已在 `_MEIPASS` 内，由 `run.py` 处理。`configure_webview()` 保持不变即可（`setdefault` 不覆盖已有值）。

- [ ] **Step 4: 验证**

```bash
python main.py
```
窗口应正常弹出。

---

### Task 6: 创建 `build.py` — PyInstaller 构建脚本

**Files:**
- Create: `build.py`

- [ ] **Step 1: 创建 build.py**

```python
#!/usr/bin/env python3
"""PyInstaller 构建脚本 — 将项目打包为文件夹分发的 Windows 桌面应用"""
import glob
import os
import shutil
import subprocess
import sys


def find_playwright_browsers():
    """查找系统已安装的 Playwright Chromium 浏览器路径"""
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    ms_pw = os.path.join(local_app_data, 'ms-playwright')
    if not os.path.isdir(ms_pw):
        return None
    return ms_pw


def clean_dist():
    """清理上次构建产物"""
    dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist')
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    build_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build')
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)


def ensure_dependencies():
    """确保打包所需依赖已安装"""
    deps = ['pyinstaller', 'pywebview']
    for dep in deps:
        result = subprocess.run(
            [sys.executable, '-c', f'import {dep}'],
            capture_output=True
        )
        if result.returncode != 0:
            print(f'[ERROR] {dep} 未安装，请先运行: pip install {dep}')
            sys.exit(1)


def build():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base_dir)

    clean_dist()
    ensure_dependencies()

    # 查找 Playwright Chromium
    pw_browsers = find_playwright_browsers()
    if pw_browsers:
        print(f'[INFO] 找到 Playwright 浏览器: {pw_browsers}')
    else:
        print('[WARN] 未找到 Playwright 浏览器，打包后将不含 Chromium')
        print('[WARN] 请先运行: playwright install chromium')

    # 构建 PyInstaller 命令行
    # --add-data 格式: "source;dest" (Windows 用分号)
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onedir',
        '--name', '视频号批量上传',
        '--add-data', f'public{os.pathsep}public',
        '--add-data', f'accounts.json{os.pathsep}.',
        '--collect-submodules', 'flask_socketio',
        '--collect-submodules', 'engineio.async_drivers.threading',
        '--hidden-import', 'engineio.async_drivers.threading',
        '--hidden-import', 'playwright.async_api',
        '--hidden-import', 'webview',
        '--hidden-import', 'webview.platforms.windowsforms',
        '--copy-metadata', 'flask',
        '--copy-metadata', 'flask_socketio',
        '--copy-metadata', 'python-socketio',
        '--copy-metadata', 'python-engineio',
        '--copy-metadata', 'playwright',
        '--clean',
        '--noconfirm',
        'run.py',
    ]

    # 如果找到 Playwright Chromium，添加为数据
    if pw_browsers:
        cmd.insert(-1, '--add-data')
        cmd.insert(-1, f'{pw_browsers}{os.pathsep}ms-playwright')

    print(f'[INFO] 开始打包...')
    print(f'[INFO] 命令: {" ".join(cmd)}')
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print('[ERROR] 打包失败')
        sys.exit(1)

    # 验证产物
    exe = os.path.join(base_dir, 'dist', '视频号批量上传', '视频号批量上传.exe')
    if os.path.exists(exe):
        # 清理多余文件
        spec_file = os.path.join(base_dir, '视频号批量上传.spec')
        if os.path.exists(spec_file):
            os.remove(spec_file)
        print(f'[SUCCESS] 打包完成: {os.path.dirname(exe)}')
        # 计算大小
        total = 0
        for root, dirs, files in os.walk(os.path.dirname(exe)):
            for f in files:
                total += os.path.getsize(os.path.join(root, f))
        print(f'[INFO] 总大小: {total / (1024 * 1024 * 1024):.2f} GB')
    else:
        print('[ERROR] 打包产物未找到')
        sys.exit(1)


if __name__ == '__main__':
    build()
```

- [ ] **Step 2: 执行构建**

在 Python 3.11 环境下：
```bash
pip install pyinstaller pywebview
python build.py
```

---

### Task 7: 构建后验证

- [ ] **Step 1: 检查产物结构**

```bash
ls -la dist/视频号批量上传/
```
确认包含 `视频号批量上传.exe` 和 `_internal/` 目录。

- [ ] **Step 2: 双击启动 exe**

直接双击 `dist/视频号批量上传/视频号批量上传.exe`，确认桌面窗口正常弹出。

- [ ] **Step 3: 验证核心功能**

- 前端页面加载正常
- 登录功能可用（Playwright Chromium 启动）
- 创建账号、上传视频流程
- 关闭窗口确认对话框

- [ ] **Step 4: 跨机器验证**

将 `dist/视频号批量上传/` 文件夹拷贝到另一台 Windows 机器（无 Python 环境），双击 exe 确认可运行。

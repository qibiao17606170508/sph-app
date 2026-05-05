# PyInstaller 打包设计

## 目标

将视频号批量上传工具打包为可独立分发的 Windows 桌面应用（文件夹形式），用户解压后双击 exe 即可使用。

## 打包方案

- **工具**: PyInstaller + `--onedir`
- **Python**: 3.11（pywebview pythonnet 兼容）
- **产物**: 一个文件夹，包含 exe + `_internal/` 依赖 + 可写数据目录
- **参考**: 青风 的文件夹分发模式

## 最终分发结构

```
视频号批量上传/
├── 视频号批量上传.exe          ← 双击启动
├── _internal/                   ← PyInstaller 生成（只读）
│   ├── public/                  ← 前端 SPA
│   ├── accounts_init.json      ← 初始账号模板
│   └── ...                      ← Python 库、DLL、Chromium 等
├── accounts.json                ← 用户数据（可写）
├── uploads/                     ← 上传目录（可写）
├── screenshots/                 ← 截图目录（可写）
├── browser-profiles/            ← Chrome 持久化配置（可写）
└── app.log                      ← 应用日志（可写）
```

## 路径解析策略

PyInstaller 打包后 `__file__` 不可靠，需区分两类路径：

| 类型 | 方法 | 说明 |
|------|------|------|
| 只读资源 | `sys._MEIPASS`（frozen）或 `__file__` 目录 | 前端文件、初始 JSON |
| 可写数据 | `sys.executable` 所在目录（frozen）或 `__file__` 目录 | accounts.json, browser-profiles/ |

在 `accounts.py`、`server.py`、`main.py` 中统一用两个工具函数：

```python
def _base_dir():
    """返回可写数据目录（exe 所在目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _data_dir():
    """返回只读资源目录（打包后为临时解压目录）"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return _base_dir()
```

## 源码改动清单

### 1. `accounts.py`

- `_BASE_DIR` → 使用 `_base_dir()`
- `ACCOUNTS_PATH` → 使用 `_BASE_DIR`（可写）
- `BASE_PROFILE_DIR` → 使用 `_BASE_DIR`（可写）
- `createAccount()` 中 `profileDir` → 使用 `_BASE_DIR`（可写）

### 2. `main.py`

- `os.chdir()` → `_base_dir()`
- 启动时 `os.makedirs()` 的 uploads/screenshots/browser-profiles 目录 → `_base_dir()`
- `configure_webview()` → frozen 模式下同样扫描 site-packages；`_MEIPASS` 内也有 webview DLL
- `get_icon_path()` → 优先查 `_base_dir()`，再查 `APP_RES_DIR`

### 3. `server.py`

- `static_folder` / `send_from_directory` → `_data_dir()` + public/
- 导入 `accounts` 模块的函数，路径由 accounts.py 内部处理，server.py 无需自己计算

### 4. `batch_upload.py`

- Playwright 浏览器路径 → 设置 `PLAYWRIGHT_BROWSERS_PATH` 环境变量指向 `_data_dir()` 下的捆绑 Chromium

### 5. `run.py`

- 路径初始化逻辑对 frozen 模式透明（已在 main.py 中处理）

### 6. 新增 `build.py`（打包脚本）

- 配置 PyInstaller `.spec` 或命令行参数
- 指定 `--onedir`、`--add-data`、hidden imports
- 自动处理 public/ 目录拷贝
- 输出到 `dist/` 目录

## PyInstaller 配置要点

- `--onedir`：文件夹输出
- `--add-data "public;public"`：前端资源
- `--add-data "accounts.json;."`：初始账号模板
- `--collect-binaries webview`：WebView2 runtime DLL
- `--collect-submodules flask_socketio`：确保 SocketIO 不丢
- `--hidden-import engineio.async_drivers.threading`：SocketIO 异步驱动
- `--hidden-import playwright.async_api`：Playwright
- `--name 视频号批量上传`

## Playwright Chromium 处理

打包时直接将系统已安装的 Playwright Chromium 复制到构建目录：

1. 找到 Playwright 浏览器路径：`%LOCALAPPDATA%\ms-playwright\chromium-*`
2. `build.py` 将整个 `ms-playwright` 目录复制到 dist，然后 `--add-binary` 打包进 `_internal/`
3. 运行时设置 `PLAYWRIGHT_BROWSERS_PATH=os.path.join(_data_dir(), 'ms-playwright')`

## 验证清单

- [ ] `pip install pyinstaller pywebview`（Python 3.11）
- [ ] `python build.py` 生成 dist 目录
- [ ] 双击 exe 启动，桌面窗口正常弹出
- [ ] 登录功能正常（Playwright 启动 Chromium）
- [ ] 前端页面正常加载
- [ ] 创建账号、上传视频完整流程
- [ ] 关闭窗口确认对话框有效
- [ ] 拷贝到其他机器（无 Python）可正常运行

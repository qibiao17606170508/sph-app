# 视频号批量上传工具

将视频批量上传到微信视频号的 Windows 桌面应用。基于 Playwright 浏览器自动化，支持多账号管理。

## 功能

- 🖥️ 独立桌面窗口，无浏览器地址栏
- 🔐 原生 Chrome 窗口扫码登录，稳定可靠
- 📤 批量上传视频，支持标题、封面、描述、定时发布
- 👥 多账号管理，每个账号独立登录态
- 🌓 自动跟随系统亮色/暗色模式
- 🎨 玻璃拟态现代界面风格

## 快速开始

从 [Releases](../../releases) 下载最新版本，解压后双击 `视频号批量上传.exe` 即可使用。

**系统要求**：Windows 10 1809+ 或 Windows 11（WebView2 系统自带）。

## 开发

```bash
# 安装依赖（Python 3.11）
pip install -r requirements.txt
playwright install chromium

# 启动开发服务器
python run.py

# 打包为 exe
python build.py
```

## 技术栈

- **后端**：Flask + Flask-SocketIO
- **自动化**：Playwright（Chromium）
- **桌面窗口**：pywebview + WebView2
- **前端**：原生 HTML/CSS/JS（玻璃拟态风格）
- **打包**：PyInstaller

## 项目结构

```
├── run.py            # 启动入口
├── main.py           # 桌面窗口管理
├── server.py         # Flask + WebSocket 服务
├── batch_upload.py   # Playwright 上传核心
├── accounts.py       # 账号 CRUD
├── build.py          # PyInstaller 构建脚本
├── public/           # 前端资源
│   ├── index.html
│   ├── app.js
│   └── style.css
└── accounts.json     # 账号数据（初始为空）
```

## License

MIT

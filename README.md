# 视频号批量上传工具

将视频批量上传到微信视频号的桌面应用。基于 Playwright 浏览器自动化，支持多账号管理。

## 功能

- 🖥️ 独立桌面窗口，无浏览器地址栏
- 🔐 原生 Chrome 窗口扫码登录，稳定可靠
- 📤 批量上传视频，支持标题、封面、描述、定时发布
- 👥 多账号管理，每个账号独立登录态
- 🌓 自动跟随系统亮色/暗色模式
- 🎨 玻璃拟态现代界面风格

## 快速开始

从 [Releases](../../releases) 下载最新版本：

- Windows：解压后双击 `视频号批量上传.exe`
- macOS：打开 `视频号批量上传.app`

应用始终以原生桌面窗口运行，不提供浏览器回退模式。

**系统要求**

- Windows 10 1809+ 或 Windows 11；Windows 发布包内已携带 WebView2 安装器，缺失时会优先离线自动安装
- macOS；打包和开发环境需安装 `pyobjc` 相关依赖

## 开发

```bash
# 安装依赖（Python 3.11）
pip install -r requirements.txt
playwright install chromium

# 启动开发服务器
python run.py

# 打包为桌面 app
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

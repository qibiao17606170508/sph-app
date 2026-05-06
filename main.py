"""视频号批量上传工具 - 桌面版入口"""
import os
import shutil
import sys
import time
import threading
import urllib.error
import urllib.request
import webbrowser
import subprocess

# 确保在可写数据目录运行
_BASE = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
os.chdir(_BASE)

# 确保必要目录存在
for d in ['uploads', 'screenshots', 'browser-profiles']:
    os.makedirs(os.path.join(_BASE, d), exist_ok=True)


def configure_webview():
    """配置 WebView2 环境以确保稳定性和 DLL 解析。
    使用 setdefault，允许 run.py 或外层脚本预先设置。
    """
    if sys.platform == 'win32':
        # Windows 下强制使用 Edge Chromium，避免退回到过时的 MSHTML/IE 内核。
        os.environ.setdefault('PYWEBVIEW_GUI', 'edgechromium')
    os.environ.setdefault(
        'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
        '--disable-gpu-rasterization --disable-software-rasterizer'
    )
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


def get_icon_path():
    """返回应用图标路径，不存在则返回 None。"""
    candidates = [
        os.path.join(_BASE, 'app.ico'),
        os.path.join(os.environ.get('APP_RES_DIR', _BASE), 'app.ico'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def has_webview2_runtime():
    """检测 Windows 是否已安装 Edge WebView2 Runtime。"""
    if sys.platform != 'win32':
        return True
    try:
        import winreg
    except Exception:
        return False

    client_guid = r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    uninstall_keys = (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft EdgeWebView',
        r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft EdgeWebView',
    )

    def _has_version(root, subkey):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, 'pv')
                return bool(str(value or '').strip())
        except Exception:
            return False

    def _has_display_version(root, subkey):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, 'DisplayVersion')
                return bool(str(value or '').strip())
        except Exception:
            return False

    roots = []
    for name in ('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE'):
        root = getattr(winreg, name, None)
        if root is not None:
            roots.append(root)

    for root in roots:
        if _has_version(root, client_guid):
            return True
        for subkey in uninstall_keys:
            if _has_display_version(root, subkey):
                return True
    return False


def notify_missing_webview2():
    message = (
        '检测到当前 Windows 正在使用旧版 MSHTML 内核，界面和登录功能会异常。\n'
        '请先安装 Microsoft Edge WebView2 Runtime 后再打开软件。\n'
        '安装完成后重新启动即可。'
    )
    print(f'[错误] {message}')
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, '缺少 WebView2 Runtime', 0x10)
    except Exception:
        pass
    try:
        webbrowser.open('https://developer.microsoft.com/microsoft-edge/webview2/')
    except Exception:
        pass


def find_chrome_executable():
    custom = os.environ.get('CHROME_PATH', '').strip()
    candidates = [custom] if custom else []

    if sys.platform == 'win32':
        for env_name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            root = os.environ.get(env_name, '').strip()
            if root:
                candidates.append(os.path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    elif sys.platform == 'darwin':
        candidates += [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
    else:
        which_path = shutil.which('google-chrome') or shutil.which('chrome') or shutil.which('chromium')
        if which_path:
            candidates.append(which_path)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ''


def notify_missing_chrome():
    message = (
        '未检测到谷歌浏览器。\n'
        '请先安装 Google Chrome 后再打开软件。'
    )
    print(f'[错误] {message}')
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, '缺少谷歌浏览器', 0x10)
    except Exception:
        pass
    try:
        webbrowser.open('https://www.google.com/chrome/')
    except Exception:
        pass


def open_chrome_app():
    """Windows 下优先直接使用谷歌浏览器应用窗口模式打开。"""
    chrome_path = find_chrome_executable()
    if not chrome_path:
        notify_missing_chrome()
        return False

    profile_dir = os.path.join(_BASE, 'chrome-shell-profile')
    os.makedirs(profile_dir, exist_ok=True)
    app_url = f'http://127.0.0.1:{PORT}'
    cmd = [
        chrome_path,
        f'--app={app_url}',
        '--new-window',
        '--no-first-run',
        '--disable-session-crashed-bubble',
        '--disable-features=TranslateUI',
        f'--user-data-dir={profile_dir}',
        '--window-size=1200,800',
    ]
    print(f'[启动] 使用谷歌浏览器打开: {chrome_path}')
    try:
        proc = subprocess.Popen(cmd)
    except Exception as e:
        print(f'[错误] 启动谷歌浏览器失败: {e}')
        notify_missing_chrome()
        return False

    try:
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
    return True


from server import app, socketio, PORT

_server_ready = threading.Event()


def _is_system_dark():
    """检测 Windows 系统是否使用暗色模式"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
        )
        value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        winreg.CloseKey(key)
        return value == 0
    except Exception:
        return False


def _apply_system_titlebar(title):
    """如果系统为暗色模式，启动后台线程设置窗口暗色标题栏和沉浸式暗色模式"""
    if not _is_system_dark():
        return

    def _set():
        import ctypes
        from ctypes import wintypes

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        # 等窗口创建
        time.sleep(1)

        found = []

        @WNDENUMPROC
        def _enum(hwnd, _lparam):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                if title in buf.value:
                    found.append(hwnd)
            return True

        for _ in range(10):
            found.clear()
            ctypes.windll.user32.EnumWindows(_enum, 0)
            for hwnd in found:
                val = ctypes.c_int(1)
                try:
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                        ctypes.byref(val), ctypes.sizeof(val)
                    )
                except Exception:
                    pass
            if found:
                break
            time.sleep(0.5)

    threading.Thread(target=_set, daemon=True).start()

def start_server():
    """在后台线程启动 Flask + SocketIO 服务"""
    def _run():
        try:
            print(f'[启动] 服务: http://localhost:{PORT}')
            _server_ready.set()
            socketio.run(app, host='127.0.0.1', port=PORT, allow_unsafe_werkzeug=True, log_output=False)
        except Exception as e:
            print(f'[错误] 服务启动失败: {e}')
            sys.exit(1)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_for_server_http(timeout=15):
    """等待本地 HTTP 服务真正开始响应，避免 WebView 提前打开出现空白页。"""
    deadline = time.time() + timeout
    last_error = ''
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/version', timeout=2) as resp:
                if 200 <= resp.getcode() < 500:
                    return True
        except urllib.error.HTTPError as e:
            if 200 <= e.code < 500:
                return True
            last_error = str(e)
        except Exception as e:
            last_error = str(e)
        time.sleep(0.25)
    print(f'[错误] 服务未就绪: {last_error}')
    return False


def check_webview2():
    """检查 Windows 系统是否安装了 WebView2 运行库"""
    if sys.platform != 'win32':
        return True
    import winreg
    try:
        reg_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
        if winreg.QueryValueEx(reg_key, "pv")[0]:
            return True
    except Exception:
        pass
    try:
        reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
        if winreg.QueryValueEx(reg_key, "pv")[0]:
            return True
    except Exception:
        pass
    return False

def open_desktop():
    """尝试用 pywebview 打开桌面窗口，失败则回退到浏览器"""
    if sys.platform == 'win32' and not check_webview2():
        print('[提示] 未检测到 WebView2 运行库，降级使用 Chrome App 模式')
        return open_chrome_app()

    configure_webview()
    try:
        import webview
    except ImportError:
        if sys.platform == 'win32':
            return open_chrome_app()
        return False

    try:
        if not _server_ready.wait(timeout=10):
            print('[错误] 服务启动超时')
            return False
        if not wait_for_server_http(timeout=15):
            print('[错误] 服务页面加载超时')
            return False
    except Exception as e:
        print(f'[错误] 等待服务时出错: {e}')
        return False

    window_params = {
        'title': '视频号批量上传 · 大金小怪',
        'url': f'http://127.0.0.1:{PORT}',
        'width': 1200,
        'height': 800,
        'min_size': (900, 650),
        'resizable': True,
        'confirm_close': False,
        'focus': True,
        'text_select': True,
    }

    icon_path = get_icon_path()
    if icon_path:
        window_params['icon'] = icon_path

    try:
        # 在 Windows 上强制指定使用 edgechromium (WebView2) 引擎
        # 如果系统没有安装 WebView2，webview.create_window / webview.start 就会抛出 WebViewException
        gui = 'edgechromium' if sys.platform == 'win32' else None
        
        _window = webview.create_window(**window_params)
        # 系统暗色模式下自动适配标题栏
        try:
            _apply_system_titlebar(window_params['title'])
        except Exception:
            pass

        menu = []
        if sys.platform == 'darwin':
            try:
                menu = [
                    webview.menu.Menu('编辑', [
                        webview.menu.MenuAction('复制', lambda: None),
                        webview.menu.MenuAction('粘贴', lambda: None),
                        webview.menu.MenuAction('剪切', lambda: None),
                        webview.menu.MenuAction('全选', lambda: None),
                    ])
                ]
            except Exception:
                menu = []

        # macOS 下关闭 private_mode，恢复更接近原生浏览器的焦点/键盘行为
        webview.start(
            gui=gui,
            debug=False,
            private_mode=False,
            storage_path=os.path.join(_BASE, 'webview-storage'),
            menu=menu,
        )
        return True
    except Exception as e:
        print(f'[错误] 桌面窗口启动失败: {e}')
        print('[提示] 将回退到 Chrome App 模式')
        try:
            from datetime import datetime
            import traceback
            log_path = os.path.join(_BASE, 'app.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now()}] pywebview error:\n')
                traceback.print_exc(file=f)
                f.write('\n')
        except Exception:
            pass
        if sys.platform == 'win32':
            return open_chrome_app()
        return False


def main():
    print('=' * 50)
    print('  视频号批量上传工具')
    print('=' * 50)
    print()

    server_thread = start_server()
    _server_ready.wait(timeout=10)
    wait_for_server_http(timeout=15)

    # 尝试 pywebview 桌面窗口
    desktop_started = open_desktop()
    if not desktop_started:
        print('[提示] pywebview 未安装，使用浏览器打开')
        print(f'[启动] 打开 http://localhost:{PORT}')
        webbrowser.open(f'http://localhost:{PORT}')
    else:
        # pywebview.start 在窗口关闭后才返回；此时直接结束主进程，
        # 不再继续等待后台 SocketIO 线程，避免点 X 后进程卡死。
        print('[关闭] 桌面窗口已关闭')
        return

    # 保持主进程存活
    try:
        while server_thread.is_alive():
            server_thread.join(1)
    except KeyboardInterrupt:
        print('\n[关闭] 正在退出...')

    print('[关闭] 再见')


if __name__ == '__main__':
    main()

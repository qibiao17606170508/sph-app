"""视频号批量上传工具 - 桌面版入口"""
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


def configure_webview():
    """配置 WebView2 环境以确保稳定性和 DLL 解析。
    使用 setdefault，允许 run.py 或外层脚本预先设置。
    """
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


def open_desktop():
    """尝试用 pywebview 打开桌面窗口，失败则回退到浏览器"""
    configure_webview()

    try:
        import webview
    except ImportError:
        return False

    try:
        if not _server_ready.wait(timeout=10):
            print('[错误] 服务启动超时')
            return False
    except Exception as e:
        print(f'[错误] 等待服务时出错: {e}')
        return False

    window_params = {
        'title': '视频号批量上传',
        'url': f'http://127.0.0.1:{PORT}',
        'width': 1200,
        'height': 800,
        'min_size': (900, 650),
        'resizable': True,
        'confirm_close': False,
    }

    icon_path = get_icon_path()
    if icon_path:
        window_params['icon'] = icon_path

    try:
        webview.create_window(**window_params)
        # 系统暗色模式下自动适配标题栏
        try:
            _apply_system_titlebar(window_params['title'])
        except Exception:
            pass
        webview.start(debug=False)
        return True
    except Exception as e:
        print(f'[错误] 桌面窗口启动失败: {e}')
        print('[提示] 将使用浏览器打开')
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
        return False


def main():
    print('=' * 50)
    print('  视频号批量上传工具')
    print('=' * 50)
    print()

    server_thread = start_server()
    _server_ready.wait(timeout=10)

    # 尝试 pywebview 桌面窗口
    if not open_desktop():
        print('[提示] pywebview 未安装，使用浏览器打开')
        print(f'[启动] 打开 http://localhost:{PORT}')
        webbrowser.open(f'http://localhost:{PORT}')

    # 保持主进程存活
    try:
        while server_thread.is_alive():
            server_thread.join(1)
    except KeyboardInterrupt:
        print('\n[关闭] 正在退出...')

    print('[关闭] 再见')


if __name__ == '__main__':
    main()

"""视频号批量上传工具 - 桌面版入口"""
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

# 确保在可写数据目录运行
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))

# 打包版更新后由旧进程拉起新进程时，会继承旧的 APP_RES_DIR。
# 该目录可能仍指向旧版本的临时解包目录，导致新版本继续读取旧资源/旧 version.json。
# 因此 frozen 模式下始终以当前进程自己的资源目录为准，不沿用继承值。
if getattr(sys, 'frozen', False):
    if hasattr(sys, '_MEIPASS'):
        os.environ['APP_RES_DIR'] = sys._MEIPASS
    else:
        os.environ['APP_RES_DIR'] = _CODE_DIR
elif not os.environ.get('APP_RES_DIR'):
    os.environ['APP_RES_DIR'] = _CODE_DIR

if not os.environ.get('APP_BASE_DIR'):
    if getattr(sys, 'frozen', False):
        if sys.platform == 'darwin':
            base_dir = os.path.join(os.path.expanduser('~/Library/Application Support'), '视频号批量上传')
        else:
            base_dir = os.path.dirname(sys.executable)
    else:
        if sys.platform == 'darwin':
            base_dir = os.path.join(os.path.expanduser('~/Library/Application Support'), '视频号批量上传')
        else:
            base_dir = _CODE_DIR
    try:
        os.makedirs(base_dir, exist_ok=True)
        if base_dir != _CODE_DIR:
            for name in ('results.csv', 'upload.log'):
                src = os.path.join(_CODE_DIR, name)
                dst = os.path.join(base_dir, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
            src_storage = os.path.join(_CODE_DIR, 'webview-storage')
            dst_storage = os.path.join(base_dir, 'webview-storage')
            if os.path.isdir(src_storage) and not os.path.exists(dst_storage):
                shutil.copytree(src_storage, dst_storage)
    except Exception:
        pass
    os.environ['APP_BASE_DIR'] = base_dir

_BASE = os.environ.get('APP_BASE_DIR', _CODE_DIR)
os.makedirs(_BASE, exist_ok=True)
os.chdir(_BASE)

# 确保必要目录存在
for d in ['uploads', 'screenshots', 'browser-profiles']:
    os.makedirs(os.path.join(_BASE, d), exist_ok=True)

_RES_DIR = os.environ.get('APP_RES_DIR', _BASE)
_WEBVIEW2_BOOTSTRAPPER_URL = os.environ.get(
    'WEBVIEW2_BOOTSTRAPPER_URL',
    'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
)


class InstallProgressDialog:
    """Windows 下的轻量安装进度窗口。"""

    def __init__(self, title):
        self.title = title
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self._thread = None
        self._available = sys.platform == 'win32'

    def start(self, message, progress=None, indeterminate=False):
        if not self._available:
            return
        try:
            __import__('tkinter')
            __import__('tkinter.ttk')
        except Exception as e:
            append_app_log(f'安装进度条不可用，将退回日志提示: {e}')
            self._available = False
            return
        self._thread = threading.Thread(
            target=self._run,
            args=(message, progress, indeterminate),
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def update(self, message=None, progress=None, indeterminate=None):
        if self._available:
            self._queue.put({
                'action': 'update',
                'message': message,
                'progress': progress,
                'indeterminate': indeterminate,
            })

    def close(self):
        if self._available:
            self._queue.put({'action': 'close'})
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2)

    def _run(self, message, progress, indeterminate):
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(self.title)
        root.geometry('420x140')
        root.resizable(False, False)
        root.attributes('-topmost', True)
        try:
            root.iconbitmap(default=get_icon_path() or '')
        except Exception:
            pass

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)

        title_label = ttk.Label(frame, text=self.title)
        title_label.pack(anchor='w')

        message_var = tk.StringVar(value=message or '')
        message_label = ttk.Label(frame, textvariable=message_var, wraplength=380)
        message_label.pack(anchor='w', pady=(8, 10))

        progress_var = tk.DoubleVar(value=float(progress or 0))
        percent_var = tk.StringVar(value='')
        progress_bar = ttk.Progressbar(
            frame,
            orient='horizontal',
            length=380,
            mode='determinate',
            maximum=100,
            variable=progress_var,
        )
        progress_bar.pack(fill='x')

        percent_label = ttk.Label(frame, textvariable=percent_var)
        percent_label.pack(anchor='e', pady=(8, 0))

        def apply_state(msg=None, pct=None, is_indeterminate=None):
            if msg is not None:
                message_var.set(msg)
            if is_indeterminate is None:
                is_indeterminate = progress_bar.cget('mode') == 'indeterminate'
            if is_indeterminate:
                if progress_bar.cget('mode') != 'indeterminate':
                    progress_bar.configure(mode='indeterminate')
                    progress_bar.start(10)
                percent_var.set('正在安装...')
            else:
                if progress_bar.cget('mode') != 'determinate':
                    progress_bar.stop()
                    progress_bar.configure(mode='determinate')
                if pct is not None:
                    pct = max(0, min(100, float(pct)))
                    progress_var.set(pct)
                percent_var.set(f'{int(progress_var.get())}%')

        def center_window():
            root.update_idletasks()
            width = root.winfo_width()
            height = root.winfo_height()
            x = (root.winfo_screenwidth() // 2) - (width // 2)
            y = (root.winfo_screenheight() // 2) - (height // 2)
            root.geometry(f'{width}x{height}+{x}+{y}')

        def poll():
            try:
                while True:
                    item = self._queue.get_nowait()
                    if item.get('action') == 'close':
                        try:
                            progress_bar.stop()
                        except Exception:
                            pass
                        root.destroy()
                        return
                    if item.get('action') == 'update':
                        apply_state(
                            msg=item.get('message'),
                            pct=item.get('progress'),
                            is_indeterminate=item.get('indeterminate'),
                        )
            except queue.Empty:
                pass
            root.after(100, poll)

        apply_state(message, progress, indeterminate)
        center_window()
        self._ready.set()
        root.protocol('WM_DELETE_WINDOW', lambda: None)
        root.after(100, poll)
        root.mainloop()


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
    """仅在 Windows 返回应用图标路径。"""
    if sys.platform != 'win32':
        return None
    candidates = [
        os.path.join(_BASE, 'app.ico'),
        os.path.join(os.environ.get('APP_RES_DIR', _BASE), 'app.ico'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def append_app_log(message):
    try:
        log_path = os.path.join(_BASE, 'app.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now()}] {message}\n')
    except Exception:
        pass


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
        '当前 Windows 缺少 Microsoft Edge WebView2 Runtime，且自动安装未成功。\n'
        '请手动安装 WebView2 Runtime，安装完成后重新打开软件。'
    )
    show_error_dialog('缺少 WebView2 Runtime', message)
    try:
        webbrowser.open('https://developer.microsoft.com/microsoft-edge/webview2/')
    except Exception:
        pass


def find_local_webview2_bootstrapper():
    candidates = [
        os.path.join(_BASE, 'MicrosoftEdgeWebview2Setup.exe'),
        os.path.join(_BASE, 'MicrosoftEdgeWebView2Setup.exe'),
        os.path.join(_RES_DIR, 'MicrosoftEdgeWebview2Setup.exe'),
        os.path.join(_RES_DIR, 'MicrosoftEdgeWebView2Setup.exe'),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ''


def download_webview2_bootstrapper(progress_dialog=None):
    target_path = os.path.join(tempfile.gettempdir(), 'MicrosoftEdgeWebview2Setup.exe')
    append_app_log(f'开始下载 WebView2 安装器: {_WEBVIEW2_BOOTSTRAPPER_URL}')
    req = urllib.request.Request(
        _WEBVIEW2_BOOTSTRAPPER_URL,
        headers={'User-Agent': 'Mozilla/5.0 sph-app WebView2 Bootstrapper'}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = 0
        try:
            total = int(resp.headers.get('Content-Length') or '0')
        except Exception:
            total = 0
        downloaded = 0
        with open(target_path, 'wb') as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_dialog:
                    if total > 0:
                        pct = min(100, downloaded * 100.0 / total)
                        progress_dialog.update(
                            message='正在下载 WebView2 运行库安装器...',
                            progress=pct,
                            indeterminate=False,
                        )
                    else:
                        progress_dialog.update(
                            message='正在下载 WebView2 运行库安装器...',
                            indeterminate=True,
                        )
    append_app_log(f'WebView2 安装器已下载到: {target_path}')
    return target_path


def ensure_webview2_runtime():
    if sys.platform != 'win32':
        return True
    if has_webview2_runtime():
        return True

    progress_dialog = InstallProgressDialog('正在安装 WebView2')
    try:
        installer_path = find_local_webview2_bootstrapper()
        if installer_path:
            append_app_log(f'检测到本地 WebView2 安装器: {installer_path}')
            progress_dialog.start(
                '检测到系统缺少 WebView2 Runtime，正在准备安装...',
                progress=0,
                indeterminate=False,
            )
            progress_dialog.update(
                message='已找到安装器，正在启动安装...',
                progress=100,
                indeterminate=False,
            )
        else:
            progress_dialog.start(
                '检测到系统缺少 WebView2 Runtime，正在下载安装器...',
                progress=0,
                indeterminate=False,
            )
            installer_path = download_webview2_bootstrapper(progress_dialog=progress_dialog)

        progress_dialog.update(
            message='正在安装 WebView2 Runtime，请稍候...',
            indeterminate=True,
        )
        append_app_log(f'开始静默安装 WebView2 Runtime: {installer_path}')
        result = subprocess.run(
            [installer_path, '/silent', '/install'],
            check=False,
            timeout=600,
        )
        append_app_log(f'WebView2 安装器退出码: {result.returncode}')
    except Exception as e:
        append_app_log(f'执行 WebView2 安装器失败: {e}')
        progress_dialog.close()
        return False

    for _ in range(20):
        if has_webview2_runtime():
            append_app_log('WebView2 Runtime 安装成功')
            progress_dialog.update(
                message='WebView2 Runtime 安装成功，正在继续启动应用...',
                progress=100,
                indeterminate=False,
            )
            time.sleep(0.5)
            progress_dialog.close()
            return True
        time.sleep(1)

    append_app_log('WebView2 Runtime 安装后仍未检测到可用运行库')
    progress_dialog.close()
    return False


def show_error_dialog(title, message):
    print(f'[错误] {title}: {message}')
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            return
        if sys.platform == 'darwin':
            import subprocess
            script = f'display alert "{title}" message "{message.replace(chr(34), chr(39))}" as critical'
            subprocess.run(['osascript', '-e', script], check=False)
            return
    except Exception:
        pass


def show_info_dialog(title, message):
    print(f'[提示] {title}: {message}')
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
            return
        if sys.platform == 'darwin':
            script = f'display dialog "{message.replace(chr(34), chr(39))}" with title "{title}" buttons {{"确定"}} default button "确定"'
            subprocess.run(['osascript', '-e', script], check=False)
            return
    except Exception:
        pass


def notify_missing_desktop_runtime(missing_modules):
    modules_text = ', '.join(missing_modules)
    if sys.platform == 'win32':
        message = (
            '桌面应用运行依赖缺失，无法打开原生窗口。\n'
            f'缺少模块: {modules_text}\n'
            '请重新执行依赖安装或使用完整打包版本。'
        )
    elif sys.platform == 'darwin':
        message = (
            'macOS 桌面运行依赖缺失，无法打开原生窗口。\n'
            f'缺少模块: {modules_text}\n'
            '请重新执行依赖安装或重新打包应用。'
        )
    else:
        message = (
            '桌面运行依赖缺失，无法打开原生窗口。\n'
            f'缺少模块: {modules_text}'
        )
    show_error_dialog('桌面运行依赖缺失', message)
    append_app_log(message)


def notify_desktop_launch_failure(error):
    message = (
        '桌面应用启动失败，本次不会回退到浏览器模式。\n'
        f'详细错误: {error}\n'
        f'请查看日志: {os.path.join(_BASE, "app.log")}'
    )
    show_error_dialog('桌面窗口启动失败', message)


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


def open_desktop():
    """仅使用 pywebview 打开桌面窗口，不回退浏览器。"""
    if sys.platform == 'win32' and not ensure_webview2_runtime():
        notify_missing_webview2()
        return False

    configure_webview()
    try:
        import webview
    except ImportError as e:
        append_app_log(f'pywebview import failed: {e}')
        missing_modules = ['pywebview']
        if sys.platform == 'win32':
            missing_modules.append('pythonnet')
        elif sys.platform == 'darwin':
            missing_modules.extend(['pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-WebKit'])
        notify_missing_desktop_runtime(missing_modules)
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
        'title': '视频号批量上传',
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
    if sys.platform == 'win32' and icon_path:
        window_params['icon'] = icon_path

    try:
        gui = 'edgechromium' if sys.platform == 'win32' else None
        try:
            _window = webview.create_window(**window_params)
        except TypeError as e:
            if 'icon' not in str(e) or 'unexpected keyword argument' not in str(e):
                raise
            append_app_log(f'当前 pywebview 不支持 icon 参数，已自动降级重试: {e}')
            window_params.pop('icon', None)
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
        append_app_log(f'桌面窗口启动失败: {e}')
        try:
            import traceback
            log_path = os.path.join(_BASE, 'app.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now()}] pywebview error:\n')
                traceback.print_exc(file=f)
                f.write('\n')
        except Exception:
            pass
        if sys.platform == 'win32' and not has_webview2_runtime():
            notify_missing_webview2()
        else:
            notify_desktop_launch_failure(e)
        return False


def main():
    print('=' * 50)
    print('  视频号批量上传工具')
    print('=' * 50)
    print()

    start_server()
    _server_ready.wait(timeout=10)
    wait_for_server_http(timeout=15)

    # 尝试 pywebview 桌面窗口
    desktop_started = open_desktop()
    if not desktop_started:
        message = '桌面窗口未能启动，程序已终止；本次不会回退到浏览器模式。'
        print(f'[错误] {message}')
        append_app_log(message)
        return
    # pywebview.start 在窗口关闭后才返回；此时直接结束主进程，
    # 不再继续等待后台 SocketIO 线程，避免点 X 后进程卡死。
    print('[关闭] 桌面窗口已关闭')
    return


if __name__ == '__main__':
    main()

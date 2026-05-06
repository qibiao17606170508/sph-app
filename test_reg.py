import sys
if sys.platform == 'win32':
    import winreg
    def is_webview2_installed():
        try:
            reg_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
            return bool(winreg.QueryValueEx(reg_key, "pv")[0])
        except Exception:
            try:
                reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
                return bool(winreg.QueryValueEx(reg_key, "pv")[0])
            except Exception:
                return False
    print(is_webview2_installed())

import webview
try:
    window = webview.create_window('Test', 'https://www.google.com')
    webview.start(gui='edgechromium')
    print("Success")
except Exception as e:
    print(f"Failed with exception: {type(e).__name__}: {e}")

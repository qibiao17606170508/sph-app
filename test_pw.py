import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        print("Playwright started")
        # Try to launch with a fake executable_path just to see if it bypasses the validation
        try:
            await p.chromium.launch_persistent_context(
                user_data_dir="./test_profile",
                executable_path="C:\\nonexistent\\chrome.exe",
            )
        except Exception as e:
            print("Error:", e)

asyncio.run(main())

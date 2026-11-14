# -*- coding: utf-8 -*-
"""Chromium THẬT mở cổng gỡ lỗi (như profile Genlogin: Selenium gắn qua debuggerAddress) + Playwright gắn vào qua CDP.
TAM thấy một 'driver' có capabilities.goog:chromeOptions.debuggerAddress → mở được WebSocket CDP riêng tới đúng tab (đường bấm ws)."""
import os
import subprocess
import time
from pathlib import Path

from pw_adapter import PWDriver


def start(pw, work: Path, headless: bool = True):
    udd = work / "chrome-prof"
    udd.mkdir(parents=True, exist_ok=True)
    exe = pw.chromium.executable_path
    args = [exe, f"--user-data-dir={udd}", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--no-sandbox",
            "--disable-background-networking", "--window-size=1280,800", "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    f = udd / "DevToolsActivePort"
    end = time.time() + 20
    while time.time() < end and not (f.exists() and f.read_text().strip()):
        time.sleep(0.05)
    port = int(f.read_text().split()[0])
    addr = f"127.0.0.1:{port}"
    os.environ["NO_PROXY"] = ",".join(x for x in (os.environ.get("NO_PROXY"), "127.0.0.1,localhost") if x)
    browser = pw.chromium.connect_over_cdp(f"http://{addr}")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_viewport_size({"width": 1280, "height": 800})
    cdp = ctx.new_cdp_session(page)
    return {"proc": proc, "browser": browser, "page": page, "cdp": cdp, "drv": PWDriver(page, cdp, addr), "addr": addr}


def stop(h):
    try:
        h["browser"].close()
    except Exception:
        pass
    h["proc"].terminate()
    try:
        h["proc"].wait(5)
    except Exception:
        h["proc"].kill()

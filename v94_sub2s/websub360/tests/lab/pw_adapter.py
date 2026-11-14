# -*- coding: utf-8 -*-
"""Bộ chuyển Playwright → giao diện Selenium mà TAM dùng (execute_script / execute_async_script / execute_cdp_cmd /
get / refresh / current_url / find_element / send_keys). Để chạy ĐÚNG mã TAM (JS watcher + vòng chờ Python) trên Chromium
thật khi máy test không có chromedriver khớp phiên bản. Cả Selenium(chromedriver) lẫn Playwright đều chạy script bằng CDP
Runtime.callFunctionOn và gắn file bằng DOM.setFileInputFiles, nên phần CHẠY TRONG TRANG giống hệt; chi phí mỗi lệnh phía
Python khác nhau (chromedriver thêm 1 bước HTTP) → số LỆNH được đếm riêng, không suy ra ms của máy thật."""

WRAP_SYNC = "([src, args]) => (new Function(src)).apply(null, args)"
WRAP_ASYNC = """([src, args, to]) => new Promise((res) => {
  const t = setTimeout(() => res('__TAM_SCRIPT_TIMEOUT__'), to);
  (new Function(src)).apply(null, args.concat([(v) => { clearTimeout(t); res(v); }]));
})"""


class ScriptTimeout(Exception):
    pass


class NoAlert(Exception):
    pass


class _Alert:
    def __init__(self, drv=None):
        self.drv = drv

    @property
    def alert(self):
        raise NoAlert("no such alert")

    def new_window(self, kind="tab"):                 # như Selenium 4: mở tab mới và chuyển sang
        d = self.drv
        pg = d.page.context.new_page()
        d._pages[id(pg)] = pg
        d._use(pg)

    def window(self, handle):
        d = self.drv
        d._use(d._pages[handle])


class PWElement:
    def __init__(self, handle, drv):
        self.h = handle
        self.drv = drv

    def send_keys(self, text):
        self.drv.command_count += 1
        self.h.set_input_files(str(text).split("\n"))


class PWDriver:
    def __init__(self, page, cdp, debugger_address=None):
        self.page = page
        self.cdp = cdp
        # như Selenium attach qua debuggerAddress (Genlogin): chromedriver trả goog:chromeOptions.debuggerAddress
        self.capabilities = {"goog:chromeOptions": {"debuggerAddress": debugger_address}} if debugger_address else {}
        self.command_count = 0
        self.cdp_count = 0
        self.script_to_s = 30.0
        self.switch_to = _Alert(self)
        self._pages = {id(page): page}

    def _use(self, pg):
        self.page = pg
        self.cdp = pg.context.new_cdp_session(pg)

    @property
    def current_window_handle(self):
        return id(self.page)

    def close(self):
        self._pages.pop(id(self.page), None)
        self.page.close()

    @property
    def current_url(self):
        self.command_count += 1
        return self.page.url

    def get(self, url):
        self.command_count += 1
        self.page.goto(url, wait_until="load")

    def refresh(self):
        self.command_count += 1
        self.page.reload(wait_until="load")

    def set_script_timeout(self, s):
        self.script_to_s = float(s)

    def execute_script(self, js, *args):
        self.command_count += 1
        return self.page.evaluate(WRAP_SYNC, [js, list(args)])

    def execute_async_script(self, js, *args):
        self.command_count += 1
        h = self.page.evaluate_handle(WRAP_ASYNC, [js, list(args), int(self.script_to_s * 1000)])
        el = h.as_element()
        if el is not None:
            return PWElement(el, self)
        v = h.json_value()
        h.dispose()
        if v == "__TAM_SCRIPT_TIMEOUT__":
            raise ScriptTimeout("script timeout")
        return v

    def execute_cdp_cmd(self, cmd, params):
        self.command_count += 1
        self.cdp_count += 1
        return self.cdp.send(cmd, params or {})

    def find_element(self, by, sel):
        self.command_count += 1
        h = self.page.query_selector(sel)
        if h is None:
            raise RuntimeError(f"no such element: {sel}")
        return PWElement(h, self)

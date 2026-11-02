# -*- coding: utf-8 -*-
"""Regression (review V9.4.1): STATUS row verify when the target row now holds a DIFFERENT url in a form
_V94_YT_ID_IN_CELL does not recognise (but which the app accepts as a job URL)."""
import pytest
from test_v941 import _xlsx, _status


@pytest.mark.parametrize("other", [
    "https://www.YouTube.com/shorts/YYYYYYYYYYY",       # mixed-case host (common when pasted)
    "https://WWW.YOUTUBE.COM/watch?v=YYYYYYYYYYY",
    "https://www.youtube.com/v/YYYYYYYYYYY",             # _extract_youtube_video_id supports /v/
    "https://youtube.com/watch/?v=YYYYYYYYYYY",
    "https://www.youtube-nocookie.com/embed/YYYYYYYYYYY",
    "https://www.tiktok.com/@abc/video/7300000000000000000",
])
def test_row_shift_onto_unrecognised_url(v94, tmp_path, other):
    p = str(tmp_path / "v.xlsx")
    X = "https://www.youtube.com/shorts/XXXXXXXXXXX"
    _xlsx(p, [X])
    from openpyxl import load_workbook
    wb = load_workbook(p); ws = wb.active
    ws.insert_rows(2)                     # scanner/user inserts a row above X while the job runs
    ws.cell(row=2, column=1).value = other
    wb.save(p)
    v94._V94_VERIFY_SKIP_LOGGED[0] = False
    logs = []
    ok = v94.write_status_to_excel(p, 0, "STATUS", "DONE", logs.append, expected_url=X, url_col_name="URL")
    st = _status(p)
    print(other, "->", ok, st, logs)
    # what the hard requirement demands: the other URL's row must NOT be DONE
    assert st[0][1] != "DONE", (st, logs)

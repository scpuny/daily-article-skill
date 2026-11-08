#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号后台导出文件解析 —— 统一转成 CSV 文本，交给 stats_provider.import_csv 做列映射。

微信公众平台「内容分析 / 图文分析」导出的默认格式是 .xls，实际可能是：
  xls       OLE2 + BIFF8（Excel 97-2003 真二进制）→ 优先 xlrd，缺失时给明确提示
  xlsx      OOXML zip                              → 标准库 zipfile + ElementTree
  html-xls  HTML <table> 另存为 .xls（很常见）     → 标准库 HTMLParser
  xml-xls   Excel 2003 SpreadsheetML               → 标准库 ElementTree
  text      CSV / TSV / UTF-16 / GBK               → 原样透传给 csv.Sniffer

对外只暴露 parse(data: bytes, filename: str) -> (csv_text, fmt_label)
"""
import csv
import io
import os
import re
import sys
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
SSML_NS = "{urn:schemas-microsoft-com:office:spreadsheet}"


# ---------------------------------------------------------------- 基础工具

def _decode(data):
    for enc in ("utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def _fix_header(row):
    """DictReader 遇到空/重复表头会丢列，先规范化。"""
    out, seen = [], {}
    for i, v in enumerate(row):
        name = _clean(v) or f"列{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def to_csv(rows):
    if not rows:
        raise ValueError("文件里没有可解析的表格数据")
    # 丢掉整行为空的前导行（公众号导出常带标题行/说明行），找到含「阅读」的表头
    start = 0
    for i, r in enumerate(rows[:20]):
        joined = "".join(_clean(x) for x in r)
        if "阅读" in joined or "分享" in joined or "标题" in joined and "日期" in joined:
            start = i
            break
    rows = rows[start:]
    if not rows:
        raise ValueError("未找到包含「阅读 / 分享 / 标题」的表头行")
    rows = [[_clean(x) for x in r] for r in rows]

    # 微信后台「数据趋势概况」会把多个区块横向拼在一张表里（日×渠道 / 日×分享 /
    # 单篇×渠道）。扁平解析会把左侧区块的日期串到右侧单篇行上，导致归因完全错位。
    # 用「表头与所有数据行都为全空」的列做分隔，只保留含「标题」的那一块。
    width = max(len(r) for r in rows)
    hdr = (rows[0] + [""] * width)[:width]
    data = [(r + [""] * width)[:width] for r in rows[1:]]
    ti = next((j for j, v in enumerate(hdr) if "标题" in v), None)
    if ti is not None:
        sep = [j for j in range(width) if not hdr[j] and all(not r[j] for r in data)]
        lo = max([j for j in sep if j < ti], default=-1) + 1
        hi = min([j for j in sep if j > ti], default=width)
        if lo > 0 or hi < width:
            rows = [hdr[lo:hi]] + [r[lo:hi] for r in data]

    rows = [_fix_header(rows[0])] + rows[1:]
    width = max(len(r) for r in rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow((r + [""] * width)[:width])
    return buf.getvalue()


# ---------------------------------------------------------------- .xls (BIFF8)

def _xls_biff(data):
    """优先用 xlrd；环境里没装时用项目自带的零依赖 biff8 解析（结果逐格一致）。

    管理台若被系统 Python 启动，site-packages 里通常没有 xlrd，真 .xls 会直接
    报「缺少解析库」。兜底后任何解释器都能导入公众号后台导出的 .xls。
    """
    try:
        import xlrd
    except ImportError:
        xlrd = None
    if xlrd is not None:
        try:
            book = xlrd.open_workbook(file_contents=data)
            sh = book.sheet_by_index(0)
            rows = []
            for i in range(sh.nrows):
                row = []
                for j in range(sh.ncols):
                    c = sh.cell(i, j)
                    if c.ctype == xlrd.XL_CELL_DATE:
                        try:
                            row.append(xlrd.xldate.xldate_as_datetime(
                                c.value, book.datemode).strftime("%Y-%m-%d"))
                        except Exception:
                            row.append(c.value)
                    elif c.ctype == xlrd.XL_CELL_BOOLEAN:
                        row.append("1" if c.value else "0")
                    else:
                        row.append(c.value)
                rows.append(row)
            return rows
        except Exception:
            pass                     # xlrd 解不开也别直接失败，再试内置解析器

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    import biff8
    try:
        return biff8.read_xls(data)
    except Exception as e:
        raise ValueError(f".xls 解析失败：{e}。可尝试用 Excel/WPS 打开后另存为 .xlsx 或 CSV。")


# ---------------------------------------------------------------- .xlsx (OOXML)

def _col_index(ref):
    m = re.match(r"([A-Z]+)", str(ref or ""))
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise ValueError(f".xlsx 不是有效的压缩包：{e}")
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        try:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(XLSX_NS + "t")))
        except Exception:
            pass

    sheet_path = None
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet = wb.find(XLSX_NS + "sheets/" + XLSX_NS + "sheet")
        rid = sheet.get(REL_NS + "id") if sheet is not None else None
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for r in rels:
            if r.get("Id") == rid:
                target = (r.get("Target") or "").lstrip("/")
                sheet_path = target if target.startswith("xl/") else "xl/" + target
    except Exception:
        pass
    if not sheet_path or sheet_path not in names:
        sheet_path = next((n for n in sorted(names)
                           if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), None)
    if not sheet_path:
        raise ValueError(".xlsx 里找不到工作表数据")

    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for row in root.iter(XLSX_NS + "row"):
        cells = {}
        for c in row.findall(XLSX_NS + "c"):
            idx = _col_index(c.get("r"))
            t, v = c.get("t"), c.find(XLSX_NS + "v")
            if t == "inlineStr":
                isn = c.find(XLSX_NS + "is")
                val = "".join(x.text or "" for x in isn.iter(XLSX_NS + "t")) \
                    if isn is not None else ""
            elif t == "s":
                try:
                    val = shared[int(v.text)]
                except Exception:
                    val = ""
            else:
                val = v.text if v is not None and v.text is not None else ""
            cells[idx] = val
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


# ---------------------------------------------------------------- 伪 .xls：HTML <table>

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.in_cell = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell, self.in_cell = [], True
        elif tag == "br" and self.in_cell:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            if self.row is not None:
                self.row.append("".join(self.cell))
            self.cell, self.in_cell = None, False
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _html_table(text):
    p = _TableParser()
    p.feed(text)
    if not p.rows:
        raise ValueError("HTML 里没有 <table> 表格数据")
    return p.rows


# ---------------------------------------------------------------- 伪 .xls：SpreadsheetML 2003

def _ssml(text):
    try:
        root = ET.fromstring(re.sub(r"<\?xml[^>]*\?>", "", text, count=1).strip())
    except Exception as e:
        raise ValueError(f"XML 表格解析失败：{e}")
    rows = []
    for row in root.iter(SSML_NS + "Row"):
        cells, col = [], 0
        for c in row.findall(SSML_NS + "Cell"):
            try:
                idx = int(c.get(SSML_NS + "index") or "0") - 1
                col = max(col, idx)
            except ValueError:
                pass
            while len(cells) < col:
                cells.append("")
            d = c.find(SSML_NS + "Data")
            cells.append("".join(d.itertext()) if d is not None else "")
            col += 1
        rows.append(cells)
    return rows


# ---------------------------------------------------------------- 入口

def parse(data, filename=""):
    """返回 (csv_text, fmt_label)。csv_text 可直接喂给 stats_provider.import_csv。"""
    if not data:
        raise ValueError("文件内容为空")
    fn = str(filename or "").lower()
    head = data[:8]

    if head == OLE_MAGIC:
        rows = _xls_biff(data)
        return to_csv(rows), "xls"
    if head[:2] == b"PK":
        rows = _xlsx(data)
        return to_csv(rows), "xlsx"
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace").lstrip("\ufeff")
        return _text_or_table(text), "utf16"

    text = _decode(data).lstrip("\ufeff")
    low = text.lstrip().lower()[:2000]
    if low.startswith("<!doctype html") or low.startswith("<html") or "<table" in low:
        rows = _html_table(text)
        return to_csv(rows), "html-xls"
    if "workbook" in low and "<row" in low:
        rows = _ssml(text)
        return to_csv(rows), "xml-xls"
    if fn.endswith((".xls", ".xlsx")) and "<table" not in low and "workbook" not in low:
        raise ValueError("文件扩展名是 Excel，但内容是普通文本；请确认导出文件未损坏，"
                         "或用 Excel 打开后另存为 .xls/.xlsx。")
    return text, "csv"


def _text_or_table(text):
    low = text.lstrip().lower()[:2000]
    if low.startswith("<html") or "<table" in low:
        rows = _html_table(text)
        return to_csv(rows)
    if "workbook" in low and "<row" in low:
        rows = _ssml(text)
        return to_csv(rows)
    return text

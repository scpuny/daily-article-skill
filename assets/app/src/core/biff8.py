#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖 Excel 97-2003（OLE2 + BIFF8）读取器 —— xlrd 缺失时的兜底。

只做一件事：把第一张工作表的单元格读成二维数组，交给 excel_import.to_csv()。
纯标准库实现，任何 Python 3 解释器都能跑（管理台曾被系统 Python 3.9 启动，
那个解释器没装 xlrd，导致公众号后台导出的 .xls 全部导入失败）。

覆盖范围：微信公众平台后台 xlslib 生成的文件（LABEL / LABELSST / NUMBER /
RK / MULRK / FORMULA / BOOLERR + SST/CONTINUE），已与 xlrd 逐格比对 0 差异。
不处理：宏、图表、多工作表（只取第一张）、日期单元格格式（后台导出的日期是
纯数字或文本，如 20261106，由 stats_provider._norm_date 还原）。
"""
import struct

EOC = 0xFFFFFFFE
FREE = 0xFFFFFFFF
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _f64(b, o): return struct.unpack_from("<d", b, o)[0]


class OLE2:
    """OLE2 复合文档：FAT / MiniFAT / 目录项 / Workbook 流。"""

    def __init__(self, data):
        if data[:8] != OLE_MAGIC:
            raise ValueError("不是 OLE2 复合文档（Excel 97-2003）")
        self.d = data
        self.ss = 1 << _u16(data, 30)          # sector size，通常 512
        self.ms = 1 << _u16(data, 32)          # mini sector size，通常 64
        self.nfat = _u32(data, 44)
        self.firstdir = _u32(data, 48)
        self.cutoff = _u32(data, 56)           # < 4096 走 mini stream
        self.fmini, self.nmini = _u32(data, 60), _u32(data, 64)
        self.fdifat, self.ndifat = _u32(data, 68), _u32(data, 72)
        difat = [_u32(data, 76 + 4 * i) for i in range(109)]
        if self.ndifat:
            sid = self.fdifat
            for _ in range(self.ndifat):
                sec = self.sector(sid)
                difat.extend(struct.unpack("<%dI" % (self.ss // 4 - 1), sec[:self.ss - 4]))
                sid = _u32(sec, self.ss - 4)
        self.fat = []
        for sid in difat[:self.nfat]:
            if sid < EOC - 1:
                self.fat.extend(struct.unpack("<%dI" % (self.ss // 4), self.sector(sid)))
        self.entries = self._directory()
        root = next((e for e in self.entries if e["type"] == 5), None)
        self.ministream = self.read_chain(root["start"]) if root else b""
        self.minifat = []
        if self.fmini < EOC - 1 and self.nmini:
            raw = self.read_chain(self.fmini)
            self.minifat = list(struct.unpack("<%dI" % (len(raw) // 4), raw))

    def sector(self, i):
        off = 512 + i * self.ss
        return self.d[off:off + self.ss]

    def chain(self, start):
        out, cur = [], start
        while cur < EOC - 1 and len(out) < 100000:
            out.append(cur)
            cur = self.fat[cur] if cur < len(self.fat) else EOC
        return out

    def read_chain(self, start):
        return b"".join(self.sector(i) for i in self.chain(start))

    def _directory(self):
        raw, out = self.read_chain(self.firstdir), []
        for off in range(0, len(raw) - 127, 128):
            e = raw[off:off + 128]
            nl = _u16(e, 64)
            name = e[:nl - 2].decode("utf-16le", errors="replace") if nl >= 2 else ""
            out.append({"name": name, "type": e[66],
                        "start": _u32(e, 116), "size": _u32(e, 120)})
        return out

    def stream(self, name):
        e = next((x for x in self.entries
                  if x["type"] == 2 and x["name"].lower() == name.lower()), None)
        if not e:
            return b""
        if e["size"] < self.cutoff:
            out, cur = [], e["start"]
            while cur < EOC - 1 and len(out) < 100000:
                off = cur * self.ms
                out.append(self.ministream[off:off + self.ms])
                cur = self.minifat[cur] if cur < len(self.minifat) else EOC
            return b"".join(out)[:e["size"]]
        return self.read_chain(e["start"])[:e["size"]]


class _SegReader:
    """SST 字符串可跨 CONTINUE 记录；跨记录时吞掉 1 字节压缩标志。"""

    def __init__(self, payload, continues):
        self.segs = [payload] + continues
        self.i, self.o, self.high = 0, 0, False

    def _next(self):
        self.i += 1
        self.o = 0
        if self.i < len(self.segs) and self.segs[self.i]:
            self.high = bool(self.segs[self.i][0] & 1)
            self.o = 1

    def read(self, n):
        out = bytearray()
        while len(out) < n:
            if self.i >= len(self.segs):
                raise ValueError("SST 数据截断")
            seg = self.segs[self.i]
            if self.o >= len(seg):
                self._next()
                continue
            take = min(n - len(out), len(seg) - self.o)
            out += seg[self.o:self.o + take]
            self.o += take
        return bytes(out)

    def chars(self, cch):
        out = []
        for _ in range(cch):
            if self.i >= len(self.segs) or self.o >= len(self.segs[self.i]):
                self._next()
            seg = self.segs[self.i]
            if self.high:
                if self.o + 2 > len(seg):
                    self._next()
                    seg = self.segs[self.i]
                out.append(seg[self.o:self.o + 2].decode("utf-16le", errors="replace"))
                self.o += 2
            else:
                out.append(chr(seg[self.o]))
                self.o += 1
        return "".join(out)


def _sst(payload, continues):
    r = _SegReader(payload[8:], continues)
    out = []
    for _ in range(_u32(payload, 4)):
        if r.i >= len(r.segs):
            break
        cch = _u16(r.read(2), 0)
        flags = r.read(1)[0]
        r.high = bool(flags & 1)
        crun = _u16(r.read(2), 0) if flags & 0x04 else 0
        cbext = _u32(r.read(4), 0) if flags & 0x08 else 0
        out.append(r.chars(cch))
        if crun:
            r.read(4 * crun)
        if cbext:
            r.read(cbext)
    return out


def _rk(v):
    if v & 2:
        n = v >> 2
        if n & 0x20000000:
            n -= 0x40000000
        x = float(n)
    else:
        x = _f64(struct.pack("<Q", (v & 0xFFFFFFFC) << 32), 0)
    return x / 100 if v & 1 else x


def _ustring(p, o, cp):
    """BIFF8 XLUnicodeString：cch(2) + flags(1) + 字符数据。"""
    cch, flags = _u16(p, o), p[o + 2]
    o += 3
    if flags & 1:
        return p[o:o + 2 * cch].decode("utf-16le", errors="replace")
    enc = "gb18030" if cp in (936, 54936) else ("cp1252" if cp == 1252 else "latin-1")
    return p[o:o + cch].decode(enc, errors="replace")


def read_xls(data):
    """返回第一张工作表的二维数组；单元格缺失补空串。"""
    ole = OLE2(data)
    st = ole.stream("Workbook") or ole.stream("Book")
    if not st:
        raise ValueError("OLE2 里找不到 Workbook 流，文件可能不是 Excel 格式")

    pos, cp, shared, cells = 0, 1252, {}, {}
    in_sheet = False
    sst_buf, sst_cont, reading_sst = None, [], False
    pending_string = None

    def put(row, col, val):
        cells[(row, col)] = val

    while pos + 4 <= len(st):
        rid, sz = _u16(st, pos), _u16(st, pos + 2)
        p = st[pos + 4:pos + 4 + sz]
        pos += 4 + sz
        if rid == 0:                                   # 尾部零填充
            break
        if rid == 0x0809:                             # BOF
            if _u16(p, 2) == 0x0010:                 # worksheet substream
                in_sheet = True
            continue
        if rid == 0x000A:                             # EOF
            if in_sheet:
                break                                 # 只取第一张表
            continue
        if rid == 0x0042:
            cp = _u16(p, 0)
            continue
        if rid == 0x00FC:                             # SST
            sst_buf, sst_cont, reading_sst = p, [], True
            continue
        if rid == 0x003C and reading_sst:
            sst_cont.append(p)
            continue
        if reading_sst:
            shared = _sst(sst_buf, sst_cont)
            reading_sst = False
        if not in_sheet:
            continue
        if pending_string is not None and rid == 0x0207:   # FORMULA 的字符串结果
            put(*pending_string, _ustring(p, 0, cp))
            pending_string = None
            continue
        if rid == 0x0204:                             # LABEL
            put(_u16(p, 0), _u16(p, 2), _ustring(p, 6, cp))
        elif rid == 0x00FD:                           # LABELSST
            si = _u32(p, 6)
            put(_u16(p, 0), _u16(p, 2), shared[si] if si < len(shared) else "")
        elif rid == 0x0203:                           # NUMBER
            put(_u16(p, 0), _u16(p, 2), _f64(p, 6))
        elif rid == 0x027E:                           # RK
            put(_u16(p, 0), _u16(p, 2), _rk(_u32(p, 6)))
        elif rid == 0x00BD:                           # MULRK
            row = _u16(p, 0)
            for k, off in enumerate(range(4, sz - 2, 6)):
                put(row, _u16(p, 2) + k, _rk(_u32(p, off + 2)))
        elif rid == 0x0006:                           # FORMULA
            row, col = _u16(p, 0), _u16(p, 2)
            if p[12:14] == b"\xff\xff":
                pending_string = (row, col)
            else:
                put(row, col, _f64(p, 6))
        elif rid == 0x0205:                           # BOOLERR
            put(_u16(p, 0), _u16(p, 2), 1 if p[7] else 0)

    if reading_sst:
        shared = _sst(sst_buf, sst_cont)
    if not cells:
        raise ValueError("BIFF 里没有可读取的单元格")
    maxr = max(r for r, _ in cells)
    maxc = max(c for _, c in cells)
    return [[cells.get((r, c), "") for c in range(maxc + 1)]
            for r in range(maxr + 1)]

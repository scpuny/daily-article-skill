#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容运营台 · 写作策略反馈闭环（纯标准库、确定性、不调用任何模型 API）。

数据流：
  Agent 写作完成 → record_features() → data/strategy_log.jsonl      文章特征 + 本轮假设
  WebSearch 爆款 → record_signals()  → data/external_signals.jsonl  外部热度信号
  后台导出 xls   → ingest_feedback() → data/feedback.jsonl          文章级表现
  三者 → build() → data/writing_strategy.json（constraints[] 直接注入 Skill）

设计要点：
  - 黏性得分 = log1p(阅读 / √发表天数) × (1 + 0.6 × 互动 / √阅读)，不用绝对阅读量。
  - 样本 <8 篇不做统计推断，只给外部信号 + 冷启动规则；8~20 篇用贝叶斯收缩。
  - feedback 按标题 join index.json / strategy_log.jsonl，join 不上不参与统计。
  - 绝不写 stats_cache.json；管理台导入行为不变。

CLI 回退（无 MCP 时）：
  PY strategy_engine.py get
  PY strategy_engine.py ingest [--dir ~/Downloads]
  PY strategy_engine.py features --json '{"articleId":0,...}'
  PY strategy_engine.py signals  --json '[{"date":"2026-11-07",...}]'
  PY strategy_engine.py backfill   # 一次性回填历史文章特征（幂等，可移植/冷启动时用）
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 可移植引导：无论从 MCP、app.py 还是本文件直接运行，都能找到 paths / core 模块
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(_here)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import paths  # noqa: E402

D = lambda: Path(paths.data_dir())
S = lambda: Path(paths.state_dir())
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 30


# ---------------------------------------------------------------- 基础读写
def _read(p, default):
    p = Path(p)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonl(name):
    p = D() / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                      # 单行损坏不拖垮整条链路
    return out


def count_lines(name):
    """台账行数（只读，供管理台策略面板显示样本规模）。"""
    p = D() / name
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def _append(name, rec):
    rec = dict(rec)
    rec.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with (D() / name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_features(limit=5):
    """最近几轮写作实验（只读，供管理台策略面板展示策略版本与假设）。"""
    rows = _jsonl("strategy_log.jsonl")
    keys = ("date", "title", "pillar", "titlePattern", "hookType",
            "strategyVersion", "hypothesis", "hypothesisTarget")
    return [{k: r.get(k) for k in keys} for r in reversed(rows[-limit:])]


def _save(name, obj):
    p = D() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 写入侧（Agent 调用）
FEATURE_FIELDS = {"articleId", "date", "title", "pillar", "titlePattern",
                  "hookType", "wordCount", "codeBlocks", "strategyVersion", "hypothesis"}


def record_features(rec):
    """一篇文章写完后登记特征与「本轮假设」，供日后验证策略是否真的变好。

    FEATURE_FIELDS 为必填；hypothesisTarget（{dim,val} 结构化目标特征）为可选字段，
    传入则一并持久化，不传则留空，不影响既有调用。
    """
    if not isinstance(rec, dict):
        return {"ok": False, "error": "features 必须是对象"}
    miss = FEATURE_FIELDS - rec.keys()
    if miss:
        return {"ok": False, "error": "缺字段 " + ", ".join(sorted(miss))}
    # articleId 是写入当刻的 index 位置；新文章插到头部 / 删除文章会让它整体错位。
    # 同时落一个稳定标题键，配对时优先用它，台账才不会因列表重排而串味。
    rec = dict(rec)
    rec.setdefault("articleKey", _norm(rec.get("title")))
    _append("strategy_log.jsonl", rec)
    return {"ok": True, "articleId": rec["articleId"],
            "strategyVersion": rec["strategyVersion"]}


def record_signals(items):
    """WebSearch 得到的外部高互动样本。字段：date/title/titlePattern/hookType/
    engagementLevel(0.5~2)/source/url。按 30 天半衰参与冷启动。"""
    if not isinstance(items, list):
        return {"ok": False, "error": "items 必须是数组"}
    n = 0
    for x in items:
        if isinstance(x, dict) and (x.get("titlePattern") or x.get("hookType")):
            _append("external_signals.jsonl", x)
            n += 1
    return {"ok": True, "count": n}


# ---------------------------------------------------------------- 历史文章特征回填
# 规则化提取「标题句式 / 开头钩子 / 支柱」，不是编造表现数据，也不假装当时跑过策略。
# 用途：账号已经有真实单篇表现，但 strategy_log.jsonl 是空的，配对不上就永远 cold。
TITLE_PATTERNS = [
    ("数字清单+可运行解法", r"\d+\s*个|三个|三道|几个|N个|坑|清单"),
    ("否定式劝阻+反直觉判断", r"^别|^不要|^停止|别再|别裸写|先躲开"),
    ("故障复盘+反直觉根因", r"翻车|雪崩|耗尽|打满|误删|故障|复盘|错乱|踩坑|事故"),
    ("反常识断言+趋势判断", r"重塑|反噬|暗线|收敛|开始卷|不再|为什么"),
    ("组件版本+冒号+具体做法", r"[：:]"),
]
HOOK_PATTERNS = [
    ("线上故障现场", r"报错|异常|告警|凌晨|生产事故|监控|成功率|飙红|阻塞|线程栈|事故|故障|掉到|超时|抖动"),
    ("反常识判断", r"不是|其实|大多数人|很多人|别再|不再是|差的是|从相反"),
    ("版本特性引入", r"版本|升级|发布|新特性|正式支持|加入了"),
    ("痛点场景直给", r"一次性|最近|上周|我们团队|救火|重复造轮子"),
]


def _by_rules(text, rules, fallback):
    for name, pat in rules:
        if re.search(pat, text or ""):
            return name
    return fallback


def _infer_pillar(a):
    s = " ".join([str(a.get("title") or ""), str(a.get("subtitle") or "")]
                 + [str(x) for x in (a.get("tags") or [])])
    if re.search(r"AI|Agent|RAG|LLM|大模型|模型接入|检索增强|向量|Prompt|智能", s, re.I):
        return "B"
    if re.search(r"翻车|雪崩|耗尽|打满|误删|故障|复盘|错乱|踩坑|事故", s):
        return "D"
    if re.search(r"封装|架构|管道|状态机|拦截器|插件|可插拔|Starter|限流|签名|透传|CLI|内核|收口", s, re.I):
        return "A"
    return "C"


def backfill_features():
    """从 index.json + 正文 md 补登历史文章特征，返回新增条数（幂等）。

    只补 strategy_log.jsonl 里没有的文章（按归一化标题判重，不看 articleId）；
    已由定时任务登记的真实实验记录不会被覆盖。回填记录带 origin=backfill /
    strategyVersion=backfill-* 便于区分。
    """
    idx = paths.read_json(paths.index(), []) or []
    have = {_norm(x.get("title")) for x in _jsonl("strategy_log.jsonl")}
    ver = "backfill-" + date.today().strftime("%Y%m%d")
    n = 0
    for i, a in enumerate(idx):
        title = str(a.get("title") or "")
        key = _norm(title)
        if not key or key in have:
            continue
        md = ""
        rel = a.get("md")
        if rel:
            try:
                md = Path(paths.resolve(rel)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                md = ""
        opening = " ".join([str(a.get("subtitle") or ""), md[:400]])
        rec = {
            "articleId": i,
            "articleKey": key,
            "date": a.get("date") or "",
            "title": title,
            "pillar": _infer_pillar(a),
            "titlePattern": _by_rules(title, TITLE_PATTERNS, "陈述式标题"),
            "hookType": _by_rules(opening, HOOK_PATTERNS, "概念铺垫"),
            "wordCount": len(re.sub(r"\s+", "", md)),
            "codeBlocks": len(re.findall(r"^\s*```", md, re.M)) // 2,
            "strategyVersion": ver,
            "hypothesis": "（历史回填）当时未记录策略版本，特征按标题/正文规则化提取，不作为策略效果验证依据。",
            "origin": "backfill",
        }
        _append("strategy_log.jsonl", rec)
        n += 1
    return {"ok": True, "added": n, "total": len(idx), "version": ver}


def reanchor_features(write=True):
    """把 strategy_log.jsonl 的 articleId 重锚到当前 index.json 位置（按标题）。

    index.json 是数组：新文章插到头部、删掉中间一篇，后面的位置索引都会整体
    平移，历史台账里的 articleId 随即失真。本函数幂等：
      - 标题能对上当前文章 → 修正 articleId、补 articleKey、清 orphan 标记；
      - 标题已不在 index（文章被删）→ 保留原值并标 orphan，绝不删历史记录；
      - 同一标题多条登记（回填 + 真实实验）→ 全部重锚到同一个位置。
    只在有变化时写盘；删除文章 / 每轮 build 前调用，可自愈位置漂移。
    """
    idx = paths.read_json(paths.index(), []) or []
    by_title = {}
    for i, a in enumerate(idx):
        by_title.setdefault(_norm(a.get("title")), i)
    rows = _jsonl("strategy_log.jsonl")
    changed = orphan = 0
    for r in rows:
        key = str(r.get("articleKey") or _norm(r.get("title")))
        if r.get("articleKey") != key:
            r["articleKey"] = key
            changed += 1
        i = by_title.get(key)
        if i is None:
            if not r.get("orphan"):
                r["orphan"] = True
                changed += 1
            # 文章已从 index 删除：位置索引不再有意义，置空避免被误当有效 id
            if r.get("articleId") is not None:
                r["articleId"] = None
                changed += 1
            orphan += 1
            continue
        if r.pop("orphan", None):
            changed += 1
        if str(r.get("articleId")) != str(i):
            r["articleId"] = i
            changed += 1
    if write and changed:
        _save_jsonl("strategy_log.jsonl", rows)
    return {"ok": True, "changed": changed, "total": len(rows),
            "articles": len(idx), "orphan": orphan}


# ---------------------------------------------------------------- 反馈吸收（异步、机会式）
_norm = lambda s: re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def _title_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # 主匹配：归一化子串（较长标题含较短标题）
    if min(len(a), len(b)) > 8 and (a in b or b in a):
        return True
    # R7 兜底：字符集合 Jaccard，覆盖标点 / 空格 / 前缀差异
    # （如「Java 21 虚拟线程：先躲开这三个坑」vs「Java21虚拟线程先躲开这三个坑」）
    if min(len(a), len(b)) >= 4:
        sa, sb = set(a), set(b)
        union = sa | sb
        if union and len(sa & sb) / len(union) >= 0.6:
            return True
    return False


def _log_for_article(title, i, art, logs):
    """按稳定标题键取特征台账，免疫 index.json 重排。

    articleId 只是「写入当刻的列表位置」：新文章插到头部、或删掉中间一篇，
    后面所有 id 都会整体平移。只靠它配对会把上一篇的 pillar / 句式 / 钩子
    安到这一篇上，归因结论全错。所以顺序是：
      1) articleKey / 归一化标题精确命中（取最后一条，允许同一篇多轮登记）
      2) 标题模糊匹配且唯一命中
      3) 位置索引兜底，且该条记录的标题必须确实属于这篇文章
    """
    key = _norm(title)
    exact = [l for l in logs
             if str(l.get("articleKey") or "") == key or _norm(l.get("title")) == key]
    if exact:
        return exact[-1]
    fuzzy = [l for l in logs if _title_match(title, l.get("title"))]
    if len(fuzzy) == 1:
        return fuzzy[0]
    for l in reversed(logs):
        if str(l.get("articleId")) == str(i) and _title_match(l.get("title"), art.get("title")):
            return l
    return {}


def _join(title, idx, logs):
    """把导出的图文标题配对到本地文章特征；配不上返回 {}，不瞎归因。"""
    for i, art in enumerate(idx):
        if not _title_match(title, art.get("title")):
            continue
        log = _log_for_article(art.get("title"), i, art, logs)
        return {"articleId": i, "published": log.get("date") or art.get("date") or "",
                **{k: log.get(k) for k in ("pillar", "titlePattern", "hookType",
                                           "wordCount", "codeBlocks", "strategyVersion")}}
    return {}


def absorb_rows(rows):
    """把已解析好的带标题行并入 feedback.jsonl，按稳定标题键 UPSERT。

    供 ingest_feedback（扫描 Downloads）、/api/stats/import、fetch-total 共用。
    原来「只追加、已存在就跳过」会让 feedback 永远停在首次导入的读数：
    后台总表每次导出的都是累计值，重导后 stats_cache.json 更新了，策略样本
    却还是旧数，KPI 与归因从此分叉。这里改成：
      - read / share 是累计量，只在新值更大时更新（重导旧文件不会把数据打回去）；
      - 已由单篇明细补全的行（有 detailFrom）保留 fav/like，不被总表洗掉；
      - watch/thumb/完读率/停留/detailFrom/ts 一律原样保留；
      - msgid / url 只补空，不覆盖。
    返回 added / updated / skipped / regressions，写盘后顺便对齐 KPI 快照。
    """
    fb = _jsonl("feedback.jsonl")
    by_key, by_title = {}, {}
    for i, x in enumerate(fb):
        k = (str(x.get("date") or ""), _norm(x.get("title")))
        by_key.setdefault(k, i)
        by_title.setdefault(_norm(x.get("title")), []).append(i)

    added = updated = skipped = regressions = 0
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("title"):
            continue                       # 渠道日报 / 汇总行
        # 「数据趋势概况」这类复合表会把左侧日粒度的日期串到右侧单篇行上，
        # 产生日期错配、阅读为 0 的假样本。只收「合法日期 + 有表现」的单篇行。
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("date") or "")):
            continue
        try:
            has_signal = float(r.get("read") or 0) + float(r.get("share") or 0) > 0
        except (TypeError, ValueError):
            has_signal = False
        if not has_signal:
            continue
        tkey = _norm(r["title"])
        dkey = (str(r.get("date") or ""), tkey)
        pos = by_key.get(dkey)
        if pos is None:
            # 同一篇在不同导出里「发表日期」写法有差异时，按标题唯一命中兜底
            same = by_title.get(tkey) or []
            if len(same) == 1:
                pos = same[0]
        if pos is None:
            rec = {k: r.get(k) for k in ("date", "title", "read", "share", "fav", "like")}
            rec["ts"] = datetime.now().isoformat(timespec="seconds")
            # 第五批：total 表的「内容url」→ msgid 是后续按按钮拉单篇明细的唯一线索
            if r.get("msgid"):
                rec["msgid"] = r["msgid"]
                rec["url"] = r.get("url") or ""
            fb.append(rec)
            by_key[dkey] = len(fb) - 1
            by_title.setdefault(tkey, []).append(len(fb) - 1)
            added += 1
            continue

        cur = fb[pos]
        dirty = False
        for k in ("read", "share"):
            try:
                nv, ov = int(r.get(k) or 0), int(cur.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if nv > ov:
                cur[k] = nv
                dirty = True
            elif nv < ov:
                regressions += 1          # 旧导出重放：累计量不回退
        for k in ("fav", "like"):
            if cur.get("detailFrom"):
                continue                  # 单篇明细是更准的来源，保留
            try:
                nv = int(r.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if int(cur.get(k) or 0) != nv:
                cur[k] = nv
                dirty = True
        for k in ("msgid", "url"):
            if not cur.get(k) and r.get(k):
                cur[k] = r[k]
                dirty = True
        if dirty:
            updated += 1
        else:
            skipped += 1

    if added or updated:
        _save_jsonl("feedback.jsonl", fb)
        try:
            _sync_stats_cache_interaction()
        except Exception:
            pass
    return {"ok": True, "added": added, "updated": updated,
            "skipped": skipped, "regressions": regressions}


# ---------------------------------------------------------------- 单篇明细补全（收藏 / 在看 / 点赞）
# 批量 total_*.xls 只给阅读/分享；文章详情里下载的「数据明细（<标题>...）.xls」才带
# 收藏/在看/点赞/完读率。该文件是键值横排 + 后面挂趋势/画像，parse_rows() 解不了，
# 且里面没有标题/日期/URL，唯一配对线索是文件名里的截断标题。
SINGLE_KEYS = {
    "阅读(人)": "read", "平均停留时长(秒)": "dwellSec", "完读率": "completeRate",
    "听全文(人)": "listen", "新增关注(人)": "follow", "分享(人)": "share",
    "在看(人)": "watch", "点赞(人)": "thumb", "收藏(人)": "fav",
    "赞赏(分)": "reward", "评论(条)": "comment",
}
INT_DETAIL_KEYS = ("listen", "follow", "reward", "comment")
FLOAT_DETAIL_KEYS = ("completeRate", "dwellSec")


def _norm_detail_key(s):
    return str(s or "").strip().replace("（", "(").replace("）", ")")


def is_single_detail(filename):
    """文件名形如「数据明细（Vue3 组合式函数：把请求层封装成状态...）.xls」。"""
    return bool(re.search(r"数据明细[（(]", str(filename or "")))


def title_from_filename(filename):
    """从单篇明细文件名取被截断的标题，去掉尾部省略号。"""
    m = re.search(r"数据明细[（(](.+?)[）)]", str(filename or ""))
    if not m:
        return ""
    t = m.group(1).strip()
    return re.sub(r"[.．…]+\s*$", "", t).strip()


def parse_single_detail(text):
    """只取单篇 xls 前 15 行的键值区；遇到趋势/画像区块即停。"""
    out = {}
    for line in (text or "").splitlines()[:15]:
        if any(flag in line for flag in ("阅读转化", "阅读数据趋势明细", "性别分布")):
            break
        cells = [c.strip() for c in line.split(",")]
        for i, c in enumerate(cells[:-1]):
            k = _norm_detail_key(c)
            if k not in SINGLE_KEYS:
                continue
            v = cells[i + 1].strip().rstrip("%").strip()
            if not v:
                continue
            try:
                out[SINGLE_KEYS[k]] = float(v)
            except ValueError:
                continue
    return out if any(k in out for k in ("fav", "share", "watch", "thumb", "read")) else {}


# ------------------------------------------------ 单篇明细扩展区块（渠道 / 转化漏斗 / 人群画像）
# 网络入口 mp_session.download_detail(action=detailpage&download=1) 与后台手动下载的
# 离线 .xls 是同一份文件，两条链路都经 excel_import.parse() 转 CSV 后汇入此处。
# 因此只解析一次即可双端生效：离线是保底，网络拉取是主路径（数据更新、无需人工下载）。
DETAIL_SECTIONS = {
    "阅读转化": "funnel", "阅读数据趋势明细": "trend",
    "性别分布": "gender", "年龄分布": "age", "地域分布": "region",
}
FUNNEL_KEYS = {
    "送达人数": "delivered", "公众号消息阅读人数": "msgRead",
    "首次分享人数": "firstShare", "总分享人数": "totalShare",
    "分享产生的阅读人数": "shareRead",
}
_SECTION_HEADERS = ("数据指标", "性别", "年龄", "省份/直辖市", "日期")


def _pnum(s):
    try:
        return float(str(s).strip().rstrip("%").replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_detail_sections(text):
    """按区块切开单篇明细 CSV。

    overview 仍由 parse_single_detail() 负责（to_csv 会吞掉「数据概况」标题行）；
    trend 是 4 列（日期|渠道|阅读|分享），其余区块是 2 列（键|值）。
    """
    import csv
    cur, out = None, {}
    for line in (text or "").splitlines():
        cells = [c.strip() for c in next(csv.reader([line]))]
        title = cells[1] if len(cells) > 1 else ""
        if title in DETAIL_SECTIONS:
            cur = DETAIL_SECTIONS[title]
            out.setdefault(cur, [])
            continue
        if cur is None:
            continue
        vals = [c for c in cells[1:] if c]
        if not vals or vals[0] in _SECTION_HEADERS:
            continue
        if cur == "trend":
            # 数值在第 3 列，vals[1] 是中文渠道名（不是数字）
            if len(vals) >= 3 and _pnum(vals[2]) is not None:
                out[cur].append(vals[:4])
        elif len(vals) >= 2 and _pnum(vals[1]) is not None:
            out[cur].append(vals[:2])
    return out


def derive_profile(text, vals=None):
    """单篇 → {funnel, channels, trend, profile, metrics}；无扩展区块返回 {}。"""
    sec = parse_detail_sections(text)
    if not any(sec.get(k) for k in ("funnel", "trend", "gender", "age", "region")):
        return {}
    ov = vals if vals is not None else parse_single_detail(text)
    read = int(ov.get("read") or 0)

    def dist(key):
        return {r[0]: _pnum(r[1]) for r in sec.get(key, [])
                if len(r) > 1 and _pnum(r[1]) is not None}

    gender, age, region = dist("gender"), dist("age"), dist("region")
    funnel = {FUNNEL_KEYS[r[0]]: _pnum(r[1]) for r in sec.get("funnel", [])
              if r[0] in FUNNEL_KEYS and len(r) > 1 and _pnum(r[1]) is not None}
    channels, trend = {}, []
    for r in sec.get("trend", []):
        trend.append({"date": r[0], "channel": r[1], "read": _pnum(r[2]),
                      "share": _pnum(r[3]) if len(r) > 3 else 0})
        if r[1] != "全部":
            channels[r[1]] = channels.get(r[1], 0) + (_pnum(r[2]) or 0)
    ch_total = sum(channels.values()) or 0
    core_age = sum(v for k, v in age.items() if k.startswith(("26-35", "36-45")))
    delivered, msg_read = funnel.get("delivered"), funnel.get("msgRead")
    total_share, share_read = funnel.get("totalShare"), funnel.get("shareRead")
    metrics = {
        "n": read,
        "malePct": gender.get("男"),
        "coreAgePct": round(core_age, 2) if core_age else None,
        "topRegion": max(region, key=region.get) if region else None,
        "topRegionPct": max(region.values()) if region else None,
        "regionCount": len([k for k in region if k != "未知"]),
        "regionHHI": round(sum((v / 100) ** 2 for v in region.values()), 3) if region else None,
        "recommendPct": round(channels.get("推荐", 0) / ch_total * 100, 1) if ch_total else None,
        "deliverOpenRate": round(msg_read / delivered * 100, 2)
                           if delivered and msg_read is not None else None,
        "shareViral": round(share_read / total_share, 2)
                      if total_share and share_read is not None else None,
    }
    return {"funnel": funnel, "channels": channels, "trend": trend,
            "profile": {"gender": gender, "age": age, "region": region, "n": read},
            "metrics": metrics}


def _profile_patch(text, vals):
    """扩展区块 → feedback 行增量字段。无区块返回 {}，旧行不受影响。"""
    d = derive_profile(text, vals)
    if not d:
        return {}
    return {"funnel": d["funnel"], "channels": d["channels"], "trend": d["trend"],
            "profile": d["profile"], "metrics": d["metrics"]}


def profile_summary():
    """聚合所有带画像的 feedback 行，供大屏 /api/stats 展示（只读、不写盘）。

    按各篇阅读人数 n 加权，避免小样本（如 n=30）与大盘（n=240）等权拉偏结论。
    """
    rows = _jsonl("feedback.jsonl")
    total = len([r for r in rows if r.get("title")])
    covered = [r for r in rows if r.get("profile")]
    if not covered:
        return {"covered": 0, "total": total, "gap": total, "n": 0,
                "metrics": {}, "channels": {}, "byArticle": [], "stability": {}}

    def n_of(r):
        return _pnum((r.get("profile") or {}).get("n")) or 0

    def wavg(key):
        pairs = [(_pnum((r.get("metrics") or {}).get(key)), n_of(r)) for r in covered]
        pairs = [(v, n) for v, n in pairs if v is not None and n]
        return round(sum(v * n for v, n in pairs) / sum(n for _, n in pairs), 2) if pairs else None

    def rng(key):
        vs = [_pnum((r.get("metrics") or {}).get(key)) for r in covered]
        vs = [v for v in vs if v is not None]
        return round(max(vs) - min(vs), 2) if len(vs) > 1 else None

    channels, region_total = {}, {}
    for r in covered:
        for k, v in (r.get("channels") or {}).items():
            channels[k] = channels.get(k, 0) + (_pnum(v) or 0)
        n = n_of(r)
        for k, v in ((r.get("profile") or {}).get("region") or {}).items():
            if k != "未知":
                region_total[k] = region_total.get(k, 0) + (_pnum(v) or 0) * n
    by_article = [{
        "title": r.get("title"), "date": r.get("date"), "n": n_of(r),
        "recommendPct": (r.get("metrics") or {}).get("recommendPct"),
        "malePct": (r.get("metrics") or {}).get("malePct"),
        "deliverOpenRate": (r.get("metrics") or {}).get("deliverOpenRate"),
        "source": r.get("detailFrom") or "",
    } for r in covered]
    return {
        "covered": len(covered), "total": total, "gap": max(0, total - len(covered)),
        "n": int(sum(n_of(r) for r in covered)),
        "metrics": {
            "malePct": wavg("malePct"), "coreAgePct": wavg("coreAgePct"),
            "recommendPct": wavg("recommendPct"),
            "deliverOpenRate": wavg("deliverOpenRate"),
            "shareViral": wavg("shareViral"), "regionHHI": wavg("regionHHI"),
            "topRegion": max(region_total, key=region_total.get) if region_total else None,
            "regionCount": len(region_total),
        },
        "channels": channels,
        "byArticle": sorted(by_article, key=lambda x: x["n"], reverse=True),
        "stability": {"malePctRange": rng("malePct"),
                      "recommendPctRange": rng("recommendPct")},
    }


def _detail_patch(vals, source):
    """单篇明细 → feedback 行的互动字段补丁（第四批手动导入 / 第五批按钮拉取共用）。

    只补互动，绝不动 read/share/date/title/ts；like = 在看 + 点赞，得分口径不变。
    """
    iv = lambda k: int(vals.get(k) or 0)
    patch = {"fav": iv("fav"), "watch": iv("watch"), "thumb": iv("thumb")}
    patch["like"] = patch["watch"] + patch["thumb"]
    for k in INT_DETAIL_KEYS:
        if k in vals:
            patch[k] = int(vals[k])
    for k in FLOAT_DETAIL_KEYS:
        if k in vals:
            patch[k] = round(vals[k], 4)
    patch["detailFrom"] = str(source or "")
    return patch


def _same_detail_values(r, patch):
    """幂等比较只看表现字段。

    detailFrom 是「这次是哪个文件写进来的」溯源标签，不同来源（手动导文件名 /
    批量拉取 msgid）值必然不同；若把它算进比较，同一份数据换个文件名就会被判成
    「有新变化」而反复写盘，也可能让两个不同文件的导入互相干扰。
    """
    return all(r.get(k) == v for k, v in patch.items() if k != "detailFrom")


_DETAIL_FIELD_KEYS = ("fav", "watch", "thumb", "like", "listen", "follow",
                     "reward", "comment", "completeRate", "dwellSec")


def _detail_fields(patch):
    """只回互动字段：画像/渠道是大对象，不该塞进 toast 的字段回显。"""
    return {k: v for k, v in patch.items() if k in _DETAIL_FIELD_KEYS}


def _sync_stats_cache_interaction():
    """把 feedback.jsonl 的 fav/like 增量同步到 stats_cache.json。

    管理台 KPI 的 remoteSummary 只从 stats_cache.json 汇总；第四批单篇补全
    原本只写 feedback.jsonl，导致「累计收藏 / 在看·点赞」永远显示 0。
    这里只回填这两个字段（articles 逐篇 + daily 按日汇总），
    绝不动 read/share/date/title/source/updatedAt/format。
    全量对齐：一次调用即可补齐所有历史已补全文章。异常不阻断主流程。
    """
    try:
        import stats_provider
        fb = {(r.get("date"), _norm(r.get("title"))): r
              for r in _jsonl("feedback.jsonl") if r.get("title")}
        c = stats_provider._cache()
        arts, daily = c.get("articles") or [], c.get("daily") or []
        changed = False
        for a in arts:
            src = fb.get((a.get("date"), _norm(a.get("title"))))
            if not src:
                continue
            for k in ("fav", "like"):
                v = int(src.get(k) or 0)
                if int(a.get(k) or 0) != v:
                    a[k] = v
                    changed = True
        for d in daily:
            same = [a for a in arts if a.get("date") == d.get("date")]
            if not same:
                continue
            for k in ("fav", "like"):
                v = sum(int(a.get(k) or 0) for a in same)
                if int(d.get(k) or 0) != v:
                    d[k] = v
                    changed = True
        if changed:
            stats_provider._write_json(paths.stats_cache(), c)
        return changed
    except Exception:
        return False


def absorb_single_detail(text, filename=""):
    """把单篇明细的互动字段 UPSERT 回 feedback.jsonl 已有行（按标题唯一命中）。

    绝不改 read/share/date/title；命中 0 篇或多篇一律跳过并给诊断，宁可漏补不可错补。
    幂等：值全相同则不写盘，保留原有 ts。
    """
    vals = parse_single_detail(text)
    if not vals:
        return {"ok": False, "matched": 0, "updated": 0, "reason": "未识别到单篇明细键值区"}
    hint = title_from_filename(filename)
    if len(_norm(hint)) < 8:
        return {"ok": False, "matched": 0, "updated": 0, "reason": "文件名标题过短(<8)，无法可靠配对"}
    rows = _jsonl("feedback.jsonl")
    hits = [i for i, r in enumerate(rows) if _title_match(hint, r.get("title"))]
    if len(hits) != 1:
        return {"ok": False, "matched": len(hits), "updated": 0,
                "reason": f"标题命中 {len(hits)} 篇（需唯一命中），跳过以防错补"}
    r = rows[hits[0]]
    patch = _detail_patch(vals, filename)
    patch.update(_profile_patch(text, vals))      # 离线入口：画像/渠道/漏斗
    if _same_detail_values(r, patch):
        # 台账已补全但快照可能仍是旧的（本次修复前导入的），仍做一次增量对齐
        synced = _sync_stats_cache_interaction()
        detail = sync_detail_source(r.get("title"), filename)
        return {"ok": True, "matched": 1, "updated": 0, "idempotent": True,
                "title": r.get("title"), "reason": "数据已是最新，无需重复写入",
                "fields": _detail_fields(patch), "statsSynced": synced,
                "detailIndex": detail}
    r.update(patch)
    _save_jsonl("feedback.jsonl", rows)
    synced = _sync_stats_cache_interaction()
    detail = sync_detail_source(r.get("title"), filename)
    return {"ok": True, "matched": 1, "updated": 1, "title": r.get("title"),
            "fields": _detail_fields(patch), "statsSynced": synced,
            "detailIndex": detail}


def absorb_single_file(data_b64, filename=""):
    """管理台导入入口：base64 → excel_import.parse → absorb_single_detail。"""
    import base64
    import excel_import
    try:
        raw = base64.b64decode(data_b64 or "")
    except Exception as e:
        return {"ok": False, "error": "文件编码无效: " + str(e)}
    if not raw:
        return {"ok": False, "error": "文件内容为空"}
    text, _ = excel_import.parse(raw, filename)
    return absorb_single_detail(text, filename)


# ================================================================ 第五批：手动拉取 + 7 天定格
# 不挂定时任务、不后台轮询：只有 POST /api/strategy/fetch-detail(s) 才会联网。
# state/detail_index.json 是 msgid 台账与定格状态的唯一真源；feedback.jsonl 只存表现，
# stats_cache.json 只供管理台 KPI 读取，三者互不覆盖。
#
# v3 口径（2026-11-09）：定格 ≠ 永久锁。发表满 FREEZE_DAYS 天的文章，无论是否定格，
# 都可以由用户显式重拉（补画像 / 修数据），成功后刷新 frozenAt 并累计 refetchCount；
# 未满 FREEZE_DAYS 天一律拒绝，这是后端硬门槛，不依赖前端判断。
# 默认批量仍只补「到期且未定格」的缺口，重拉必须走 scope=frozen 的显式入口。
FREEZE_DAYS = 7
_MSGID_SCAN_CACHE = {"sig": "", "rows": []}


def msgid_from_url(url):
    """复用 stats_provider 的实现（内容url → mid_idx），单点维护。"""
    import stats_provider
    return stats_provider.msgid_from_url(url)


def _detail_index():
    doc = _read(S() / "detail_index.json", {"items": {}})
    if not isinstance(doc, dict):
        doc = {"items": {}}
    doc.setdefault("items", {})
    return doc


def _write_detail_index(doc):
    p = S() / "detail_index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    doc["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def sync_msgids(rows=None):
    """把 total 表已解析行（含 url/msgid）并进 detail_index.json，并回填 feedback 的 msgid/url。

    已存在的条目只补空字段，绝不覆盖 frozenAt。返回新增条数与 feedback 回填数。
    """
    doc = _detail_index()
    items = doc["items"]
    added, patched = 0, 0
    fb_rows = _jsonl("feedback.jsonl")
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        mid = str(r.get("msgid") or msgid_from_url(r.get("url")) or "")
        if not mid:
            continue
        cur = items.get(mid) or {}
        for k, v in (("msgid", mid), ("title", r.get("title")),
                     ("date", r.get("date")), ("url", r.get("url") or "")):
            if not cur.get(k) and v:
                cur[k] = v
        cur.setdefault("frozenAt", "")
        cur.setdefault("detailFrom", "")
        cur.setdefault("lastError", "")
        cur.setdefault("lastFetchedAt", "")
        if mid not in items:
            added += 1
        items[mid] = cur
        # feedback 台账按标题唯一命中才回填，宁缺勿错
        hits = [x for x in fb_rows if _norm(x.get("title")) == _norm(r.get("title"))]
        if len(hits) != 1:
            hits = [x for x in fb_rows if _title_match(r.get("title"), x.get("title"))]
        if len(hits) == 1:
            x = hits[0]
            if x.get("msgid") != mid or not x.get("url"):
                x["msgid"] = mid
                x["url"] = cur.get("url", "")
                patched += 1
    if added or patched:
        if patched:
            _save_jsonl("feedback.jsonl", fb_rows)
        _write_detail_index(doc)
    return {"ok": True, "added": added, "patched": patched, "total": len(items)}


def sync_msgids_from_downloads(download_dir=None):
    """首次使用兜底：从 ~/Downloads/total_*.xls 只读解析出 msgid（不写 feedback 明细）。

    带 (文件名, mtime, size) 签名缓存，不会每刷一次页面就重解析；台账被删也会重建。
    """
    import excel_import
    import stats_provider
    dl = Path(download_dir or (Path.home() / "Downloads"))
    if not dl.is_dir():
        return {"ok": True, "files": 0, "rows": 0}
    files = sorted([f for f in dl.glob("total_*.xls*")
                    if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES],
                   key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    sig = "|".join(f"{f.name}:{f.stat().st_mtime_ns}:{f.stat().st_size}" for f in files)
    if sig == _MSGID_SCAN_CACHE["sig"] and _MSGID_SCAN_CACHE["rows"]:
        return {"ok": True, "files": len(files), "rows": 0, "cached": True}
    rows, errors = [], []
    for f in files:
        try:
            text, fmt = excel_import.parse(f.read_bytes(), f.name)
            parsed, _, _ = stats_provider.parse_rows(text, fmt)
            rows.extend(parsed)
        except Exception as e:
            errors.append({"file": f.name, "error": str(e)[:100]})
    _MSGID_SCAN_CACHE["sig"] = sig
    _MSGID_SCAN_CACHE["rows"] = rows
    if rows:
        sync_msgids(rows)
    return {"ok": True, "files": len(files), "rows": len(rows), "errors": errors}


def _resolve_detail_entry(title, items):
    """本地文章 → msgid 台账条目。归一化精确优先；模糊匹配必须唯一命中，否则不显示按钮。"""
    vals = list(items.values())
    exact = [v for v in vals if _norm(v.get("title")) == _norm(title)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    fuzzy = [v for v in vals if _title_match(title, v.get("title"))]
    return fuzzy[0] if len(fuzzy) == 1 else None


def detail_status(freeze_days=FREEZE_DAYS):
    """文章管理页左侧卡片状态：不联网、不触发 build。

    会顺带幂等同步 msgid 台账（state/detail_index.json + feedback 的 url/msgid），
    但不改任何表现数据、不拉单篇明细。
    发表日优先取 detail_index 台账（total 表的「发表日期」），再取 feedback，最后回退本地写作日。
    """
    import mp_session
    sync_msgids_from_downloads()
    items = _detail_index()["items"]
    idx = _read(paths.index(), [])
    fb = _jsonl("feedback.jsonl")
    sess = mp_session.status()
    today = date.today()
    out = {}
    due_n = frozen_n = no_msgid = refetch_n = 0
    for i, a in enumerate(idx):
        title = str(a.get("title") or "")
        ent = _resolve_detail_entry(title, items)
        msgid = str((ent or {}).get("msgid") or "")
        pub = str((ent or {}).get("date") or "")
        if not pub:
            hit = next((r for r in fb if _title_match(title, r.get("title"))), None)
            pub = str((hit or {}).get("date") or a.get("date") or "")
        days = None
        try:
            days = (today - date.fromisoformat(pub[:10])).days
        except (ValueError, TypeError):
            pass
        due = days is not None and days >= freeze_days
        frozen = bool((ent or {}).get("frozenAt"))
        # 到期即在卡片上给出状态（含「缺链接」提示），不因缺 msgid 而整块隐藏；
        # 真正能否拉取仍由 canFetch / fetch_detail_batch 的 msgid 条件把关。
        visible = bool(due)
        if due:
            due_n += 1
        if frozen:
            frozen_n += 1
        if due and not msgid:
            no_msgid += 1
        # canFetch 不再排除 frozen：满 7 天 + 有 msgid + 登录态有效即可拉取；
        # 未满 7 天（due=False）一律 canFetch=False，前端按钮也不渲染。
        can_fetch = bool(due and msgid and sess.get("valid"))
        can_refetch = bool(can_fetch and frozen)
        if can_refetch:
            refetch_n += 1
        out[str(i)] = {
            "title": title, "feedTitle": str((ent or {}).get("title") or title),
            "publishDate": pub[:10], "days": days, "due": due, "frozen": frozen,
            "frozenAt": str((ent or {}).get("frozenAt") or ""),
            "lastFetchedAt": str((ent or {}).get("lastFetchedAt") or ""),
            "refetchCount": int((ent or {}).get("refetchCount") or 0),
            "msgid": msgid, "visible": visible,
            # 能否拉取必须单独卡 msgid / due / 登录态，不能从 visible 推导
            "canFetch": can_fetch,
            "canRefetch": can_refetch,
            "lastError": str((ent or {}).get("lastError") or ""),
        }
    return {"ok": True, "freezeDays": freeze_days, "today": today.isoformat(),
            "session": sess,
            "summary": {"total": len(idx), "due": due_n, "frozen": frozen_n,
                        # fetchable 仍是「到期且未定格」的缺口数，供默认批量按钮用
                        "fetchable": sum(1 for v in out.values()
                                         if v["canFetch"] and not v["frozen"]),
                        "refetchable": refetch_n,
                        "noMsgid": no_msgid, "noSession": not sess.get("valid")},
            "items": out}


def absorb_single_values(vals, title, date_str="", source="batch", text=""):
    """第五批精确 UPSERT：已知 (title, date) 直接命中，不靠截断文件名猜标题。

    只补互动字段；完成后必须调用 _sync_stats_cache_interaction()，否则管理台 KPI 仍是 0。
    """
    if not vals:
        return {"ok": False, "matched": 0, "updated": 0, "reason": "未识别到单篇明细键值区"}
    rows = _jsonl("feedback.jsonl")
    hits = [i for i, r in enumerate(rows) if _norm(r.get("title")) == _norm(title)]
    if len(hits) != 1:
        hits = [i for i, r in enumerate(rows) if _title_match(title, r.get("title"))]
    if len(hits) != 1:
        return {"ok": False, "matched": len(hits), "updated": 0,
                "reason": f"标题命中 {len(hits)} 篇（需唯一命中），跳过以防错补"}
    r = rows[hits[0]]
    patch = _detail_patch(vals, source)
    if text:
        patch.update(_profile_patch(text, vals))  # 网络入口：同一份 xls 的扩展区块
    if _same_detail_values(r, patch):
        synced = _sync_stats_cache_interaction()
        detail = sync_detail_source(r.get("title"), source)
        return {"ok": True, "matched": 1, "updated": 0, "idempotent": True,
                "title": r.get("title"), "reason": "数据已是最新，无需重复写入",
                "fields": _detail_fields(patch), "statsSynced": synced,
                "detailIndex": detail}
    r.update(patch)
    _save_jsonl("feedback.jsonl", rows)
    synced = _sync_stats_cache_interaction()
    detail = sync_detail_source(r.get("title"), source)
    return {"ok": True, "matched": 1, "updated": 1, "title": r.get("title"),
            "fields": _detail_fields(patch), "statsSynced": synced,
            "detailIndex": detail}


def _detail_mark(msgid, **fields):
    doc = _detail_index()
    cur = doc["items"].get(msgid)
    if not cur:
        return False
    cur.update(fields)
    cur["lastFetchedAt"] = datetime.now().isoformat(timespec="seconds")
    _write_detail_index(doc)
    return True


def mark_frozen(msgid, source="batch"):
    return _detail_mark(msgid, frozenAt=datetime.now().isoformat(timespec="seconds"),
                        detailFrom=source, lastError="")


def mark_refetched(msgid, source="batch"):
    """已定格文章显式重拉成功：刷新定格时间、累计次数，便于审计「重拉过几次」。"""
    doc = _detail_index()
    cur = doc["items"].get(msgid) or {}
    n = int(cur.get("refetchCount") or 0) + 1
    return _detail_mark(msgid, frozenAt=datetime.now().isoformat(timespec="seconds"),
                        detailFrom=source, lastError="", refetchCount=n)


def sync_detail_source(title, source=""):
    """明细写进 feedback 后，同步 detail_index 的溯源与定格状态。

    手动导入单篇明细与批量按钮拉取共用同一条收尾：写 detailFrom /
    lastFetchedAt、清 lastError，让「这篇文章的互动数据从哪来」可追溯。
    只有「发表满 7 天且尚未定格」才写 frozenAt：未满 7 天的数据还会继续
    增长，不该标成定格。离线导入不刷新已有定格时间；网络显式重拉由
    mark_refetched() 单独刷新 frozenAt。找不到唯一 msgid 条目时静默跳过，
    不阻断主流程。
    """
    doc = _detail_index()
    ent = _resolve_detail_entry(title, doc["items"])
    mid = str((ent or {}).get("msgid") or "")
    if not mid:
        return {"ok": False, "reason": "detail_index 无唯一 msgid 条目"}
    cur = doc["items"][mid]
    now = datetime.now().isoformat(timespec="seconds")
    before = (cur.get("detailFrom"), cur.get("frozenAt"), cur.get("lastError"))
    cur["detailFrom"] = str(source or "")
    cur["lastError"] = ""
    frozen = bool(cur.get("frozenAt"))
    if not frozen:
        try:
            days = (date.today() - date.fromisoformat(str(cur.get("date") or "")[:10])).days
        except (ValueError, TypeError):
            days = None
        if days is not None and days >= FREEZE_DAYS:
            cur["frozenAt"] = now
            frozen = True
    after = (cur.get("detailFrom"), cur.get("frozenAt"), cur.get("lastError"))
    if before == after and cur.get("lastFetchedAt"):
        return {"ok": True, "msgid": mid, "frozen": frozen, "unchanged": True}
    cur["lastFetchedAt"] = now
    _write_detail_index(doc)
    return {"ok": True, "msgid": mid, "frozen": frozen}


def mark_detail_error(msgid, err):
    """失败只记诊断，绝不写 frozenAt，下次可重试。"""
    return _detail_mark(msgid, lastError=str(err)[:200])


def _fetch_one(item, session_valid):
    """单篇拉取核心：联网 → 解析 → 精确 UPSERT → 定格 / 重新定格。

    已定格也允许拉取（用户显式点「重拉」或 scope=frozen 批量），成功后刷新
    frozenAt；未满 FREEZE_DAYS 天是唯一不可逾越的门槛。任何失败都不改定格状态。
    """
    import excel_import
    import mp_session
    mid = str(item.get("msgid") or "")
    title = str(item.get("feedTitle") or item.get("title") or "")
    was_frozen = bool(item.get("frozen"))
    if not item.get("due"):
        return {"ok": False, "error": "未满 %d 天，数据尚未收敛，不可拉取" % FREEZE_DAYS}
    if not mid:
        return {"ok": False, "error": "缺少 msgid：请先导入含「内容url」的 total_*.xls"}
    if not session_valid:
        return {"ok": False, "error": "登录态未配置：请先粘贴 Copy as cURL"}
    raw, err = mp_session.download_detail(mid, item.get("publishDate"))
    if err:
        mark_detail_error(mid, err)
        return {"ok": False, "error": err, "msgid": mid}
    try:
        text, _ = excel_import.parse(raw, "数据明细（%s）.xls" % title)
        vals = parse_single_detail(text)
    except Exception as e:
        mark_detail_error(mid, "解析失败: " + str(e))
        return {"ok": False, "error": "下载内容解析失败: " + str(e), "msgid": mid}
    if not vals:
        mark_detail_error(mid, "不是单篇明细键值区")
        return {"ok": False, "error": "下载内容不是单篇数据明细（未识别到键值区）", "msgid": mid}
    res = absorb_single_values(vals, title, item.get("publishDate") or "",
                               source="batch:" + mid, text=text)
    if not res.get("ok"):
        mark_detail_error(mid, res.get("reason") or res.get("error") or "写入失败")
        return {**res, "msgid": mid}
    frozen_at = datetime.now().isoformat(timespec="seconds")
    if was_frozen:
        mark_refetched(mid, "batch:" + mid)
    else:
        mark_frozen(mid, "batch:" + mid)
    return {**res, "msgid": mid, "frozenAt": frozen_at,
            "refetched": was_frozen, "bytes": len(raw)}


def fetch_detail(article_id, dry_run=False):
    """文章管理页单篇卡片按钮入口。"""
    try:
        key = str(int(article_id))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid articleId"}
    st = detail_status()
    item = st["items"].get(key)
    if not item:
        return {"ok": False, "error": "文章不存在"}
    if dry_run:
        return {"ok": True, "dryRun": True, "item": item}
    return _fetch_one(item, st["session"]["valid"])


def fetch_detail_batch(article_ids=None, dry_run=False, pause=1.5,
                       freeze_days=FREEZE_DAYS, scope="missing"):
    """批量拉取：scope=missing(默认，补缺口) / frozen(显式重拉) / all。

    三种 scope 都硬性要求 due（发表满 freeze_days 天）且有 msgid；未满 7 天
    绝不进入 targets。逐篇间隔 pause 秒、不并发，失败不刷新定格。
    """
    if scope not in ("missing", "frozen", "all"):
        return {"ok": False, "error": "invalid scope", "results": []}
    st = detail_status(freeze_days)
    keys = sorted(st["items"].keys(), key=lambda k: int(k))
    if article_ids is not None:
        want = {str(x) for x in article_ids}
        keys = [k for k in keys if k in want]

    def _in_scope(v):
        if not v["due"] or not v["msgid"]:
            return False
        if scope == "all":
            return True
        return bool(v["frozen"]) if scope == "frozen" else not v["frozen"]

    targets = [(k, st["items"][k]) for k in keys if _in_scope(st["items"][k])]
    if dry_run:
        return {"ok": True, "dryRun": True, "total": len(targets), "scope": scope,
                "targets": [{"articleId": int(k), "title": v["title"],
                             "publishDate": v["publishDate"], "days": v["days"],
                             "frozen": v["frozen"],
                             "msgid": v["msgid"]} for k, v in targets],
                "session": st["session"]}
    results = []
    for i, (k, item) in enumerate(targets):
        r = _fetch_one(item, st["session"]["valid"])
        results.append({"articleId": int(k), "title": item["title"], **r})
        err = str(r.get("error") or "")
        if err.startswith("登录态"):
            break                       # 凭证失效，后续必然失败，立即停
        if pause and i < len(targets) - 1:
            time.sleep(pause)
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok == len(results), "total": len(targets), "okCount": ok,
            "failed": len(results) - ok, "results": results, "scope": scope,
            "session": st["session"], "freezeDays": freeze_days}


def ingest_feedback(download_dir=None, max_files=MAX_FILES):
    """扫描下载目录里没处理过的导出文件；只收带标题的行；绝不覆盖 stats_cache.json。

    「渠道×日报」导出（无标题列）会被自然跳过。文件什么时候放进来都行，
    每次 wb_get_strategy / CLI get 都会顺带扫一次，不需要新建定时任务、不卡时间。
    """
    import excel_import
    import stats_provider

    dl = Path(download_dir or (Path.home() / "Downloads"))
    if not dl.is_dir():
        return {"ok": True, "newRows": 0, "skipped": 0, "reason": "下载目录不存在"}
    seen = _read(S() / "feedback_seen.json", {"hashes": [], "errors": []})
    seen["hashes"] = seen.get("hashes") or []
    seen["errors"] = seen.get("errors") or []
    # 单篇明细用独立 seen：这些文件曾被 parse_rows 判为坏文件并记进 feedback_seen.json，
    # 共用一个 seen 会导致永远补不了。分支必须放在原 seen 检查之前。
    single_seen = _read(S() / "single_detail_seen.json", {"hashes": []})
    single_seen["hashes"] = single_seen.get("hashes") or []
    files = [f for f in list(dl.glob("*.xls*")) + list(dl.glob("*.csv"))
             if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES]
    files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:max_files]
    got, failed, skipped = 0, 0, 0
    single_ok, single_updated, single_errors = 0, 0, []

    for f in files:
        try:
            raw = f.read_bytes()
            h = hashlib.sha1(raw).hexdigest()
        except OSError:
            continue
        if is_single_detail(f.name):
            if h in single_seen["hashes"]:
                continue
            try:
                text, _ = excel_import.parse(raw, f.name)
                r = absorb_single_detail(text, f.name)
                if r.get("ok"):
                    single_ok += 1
                    single_updated += int(bool(r.get("updated")))
                    # 只有成功才记 seen：标题没命中/解析失败的文件，换个导出或修好台账后能重试。
                    # 原来是「不管成败都 append」，一个失败会把同批其它文件永久挡在门外。
                    single_seen["hashes"].append(h)
                else:
                    single_errors.append({"file": f.name,
                                          "error": str(r.get("reason") or "未补全")[:120]})
            except Exception as e:
                single_errors.append({"file": f.name, "error": str(e)[:120]})
            continue
        if h in seen["hashes"]:
            continue
        try:
            text, fmt = excel_import.parse(raw, f.name)
            head = text.splitlines()[0] if text else ""
            if "渠道" in head:
                # 「数据趋势概况 / 渠道日报」：同一篇文章按渠道拆成多行，
                # 阅读是渠道子集，混进 feedback 会重复计数并带偏权重。
                seen["hashes"].append(h)
                skipped += 1
                continue
            rows, _, _ = stats_provider.parse_rows(text, fmt)
        except Exception as e:
            seen["errors"].append({"file": f.name, "error": str(e)[:120]})
            seen["hashes"].append(h)        # 坏文件不重复扫；新导出是新 hash，仍会被处理
            failed += 1
            continue
        got += absorb_rows(rows)["added"]   # 只收带标题的行，按稳定标题键 UPSERT
        try:
            sync_msgids(rows)              # 第五批：CLI 路径也记住 msgid/url
        except Exception:
            pass
        seen["hashes"].append(h)

    S().mkdir(parents=True, exist_ok=True)
    (S() / "feedback_seen.json").write_text(
        json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    (S() / "single_detail_seen.json").write_text(
        json.dumps(single_seen, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "newRows": got, "scanned": len(files),
            "skipped": skipped, "failed": failed, "seen": len(seen["hashes"]),
            "singleDetails": {"ok": single_ok, "updated": single_updated,
                              "errors": single_errors}}


# ---------------------------------------------------------------- 策略计算
def _score(r, published=""):
    """相对排序得分：消除「老文章累计阅读天然高」与粉丝量波动。"""
    try:
        read = max(1, int(r.get("read") or 0))
    except (TypeError, ValueError):
        read = 1
    try:
        days = (date.today() - date.fromisoformat(str(published)[:10])).days
        days = max(1, min(days, 60))
    except (ValueError, TypeError):
        days = 7
    def _n(k):
        try:
            return float(r.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    interact = 3 * _n("share") + 2 * _n("fav") + 2 * _n("like")
    reach = math.log1p(read / math.sqrt(days))
    return reach * (1 + 0.6 * interact / math.sqrt(read))


def _shrunk(rows, key, k=6):
    """贝叶斯收缩：小样本向全局均值靠拢，避免单篇爆款把权重带偏。"""
    g = sum(r["_score"] for r in rows) / len(rows)
    out = {}
    for name in {r.get(key) for r in rows if r.get(key)}:
        sub = [r["_score"] for r in rows if r.get(key) == name]
        out[name] = round((sum(sub) + k * g) / (len(sub) + k), 3)
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def _external(signals):
    """外部信号 30 天半衰聚合，冷启动主力。"""
    ext = {}
    for x in signals:
        try:
            age = (date.today() - date.fromisoformat(str(x.get("date", ""))[:10])).days
        except (ValueError, TypeError):
            continue
        try:
            level = float(x.get("engagementLevel", 1))
        except (TypeError, ValueError):
            level = 1.0
        w = 0.5 ** (max(0, age) / 30) * level
        for key in ("titlePattern", "hookType"):
            if x.get(key):
                ext.setdefault(key, {})
                ext[key][x[key]] = round(ext[key].get(x[key], 0) + w, 3)
    return {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in ext.items()}


PRIOR_CONSTRAINTS = [
    "冷启动基线：标题用「具体故障/参数 + 反直觉根因」或「坑点清单 + 可运行解法」，禁用「一文搞懂 / 深入浅出 / 随着…的发展」。",
    "冷启动基线：开头用第一人称真实故障现场（带版本号、报错栈或监控指标），不写概念铺垫。",
    "冷启动基线：正文至少 1 段完整可运行代码 + 1 处设计取舍，结尾给可复用结论或开放问题。",
]


def _constraints(own, ext, n, confidence, samples):
    out = ["本轮选题/标题/开头优先复用下列高权重句式；禁止连续两篇使用同一 titlePattern；"
           "自有样本权重优先于外部信号（外部仅作备选）。"]
    # 收缩后各维度得分往往只差零点几，不足以断言某句式更好。领先不足 0.3 时
    # 不下「优先用 X」的硬指令，避免 AI 把一篇爆款当规律，反而把写法带窄。
    if own.get("titlePattern"):
        items = list(own["titlePattern"].items())
        k, v = items[0]
        lead = v - (items[1][1] if len(items) > 1 else v)
        if lead >= 0.3:
            out.append(f"标题优先用「{k}」句式（自有样本 n={n}，收缩后得分 {v}，领先次优 {lead:.2f}）。")
        else:
            out.append(f"各标题句式收缩后得分接近（{items[-1][1]}~{v}，n={n}），差异不足以推断优劣；"
                       "本轮按支柱轮转选题，禁止连续两篇同句式。")
    if own.get("pillar"):
        items = list(own["pillar"].items())
        k, v = items[0]
        lead = v - (items[1][1] if len(items) > 1 else v)
        if lead >= 0.3:
            out.append(f"同等条件下优先选支柱 {k}（表现得分 {v}，领先次优 {lead:.2f}）；"
                       "仍须遵守四大支柱轮转，不得连续三篇同支柱。")
        else:
            out.append("各支柱表现差异不显著，严格按四大支柱轮转，不因单篇爆款偏向某一支柱。")
    if own.get("hookType") and len(own["hookType"]) > 1:
        vals = list(own["hookType"].values())
        if vals[0] - vals[-1] >= 0.5:
            out.append(f"避免「{list(own['hookType'])[-1]}」开头（自有样本表现最弱，"
                       f"与最优钩子差 {vals[0] - vals[-1]:.2f}）。")
    if ext.get("titlePattern"):
        k, v = next(iter(ext["titlePattern"].items()))
        out.append(f"外部近 30 天高互动样本中「{k}」权重最高（{v}），作为标题备选。")
    if ext.get("hookType"):
        k, v = next(iter(ext["hookType"].items()))
        out.append(f"开头钩子参考外部高频「{k}」（{v}），但必须用本账号的真实故障/第一人称经验切入。")
    if n < 8:
        out.append(f"自有样本仅 {n} 篇，不做统计推断；本轮按外部信号 + 下列冷启动基线 + 第 5 节反 AI 规则执行，"
                   "并把本轮假设写进 wb_record_features 以便日后验证。")
        out.extend(PRIOR_CONSTRAINTS)
    out.append("黏性优先：正文给可复用结论/开放问题，避免口号式收尾；指标以分享、收藏为准，不追绝对阅读量。")
    return out


def build(download_dir=None, do_ingest=True, write=True):
    """产出 data/writing_strategy.json，返回给 Skill 当硬约束用。

    write=False 时纯计算不落盘，供管理台只读面板在策略文件缺失时现算兜底。
    每次 write=True 的 build 同时追加一版 strategy_history.jsonl 快照，并提炼候选已验证规则。
    """
    ing = ingest_feedback(download_dir) if do_ingest else {"ok": True, "newRows": 0}
    # 只读面板（write=False / do_ingest=False）绝不写台账；正式构建前先自愈
    # index 增删导致的 articleId 漂移，保证归因用的是「这篇文章」的特征。
    if write or do_ingest:
        reanchor_features()
    signals = _jsonl("external_signals.jsonl")
    logs = _jsonl("strategy_log.jsonl")
    samples = _samples()
    n = len(samples)
    confidence = "cold" if n < 8 else ("warm" if n < 20 else "strong")
    own = {k: _shrunk(samples, k) for k in ("pillar", "titlePattern", "hookType")} if n >= 8 else {}
    ext = _external(signals)

    n_bf = sum(1 for s in samples if str(s.get("strategyVersion", "")).startswith("backfill"))
    insights = [
        f"已配对 {n} 篇带表现的文章（confidence={confidence}）",
        f"外部信号 {len(signals)} 条，特征台账 {len(logs)} 条",
        "覆盖支柱：" + (", ".join(sorted({s["pillar"] for s in samples})) or "无"),
    ]
    if n_bf:
        insights.append(f"其中 {n_bf} 篇特征为历史规则化回填（非实验台账），权重仅作参考。")

    constraints = _constraints(own, ext, n, confidence, samples)
    out = {
        "version": datetime.now().strftime("%Y%m%d-%H%M"),
        "builtAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sampleSize": n, "confidence": confidence,
        "scoreDef": "log1p(read/√发表天数)×(1+0.6×(3×分享+2×收藏+2×在看)/√read)",
        "ingest": ing,
        "own": own, "external": ext,
        "coverage": {k: len([s for s in samples if s.get(k)]) for k in
                     ("pillar", "titlePattern", "hookType")},
        "constraints": constraints,
        "constraintMeta": _constraint_meta(constraints),
        "insights": insights,
    }
    if write:
        _save("writing_strategy.json", out)
        # 每次 build 追加一版历史快照，驱动「策略演进」时间线 / diff / 回滚
        _append("strategy_history.jsonl", {
            "version": out["version"], "builtAt": out["builtAt"],
            "sampleSize": n, "confidence": confidence,
            "constraints": _constraint_meta(constraints),
            "own": own, "external": ext, "coverage": out["coverage"],
            "scoreDef": out["scoreDef"], "insights": insights,
        })
        # 仅在真实样本充足时提炼候选已验证规则（幂等，不覆盖既有 writing_strategy.json）
        try:
            extract_verified_candidates()
        except Exception:
            pass
    return out


def refresh_strategy_snapshot():
    """数据写入后刷新 writing_strategy.json 的派生字段，保持面板与 Skill 同源。

    导入 total / 单篇明细 / 按钮拉取都会改表现数据，若策略文件还停在上一版，
    管理台读旧权重、wb_get_strategy 现算新权重，两处口径就不一致了。
    这里只重算 sampleSize / own / constraints / insights 等派生字段并写回，
    **不新增策略版本、不追加 history 快照**（版本只在正式 build 时推进），
    也不碰 verified_rules.jsonl，零副作用。
    """
    st = _read(D() / "writing_strategy.json", {})
    if not st:
        return build(do_ingest=False, write=True)
    cur = build(do_ingest=False, write=False)
    out = dict(st)
    for k in ("sampleSize", "confidence", "scoreDef", "own", "external",
              "coverage", "constraints", "constraintMeta", "insights"):
        out[k] = cur[k]
    out["dataSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save("writing_strategy.json", out)
    return {"ok": True, "version": out.get("version"),
            "sampleSize": out["sampleSize"], "dataSyncedAt": out["dataSyncedAt"]}


def data_health():
    """只读数据链路体检：把管理台 KPI / 策略面板 / Skill 会读到的数交叉核一遍。

    纯读、不联网、不触发 build、不写任何台账。issues 为空 = 全链路一致；
    info 里是「本地已写但后台未出数」「后台有但本地文章已删」这类正常孤儿。
    """
    def num(v):
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0

    def key(r):
        return (str(r.get("date") or ""), _norm(r.get("title")))

    issues = []
    idx = paths.read_json(paths.index(), []) or []
    fb = _jsonl("feedback.jsonl")
    logs = _jsonl("strategy_log.jsonl")
    hist = _jsonl("strategy_history.jsonl")
    cache = _read(paths.stats_cache(), {}) or {}
    arts, daily = cache.get("articles") or [], cache.get("daily") or []
    items = _detail_index()["items"]
    st = _read(D() / "writing_strategy.json", {}) or {}
    samples = _samples()

    # 1) 特征台账：articleId 是否与当前 index 位置一致
    pos = {}
    for i, a in enumerate(idx):
        pos.setdefault(_norm(a.get("title")), i)
    drift, orphan_logs = [], []
    for l in logs:
        k = str(l.get("articleKey") or _norm(l.get("title")))
        i = pos.get(k)
        if i is None:
            orphan_logs.append(l.get("title"))
        elif str(l.get("articleId")) != str(i):
            drift.append({"title": l.get("title"),
                          "logged": l.get("articleId"), "actual": i})
    if drift:
        issues.append({"code": "feature-id-drift", "count": len(drift),
                       "message": "特征台账 articleId 与文章清单错位，会串味归因",
                       "detail": drift[:8]})

    # 2) feedback ↔ stats_cache 逐篇数值
    fb_by = {key(r): r for r in fb}
    sc_by = {key(a): a for a in arts}
    only_fb = [f"{r.get('date')} {r.get('title')}" for k, r in fb_by.items() if k not in sc_by]
    only_sc = [f"{a.get('date')} {a.get('title')}" for k, a in sc_by.items() if k not in fb_by]
    value_diffs = []
    for k in set(fb_by) & set(sc_by):
        f, a = fb_by[k], sc_by[k]
        bad = {fld: [num(f.get(fld)), num(a.get(fld))]
               for fld in ("read", "share", "fav", "like")
               if num(f.get(fld)) != num(a.get(fld))}
        if bad:
            value_diffs.append({"date": f.get("date"), "title": f.get("title"), "fields": bad})
    if only_fb or only_sc:
        issues.append({"code": "ledger-coverage", "count": len(only_fb) + len(only_sc),
                       "message": "feedback 与 KPI 快照文章集合不一致",
                       "onlyFeedback": only_fb[:8], "onlyCache": only_sc[:8]})
    if value_diffs:
        issues.append({"code": "ledger-values", "count": len(value_diffs),
                       "message": "feedback 与 KPI 快照逐篇数值不一致",
                       "detail": value_diffs[:8]})

    # 3) daily 是否等于 articles 按日汇总
    agg = {}
    for a in arts:
        d = agg.setdefault(a.get("date"), {"read": 0, "share": 0, "fav": 0, "like": 0})
        for fld in d:
            d[fld] += num(a.get(fld))
    daily_diffs = []
    for d in daily:
        bad = {fld: [num(d.get(fld)), agg.get(d.get("date"), {}).get(fld, 0)]
               for fld in ("read", "share", "fav", "like")
               if num(d.get(fld)) != agg.get(d.get("date"), {}).get(fld, 0)}
        if bad:
            daily_diffs.append({"date": d.get("date"), "fields": bad})
    if daily_diffs:
        issues.append({"code": "daily-aggregate", "count": len(daily_diffs),
                       "message": "日汇总与逐篇合计不一致", "detail": daily_diffs[:8]})

    # 4) detail_index 溯源 / 定格
    missing_msgid = [f"{r.get('date')} {r.get('title')}" for r in fb if not r.get("msgid")]
    source_gaps, frozen_gaps = [], []
    for r in fb:
        if not r.get("detailFrom"):
            continue
        ent = items.get(str(r.get("msgid") or "")) or {}
        if not ent.get("detailFrom"):
            source_gaps.append({"date": r.get("date"), "title": r.get("title")})
    for mid, ent in items.items():
        if ent.get("frozenAt") and not any(
                str(r.get("msgid")) == str(mid) and r.get("detailFrom") for r in fb):
            frozen_gaps.append({"msgid": mid, "title": ent.get("title")})
    if missing_msgid:
        issues.append({"code": "missing-msgid", "count": len(missing_msgid),
                       "message": "反馈行缺 msgid，无法拉取单篇明细",
                       "detail": missing_msgid[:8]})
    if source_gaps:
        issues.append({"code": "detail-source-gap", "count": len(source_gaps),
                       "message": "明细已导入但 detail_index 无溯源",
                       "detail": source_gaps[:8]})
    if frozen_gaps:
        issues.append({"code": "frozen-without-detail", "count": len(frozen_gaps),
                       "message": "已定格但反馈台账无明细来源", "detail": frozen_gaps[:8]})

    # 5) 策略文件新鲜度（面板读文件，Skill 现算，两者必须同口径）
    cur_own = ({k: _shrunk(samples, k) for k in ("pillar", "titlePattern", "hookType")}
               if samples else {})
    stale = []
    if not st:
        stale.append("writing_strategy.json 缺失")
    else:
        if st.get("sampleSize") != len(samples):
            stale.append(f"sampleSize {st.get('sampleSize')} → {len(samples)}")
        if st.get("own") != cur_own:
            stale.append("own 权重已漂移")
    if stale:
        issues.append({"code": "strategy-stale", "count": len(stale),
                       "message": "策略快照落后于当前台账", "detail": stale})
    if hist and st and hist[-1].get("version") != st.get("version"):
        issues.append({"code": "history-mismatch", "count": 1,
                       "message": "策略文件版本与历史快照末版不一致",
                       "detail": [hist[-1].get("version"), st.get("version")]})

    idx_titles = set(pos)
    fb_titles = {_norm(r.get("title")) for r in fb}
    return {
        "ok": not issues,
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "counts": {"articles": len(idx), "features": len(logs), "feedback": len(fb),
                   "cacheArticles": len(arts), "cacheDaily": len(daily),
                   "msgids": len(items), "samples": len(samples),
                   "strategySampleSize": st.get("sampleSize"), "history": len(hist)},
        "issues": issues,
        "info": {
            "orphanFeatures": orphan_logs,
            "localWithoutStats": [{"articleId": i, "date": a.get("date"), "title": a.get("title")}
                                  for i, a in enumerate(idx)
                                  if _norm(a.get("title")) not in fb_titles],
            "statsWithoutLocal": [{"date": r.get("date"), "title": r.get("title")}
                                  for r in fb if _norm(r.get("title")) not in idx_titles],
        },
    }


def repair_data():
    """一键自愈（幂等）：把三处台账按当前真源对齐，再刷新策略快照。

    真源分工不变：stats_cache.json 管 KPI 读数，feedback.jsonl 管表现 + 明细，
    detail_index.json 管 msgid / 定格。只补差、不删历史、不臆造结论。
    """
    a = reanchor_features()
    cache = _read(paths.stats_cache(), {}) or {}
    b = absorb_rows(cache.get("articles") or [])
    c = {"ok": 0, "failed": 0}
    for r in _jsonl("feedback.jsonl"):
        if not r.get("detailFrom"):
            continue
        d = sync_detail_source(r.get("title"), r.get("detailFrom"))
        c["ok" if d.get("ok") else "failed"] += 1
    d = refresh_strategy_snapshot()
    return {"ok": True, "reanchor": a, "feedback": b,
            "detailSources": c, "strategy": d, "health": data_health()}


# ---------------------------------------------------------------- 闭环扩展：样本 / 约束 id / 有效性 / 假设 / 历史 / 回滚 / 规则
def _samples():
    """配对成功的文章样本（含黏性得分与 strategyVersion），供 build / 有效性 / 假设验证共用。"""
    idx = paths.read_json(paths.index(), []) or []
    logs = _jsonl("strategy_log.jsonl")
    feeds = _jsonl("feedback.jsonl")
    samples = []
    for f in feeds:
        meta = _join(f.get("title"), idx, logs)
        if not meta.get("pillar"):
            continue
        # 「发表天数」必须用后台真实发表日（feedback.date），不能用本地写作日
        # 或回填日志日期，否则老文章的相对得分会凭空多算/少算一天。
        published = str(f.get("date") or meta.get("published") or "")
        rec = {**meta, **f}
        rec["published"] = published
        rec["_score"] = _score(f, published)
        samples.append(rec)
    return samples


def _constraint_type(text):
    t = text or ""
    if "冷启动基线" in t:
        return "基线"
    if "标题" in t or "句式" in t:
        return "标题句式"
    if "开头" in t or "钩子" in t:
        return "开头钩子"
    if "支柱" in t:
        return "内容支柱"
    if "外部" in t or "信号" in t:
        return "外部信号"
    if "禁止" in t or "优先" in t or "避免" in t:
        return "硬约束"
    return "通用"


def _constraint_meta(constraints):
    """给每条约束生成稳定 id（内容 hash），便于跨版本历史对照。"""
    out = []
    for t in constraints or []:
        h = hashlib.md5(t.encode("utf-8")).hexdigest()[:8]
        out.append({"id": "c-" + h, "type": _constraint_type(t), "text": t})
    return out


def list_history(limit=12):
    """最近的策略版本快照（倒序，最新在前），供管理台时间线 / diff 渲染。"""
    rows = _jsonl("strategy_history.jsonl")
    rows = rows[-limit:]
    rows.reverse()
    return rows


def effectiveness():
    """版本级约束有效性：按 strategyVersion 分组算平均黏性得分，对比全局均值。

    回填样本（strategyVersion 前缀 backfill-*）不计入版本有效性判定；
    某版本平均得分连续两轮低于全局均值 → 标记降级（SKILL §13 原话落地）。
    """
    samples = _samples()
    if not samples:
        return {"globalMean": 0, "versions": [], "note": "no samples"}
    global_mean = sum(s["_score"] for s in samples) / len(samples)
    groups = {}
    for s in samples:
        v = str(s.get("strategyVersion", ""))
        if v.startswith("backfill"):
            continue                    # 回填样本不参与版本有效性判定
        groups.setdefault(v, []).append(s["_score"])
    # 版本按时间戳字符串排序（YYYYMMDD-HHMM 可字典序比较）
    versions = []
    for v in sorted(groups.keys()):
        sub = groups[v]
        mean = sum(sub) / len(sub)
        versions.append({"version": v, "count": len(sub),
                         "meanScore": round(mean, 3),
                         "globalMean": round(global_mean, 3),
                         "delta": round(mean - global_mean, 3)})
    # 连续两轮低于全局均值 → 降级
    streak = 0
    for ver in versions:
        streak = streak + 1 if ver["delta"] < 0 else 0
        ver["belowStreak"] = streak
        ver["status"] = "down" if streak >= 2 else ("below" if ver["delta"] < 0 else "ok")
    return {"globalMean": round(global_mean, 3), "versions": versions}


def verify_hypotheses():
    """假设验证闭环：每轮 hypothesis 提出后，取之后文章分命中组 / 控制组比对黏性得分。

    命中组 = 特征匹配 target（structured hypothesisTarget，或回退到从假设文本模糊匹配已知维度值）；
    回填样本不参与验证。命中组样本 <3 → ⏳ 待观察；命中组均值高于控制组 → ✅，否则 → ❌。
    """
    logs = _jsonl("strategy_log.jsonl")
    samples = _samples()
    # 位置索引会随 index 增删漂移，优先按稳定标题键取样本得分
    score_by_key = {_norm(s.get("title")): s["_score"]
                    for s in samples if s.get("title")}
    score_by_id = {s["articleId"]: s["_score"] for s in samples if "articleId" in s}
    # 已知维度值集合（用于旧记录无结构化 target 时模糊匹配）
    known = {"titlePattern": set(), "hookType": set(), "pillar": set()}
    for s in samples:
        for k in known:
            if s.get(k):
                known[k].add(s[k])
    results = []
    for log in logs:
        hyp = log.get("hypothesis")
        if not hyp or str(hyp).startswith("（历史回填）"):
            continue
        date = str(log.get("date", "")).strip()
        target = log.get("hypothesisTarget") or {}
        dim, val = target.get("dim"), target.get("val")
        if not dim or not val:
            # 回退：从假设文本里找已知维度值
            for k, vals in known.items():
                hit = next((v for v in vals if v and v in str(hyp)), None)
                if hit:
                    dim, val = k, hit
                    break
        if not dim or not val:
            results.append({"date": date, "hypothesis": hyp, "target": None,
                            "hitMean": None, "ctrlMean": None, "lift": None,
                            "samples": 0, "status": "wait",
                            "note": "无法定位目标特征，待结构化 hypothesisTarget"})
            continue
        after = [l for l in logs if str(l.get("date", "")).strip() >= date]
        hit, ctrl = [], []
        for l in after:
            if str(l.get("strategyVersion", "")).startswith("backfill"):
                continue
            sc = score_by_key.get(_norm(l.get("title")))
            if sc is None:
                sc = score_by_id.get(l.get("articleId"))
            if sc is None:
                continue
            if l.get(dim) == val:
                hit.append(sc)
            else:
                ctrl.append(sc)
        if len(hit) < 3 or not ctrl:
            results.append({"date": date, "hypothesis": hyp,
                            "target": {"dim": dim, "val": val},
                            "hitMean": round(sum(hit) / len(hit), 3) if hit else None,
                            "ctrlMean": round(sum(ctrl) / len(ctrl), 3) if ctrl else None,
                            "lift": None, "samples": len(hit), "status": "wait"})
            continue
        hm, cm = sum(hit) / len(hit), sum(ctrl) / len(ctrl)
        lift = round((hm - cm) / cm * 100, 1) if cm else 0.0
        results.append({"date": date, "hypothesis": hyp,
                        "target": {"dim": dim, "val": val},
                        "hitMean": round(hm, 3), "ctrlMean": round(cm, 3),
                        "lift": lift, "samples": len(hit),
                        "status": "ok" if hm > cm else "no"})
    return results


def rollback(version):
    """把指定版本整体写回 writing_strategy.json（下一轮写作即沿用该版约束）。

    回滚只动 writing_strategy.json，不删 history；constraints 转回字符串数组供 Skill 消费。
    """
    rec = next((r for r in _jsonl("strategy_history.jsonl") if r.get("version") == version), None)
    if not rec:
        return {"ok": False, "error": "版本不存在"}
    cur = _read(D() / "writing_strategy.json", {})
    constraints = [c.get("text") if isinstance(c, dict) else c
                   for c in rec.get("constraints", [])]
    cur.update({
        "version": "R-" + version,
        "builtAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "constraints": constraints,
        "constraintMeta": _constraint_meta(constraints),
        "own": rec.get("own", cur.get("own", {})),
        "external": rec.get("external", cur.get("external", {})),
        "coverage": rec.get("coverage", cur.get("coverage", {})),
        "scoreDef": rec.get("scoreDef", cur.get("scoreDef")),
        "sampleSize": rec.get("sampleSize", cur.get("sampleSize")),
        "confidence": rec.get("confidence", cur.get("confidence")),
        "insights": ["已回滚至版本 " + version + "（" + str(rec.get("builtAt", "")) + "）"] + (rec.get("insights") or []),
        "rolledBackFrom": version,
    })
    _save("writing_strategy.json", cur)
    return {"ok": True, "version": "R-" + version}


def _stable_rule_text(row):
    """由 R1 归因行生成稳定指令文本：不含 n=、得分、领先差值等会过期的数字。

    与 _constraints() 的文案风格对齐，保证 AI 读到的仍是自然指令；证据单独进注释。
    """
    typ, val, direction = row.get("type", ""), row.get("val", ""), row.get("direction", "")
    if direction == "避免":
        return {"标题句式": f"标题避免「{val}」句式",
                "开头钩子": f"开头避免「{val}」",
                "内容支柱": f"避免选支柱 {val}"}.get(typ, f"{typ}避免「{val}」")
    return {"标题句式": f"标题优先用「{val}」句式",
            "开头钩子": f"开头优先用「{val}」",
            "内容支柱": f"同等条件下优先选支柱 {val}"}.get(typ, f"{typ}优先用「{val}」")


def _dedupe_rules(rows):
    """按 directiveId 合并历史重复行：已写入态（written/stale）优先，防止状态回退成 pending。

    台账可能因跨版本重复提炼产生同 id 多行；去重后由 _save_jsonl 全量写回即完成清理。
    """
    groups = {}
    for r in rows:
        did = r.get("directiveId") or ("legacy-" + hashlib.md5(
            str(r.get("rule", "")).encode("utf-8")).hexdigest()[:8])
        r["directiveId"] = r.get("directiveId") or did
        groups.setdefault(did, []).append(r)
    out = []
    for grp in groups.values():
        if len(grp) == 1:
            out.append(grp[0])
            continue
        winner = next((r for r in reversed(grp)
                       if r.get("status") in ("written", "stale")), None)
        out.append(winner if winner is not None else grp[-1])
    return out


def _evidence_comment(r):
    """写进 SKILL.md 的证据注释：稳定锚点 verified:R4-<directiveId> + lift/样本/均值。

    锚点按 directiveId（而非 version）去重，保证同一指令跨版本不重复写入；
    兼容第一批遗留的 verified:R4-<version> 注释。
    """
    ev = r.get("evidence") or {}
    parts = ["verified:R4-" + str(r.get("directiveId") or "legacy"),
             str(r.get("fromVersion") or "")]
    if ev.get("lift") is not None:
        try:
            parts.append("lift %+.1f%%" % float(ev["lift"]))
        except (TypeError, ValueError):
            pass
    if ev.get("nConform") is not None:
        parts.append("命中%s/控制%s" % (ev.get("nConform"), ev.get("nCtrl")))
    if ev.get("conformMean") is not None:
        parts.append("均值%s/%s" % (ev.get("conformMean"), ev.get("ctrlMean")))
    return "<!-- " + " · ".join(parts) + " -->"


def extract_verified_candidates():
    """R4 证据化提炼：以 attribution_matrix()（R1 实测 lift）为**唯一依据**。

    status=ok → pending（带 evidence）；status=no → rejected（建议下线）；
    wait / backfill 不提炼。按 directiveId 稳定去重，已存在行只刷新证据/状态。
    幂等、不覆盖 writing_strategy.json；R1 解析失败 → 空矩阵 → 0 条，安全降级。
    """
    st = _read(D() / "writing_strategy.json", {})
    am = attribution_matrix()
    # 按 directiveId 合并历史重复行；legacy 行补空 evidence（不迁移正文、不删历史）
    rows = _dedupe_rules(_jsonl("verified_rules.jsonl"))
    for r in rows:
        r.setdefault("evidence", {})
    by_id = {r["directiveId"]: r for r in rows}
    now = datetime.now().isoformat(timespec="seconds")
    added = rejected = refreshed = 0
    for row in am["matrix"]:
        if row["status"] not in ("ok", "no"):
            continue                      # 样本不足 / 仅回填，不臆造结论
        lift = row.get("lift") or 0.0
        ev = {
            "lift": row.get("lift"), "nConform": row.get("nConform"),
            "nCtrl": row.get("nCtrl"), "conformMean": row.get("conformMean"),
            "ctrlMean": row.get("ctrlMean"), "fromVersion": st.get("version"),
            "sampledAt": now,
        }
        basis = (f"R1 实测 lift {lift:+.1f}%（命中 {row.get('nConform')} / "
                 f"控制 {row.get('nCtrl')}），"
                 + ("验证为正" if row["status"] == "ok" else "建议下线"))
        did, cur = row["id"], by_id.get(row["id"])
        common = {"rule": _stable_rule_text(row), "type": row.get("type"),
                  "dim": row.get("dim"), "val": row.get("val"),
                  "direction": row.get("direction"), "evidence": ev,
                  "basis": basis, "fromVersion": st.get("version")}
        if row["status"] == "ok":
            if cur and cur.get("status") in ("pending", "written", "stale"):
                cur["evidence"], cur["basis"] = ev, basis
                refreshed += 1
                continue
            if cur:                       # rejected → 重新验证为正，复位 pending
                cur.update({**common, "status": "pending",
                            "rejectedAt": None, "staleAt": None, "waitStreak": 0})
            else:
                rec = {"directiveId": did, **common, "status": "pending"}
                rows.append(rec)
                by_id[did] = rec
            added += 1
        else:                             # no
            if cur and cur.get("status") == "rejected":
                cur["evidence"], cur["basis"] = ev, basis
                refreshed += 1
                continue
            if cur:
                cur.update({**common, "status": "rejected", "rejectedAt": now})
            else:
                rec = {"directiveId": did, **common,
                       "status": "rejected", "rejectedAt": now}
                rows.append(rec)
                by_id[did] = rec
            rejected += 1
    if rows:
        _save_jsonl("verified_rules.jsonl", rows)
    rev = review_rule_lifecycle()
    return {"ok": True, "added": added, "rejected": rejected,
            "refreshed": refreshed, "stale": rev.get("stale", 0),
            "reason": "" if (added or rejected) else "无实测显著指令（样本不足或仅回填）"}


def review_rule_lifecycle():
    """复核已写入规则：最新归因 no → stale；连续 3 轮 wait/backfill → stale；
    重新 ok → 复位 written。只改 verified_rules.jsonl 的 status，绝不触碰 SKILL.md。
    """
    am = attribution_matrix()
    raw = _jsonl("verified_rules.jsonl")
    rows = _dedupe_rules(raw)
    if not rows:
        return {"stale": 0, "revived": 0}
    latest = {r["id"]: r["status"] for r in am.get("matrix", [])}
    now = datetime.now().isoformat(timespec="seconds")
    # 去重本身产生的行数变化也要写回，顺带清理历史重复行
    stale = revived = 0
    dirty = (len(rows) != len(raw))
    for r in rows:
        if r.get("status") not in ("written", "stale"):
            continue
        st = latest.get(r.get("directiveId"))
        if st == "ok":
            r["waitStreak"] = 0
            if r.get("status") == "stale":
                r["status"] = "written"
                r.pop("staleAt", None)
                revived += 1
                dirty += 1
        elif st == "no":
            r["waitStreak"] = 0
            if r.get("status") != "stale":
                r["status"] = "stale"
                r["staleAt"] = now
                stale += 1
                dirty += 1
        elif st in ("wait", "backfill"):
            r["waitStreak"] = int(r.get("waitStreak") or 0) + 1
            if r["waitStreak"] >= 3 and r.get("status") != "stale":
                r["status"] = "stale"
                r["staleAt"] = now
                stale += 1
            dirty += 1
        # st is None（该指令已不在约束并集）→ 不动，避免误标
    if dirty:
        _save_jsonl("verified_rules.jsonl", rows)
    return {"stale": stale, "revived": revived}


def _apply_verified_rules_to_markdown(md, new_rules):
    """把候选规则**追加**进 SKILL.md 的 verified-region 边界内（保留既有规则，绝不覆盖）。"""
    MARKER = "<!-- verified-region-start -->"
    END = "<!-- verified-region-end -->"
    block = "\n".join(new_rules) + "\n"
    if MARKER in md and END in md:
        i2 = md.index(END)
        return md[:i2] + block + md[i2:]
    # 边界缺失时追加独立章节，保证总能写入
    return (md.rstrip() + "\n\n## 已验证规则（R4 自动沉淀，经策略闭环验证为正，可人工修订）\n\n"
            + MARKER + "\n" + block + END + "\n")


def _mark_rules_written(ids):
    """把指定 directiveId 的 pending 规则标记为 written（写台账，不动 SKILL.md）。"""
    rows = _jsonl("verified_rules.jsonl")
    now = datetime.now().isoformat(timespec="seconds")
    changed = False
    for r in rows:
        if r.get("status") == "pending" and str(r.get("directiveId") or "legacy") in ids:
            r["status"] = "written"
            r["writtenAt"] = now
            r.pop("waitStreak", None)
            changed = True
    if changed:
        _save_jsonl("verified_rules.jsonl", rows)


def apply_verified_rules(skill_path):
    """把 pending 规则写入 SKILL.md 已验证规则段（调用方已预览 diff + 二次确认）。

    写入行 = 稳定规则文本 + 证据注释（锚点按 directiveId 去重，兼容第一批 version 注释）。
    绝不删除 / 覆盖 SKILL.md 既有内容；写后把对应 pending 标为 written。
    """
    pending = [r for r in _dedupe_rules(_jsonl("verified_rules.jsonl"))
               if r.get("status") == "pending"]
    if not pending:
        return {"ok": True, "written": 0, "message": "无待写入规则"}
    try:
        md = Path(skill_path).read_text(encoding="utf-8")
    except OSError:
        return {"ok": False, "error": "SKILL.md 不存在: " + str(skill_path)}
    new_rules, touched = [], set()
    for r in pending:
        did = str(r.get("directiveId") or "legacy")
        anchor = "verified:R4-" + did
        legacy_tag = "<!-- verified:R4-" + str(r.get("fromVersion") or "") + " -->"
        if anchor in md or legacy_tag in md or (r.get("rule") and r["rule"] in md):
            touched.add(did)              # 已在 SKILL 中，标 written 即可，不重复写
            continue
        new_rules.append("- " + str(r.get("rule") or "") + "  " + _evidence_comment(r))
        touched.add(did)
    if not new_rules:
        if touched:
            _mark_rules_written(touched)
        return {"ok": True, "written": 0, "message": "候选规则均已写入或无新增"}
    md2 = _apply_verified_rules_to_markdown(md, new_rules)
    Path(skill_path).write_text(md2, encoding="utf-8")
    _mark_rules_written(touched)
    return {"ok": True, "written": len(new_rules)}


def _save_jsonl(name, rows):
    p = D() / name
    with p.open("w", encoding="utf-8") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 闭环扩展：R1 约束归因矩阵 / R7 配对诊断
def _parse_directive(text):
    """从约束文本解析 {dim,val,direction}（仅指令型：标题句式/开头钩子/内容支柱 + 优先/避免/领先）。

    解析依赖约束文本模板（与 _constraints 生成文案对应）；解析不出返回 None，
    不报错、不崩，交由归因矩阵标「无法解析」。若日后 _constraints 改文案，需同步此处。
    """
    t = text or ""
    typ = _constraint_type(t)
    if typ not in ("标题句式", "开头钩子", "内容支柱"):
        return None
    if "优先" in t or "领先" in t:
        direction = "优先"
    elif "避免" in t:
        direction = "避免"
    else:
        return None
    val = None
    m = re.search(r"[「『](.+?)[」』]", t)        # 标题句式 / 开头钩子：值包在「」内
    if m:
        val = m.group(1).strip()
    if not val:
        m2 = re.search(r"支柱\s*([A-Da-d])\b", t)  # 内容支柱：优先选支柱 X（无「」）
        if m2:
            val = m2.group(1).strip().upper()
    if not val:
        return None
    dim = {"标题句式": "titlePattern", "开头钩子": "hookType", "内容支柱": "pillar"}[typ]
    return {"dim": dim, "val": val, "direction": direction, "type": typ}


def attribution_matrix():
    """R1 约束归因矩阵：并集当前 + 历史约束 → 解析指令 → 真实样本拆分 → lift / 显著性。

    回填样本不参与结论（仅 backfill 支撑的指令标 ⚠️ backfill）；真实样本单组 <3 篇标 ⏳ wait。
    纯只读派生，不触发 build、不扫下载目录、不写文件。
    """
    st = _read(D() / "writing_strategy.json", {})
    texts = list(st.get("constraints", []))
    for h in _jsonl("strategy_history.jsonl"):
        for c in (h.get("constraints") or []):
            ct = c.get("text") if isinstance(c, dict) else c
            if ct:
                texts.append(ct)
    seen, uniq = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    parsed = []
    seen_keys = set()
    for t in uniq:
        d = _parse_directive(t)
        if not d:
            continue
        key = (d["dim"], d["val"], d["direction"])   # 同一指令跨版本文案略有差异时也只保留一行
        if key in seen_keys:
            continue
        seen_keys.add(key)
        parsed.append((t, d))
    samples = _samples()
    real = [s for s in samples
            if not str(s.get("strategyVersion", "")).startswith("backfill")]
    has_real = len(real) > 0

    matrix = []
    for text, d in parsed:
        dim, val, direction, typ = d["dim"], d["val"], d["direction"], d["type"]
        if direction == "优先":
            hit_pred = lambda s, v=val: s.get(dim) == v
            ctrl_pred = lambda s, v=val: s.get(dim) != v
        else:  # 避免：命中组 = 不符合该值的样本
            hit_pred = lambda s, v=val: s.get(dim) != v
            ctrl_pred = lambda s, v=val: s.get(dim) == v

        hit_real = [s for s in real if hit_pred(s)]
        ctrl_real = [s for s in real if ctrl_pred(s)]
        hit_bf = [s for s in samples
                  if str(s.get("strategyVersion", "")).startswith("backfill") and hit_pred(s)]
        nH, nC = len(hit_real), len(ctrl_real)
        hm = round(sum(s["_score"] for s in hit_real) / nH, 3) if nH else None
        cm = round(sum(s["_score"] for s in ctrl_real) / nC, 3) if nC else None
        lift = round((hm - cm) / cm * 100, 1) if (hm is not None and cm) else None

        if not has_real or nH < 3 or nC < 3:
            if nH == 0 and nC == 0 and hit_bf:
                status, note = "backfill", "仅回填支撑，不足以下结论"
            else:
                status, note = "wait", "真实样本不足（单组<3），待观察"
        else:
            status, note = ("ok" if lift > 0 else "no"), ""
        matrix.append({
            "id": "a-" + hashlib.md5(f"{dim}|{val}|{direction}".encode("utf-8")).hexdigest()[:8],
            "type": typ, "dim": dim, "val": val, "direction": direction,
            "conformMean": hm, "ctrlMean": cm, "lift": lift,
            "nConform": nH, "nCtrl": nC, "status": status, "note": note,
            "hitArticles": [{"title": s.get("title", ""), "score": s["_score"]}
                            for s in hit_real[:10]],
            "ctrlArticles": [{"title": s.get("title", ""), "score": s["_score"]}
                             for s in ctrl_real[:10]],
        })
    summary = {
        "total": len(matrix),
        "significantPos": sum(1 for m in matrix if m["status"] == "ok"),
        "significantNeg": sum(1 for m in matrix if m["status"] == "no"),
        "backfillOnly": sum(1 for m in matrix if m["status"] == "backfill"),
        "wait": sum(1 for m in matrix if m["status"] == "wait"),
        "hasReal": has_real,
    }
    return {"matrix": matrix, "summary": summary}


def pairing_stats():
    """R7 配对诊断：遍历 feedback.jsonl，用 _samples() 已配对标题集合判定每条导出文章。

    已配对 → paired；未配对 → 列 unpaired 并标注原因（title_mismatch / no_feature）。
    纯派生、零写入；Jaccard 兜底后「配对率」自然提升、未配对清单缩短。
    """
    samples = _samples()
    paired_titles = [s.get("title") for s in samples if s.get("title")]
    paired_ids = {s.get("articleId") for s in samples if "articleId" in s}
    idx = paths.read_json(paths.index(), []) or []
    feeds = _jsonl("feedback.jsonl")
    paired, unpaired, reasons = 0, [], {}
    for f in feeds:
        title = f.get("title") or ""
        if any(_title_match(title, pt) for pt in paired_titles):
            paired += 1
            continue
        matched_art = next((i for i, a in enumerate(idx)
                            if _title_match(title, a.get("title"))), None)
        reason = "no_feature" if (matched_art is not None and matched_art not in paired_ids) \
            else "title_mismatch"
        unpaired.append({"title": title, "date": f.get("date") or "", "reason": reason})
        reasons[reason] = reasons.get(reason, 0) + 1
    # 单篇明细补全率：收藏/在看/点赞只在文章详情单篇导出里，批量 total 导出不含
    missing = [{"title": f.get("title") or "", "date": f.get("date") or ""}
               for f in feeds if not f.get("detailFrom")]
    detail_coverage = {"filled": len(feeds) - len(missing), "total": len(feeds),
                       "missing": missing}
    return {"paired": paired, "total": len(feeds),
            "unpaired": unpaired, "reasons": reasons,
            "detailCoverage": detail_coverage}


# ---------------------------------------------------------------- CLI 回退
def main():
    ap = argparse.ArgumentParser(description="内容运营台 写作策略闭环 CLI 回退")
    ap.add_argument("action", choices=["get", "ingest", "features", "signals",
                                       "backfill", "health", "repair"])
    ap.add_argument("--dir", default=None, help="反馈文件扫描目录，默认 ~/Downloads")
    ap.add_argument("--json", default=None, help="features 对象 / signals 数组的 JSON 文本；传 - 表示从 stdin 读")
    ap.add_argument("--no-ingest", action="store_true", help="get 时跳过下载目录扫描")
    a = ap.parse_args()

    if a.action == "get":
        res = build(a.dir, do_ingest=not a.no_ingest)
    elif a.action == "ingest":
        res = ingest_feedback(a.dir)
    elif a.action == "backfill":
        res = backfill_features()
    elif a.action == "health":
        res = data_health()
    elif a.action == "repair":
        res = repair_data()
    else:
        raw = sys.stdin.read() if a.json == "-" else (a.json or "")
        try:
            payload = json.loads(raw)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"--json 不是合法 JSON: {e}"},
                             ensure_ascii=False))
            return 2
        res = record_features(payload) if a.action == "features" else record_signals(payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())

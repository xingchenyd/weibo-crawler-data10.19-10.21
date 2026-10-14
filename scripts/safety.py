# -*- coding: utf-8 -*-
"""
账号安全护栏：限速、限额、风控熔断、审计日志
================================================================
用于「用登录态抓微博」场景，把账号风险压到最低。设计原则：

1. 速率极低
   默认每次请求间隔 8~15 秒（随机）。这不是为了躲避检测，而是让服务端看到的
   是"一个人在慢慢翻页"，而不是机器扫描。正常用户翻页也不会更快。

2. 双重限额
   单次运行上限 + 自然日上限，两个都记账。超限立即停止，不硬撑、不续跑。
   日额度按自然日累计，跨天自动重置。

3. 熔断（分两类，处置不同）
   · 登录墙（ok:-100 / passport 跳转）：Cookie 没配或已失效 → 配好即可继续。
   · 真风控（432 / 418 / 验证码 / 访问过于频繁）：账号可能已被标记 →
     **立即终止整个任务**，不做指数退避死磕。
   两类都不硬撑，但报错信息会明确指向该做哪一件事，避免"忘了配 Cookie"
   被误报成"命中风控"而去等待 12 小时。

4. 全程审计
   每个请求都写进 request_audit 表（时间、URL、状态、备注），事后可复盘
   到底发了多少请求、有没有异常。这是出问题时唯一能定位原因的凭据。

5. 绝不并发
   单线程顺序请求。并发是触发风控最常见的原因。

配套：真实请求量在跑之前可用 --dry-run 预估，先看数字再决定要不要跑。
"""
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CST = timezone(timedelta(hours=8))

DDL = """
CREATE TABLE IF NOT EXISTS request_audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    date   TEXT NOT NULL,
    url    TEXT,
    status TEXT,
    note   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_date ON request_audit(date);
"""

# ============================================================
# 风控判据 —— 全局唯一出处
# ------------------------------------------------------------
# ⚠️ 这些判据曾经在 event_crawler / multi_board_tracker 里各抄了一份，
#    于是**已经漂移过**：六榜脚本漏了 visit.sina.cn、unusual traffic、
#    verify_code、带空格的 '"ok": -100'，以及 HTTP 511。
#    后果是主脚本认不出的拦截页被当成"返回不是 JSON"或 ok=0 继续跑下去。
#    现在统一放这里，两边都 import 本模块，修一处不会再漏一处。
#    新增判据请只加在这里。
# ============================================================

#: 视为「必须停止」的 HTTP 状态码
THROTTLE_CODES = frozenset({403, 418, 429, 432, 511})

#: 语义模糊的状态码：432 按作者实测多为"需要登录"，但也会与限流一起出现。
#: 处置上仍按"立刻停手"——这一步没有分歧；但**提示里必须两种可能都给**，
#: 否则用户会照着一句"静默 12 小时"去等一个其实重新登录就能解决的问题。
AMBIGUOUS_CODES = frozenset({432})

#: 判定为"搜索类请求"的 URL 特征
SEARCH_URL_MARKER = 'container/getIndex'

#: 连续搜索请求的上限。超过即主动停止。
#: 踩过的坑：原流程是"先把所有关键词搜一遍再抓取"，关键词 130 个时
#: 发现阶段变成连续 130 次搜索请求，实测第 54 次被返回 HTTP 403。
#: 连续几十次"只搜不看"是真人不会有的模式，必须在结构上卡住。
#:
#: 取值 8（原来是 25）：8 的余量足够低——它同时也是"逼你用交替模式"的闸门，
#: 非交替模式下连续搜索数就等于关键词数，8 次即停会直接暴露结构问题，
#: 而不是等到第 54 次被 403 才发现。README 与两个脚本现在都以本值为准。
MAX_CONSECUTIVE_SEARCH = 8

#: ── 「必须停止」的信号分两类，必须分开 ──────────────────────────
#: 拆开的理由：这两种情况的**处置方式完全相反**。
#:   · 登录墙 = Cookie 没配 / 已失效 → 配好就能继续，5 分钟解决；
#:   · 真风控 = 账号已被限流 → 必须停手 12 小时，越试越糟。
#: 合成一类会把"忘了配 Cookie"报成"命中风控"，让人去排查根本不存在的限流问题。
#: 实测（未登录直接请求六榜端点）返回的就是这 90 个字节：
#:   {"ok":-100,"url":"https://weibo.com/login.php?url=..."}
#: 它是**登录哨兵**，不是限流。
#:
#: 注意：两类判据都不能用宽泛的 'verify' —— 微博正文 JSON 里本来就有
#: "verified"/"verified_type" 字段，会导致每次成功请求都误判。

#: 登录墙特征：服务端要求登录态。配好 Cookie 即可恢复，与风控无关。
LOGIN_MARKERS = (
    'passport.weibo.com',          # 登录跳转
    'login.php',                   # ok:-100 响应里给的 url 字段
    '"ok":-100', '"ok": -100',     # 未登录哨兵（两种空格写法都出现过）
    '登录后查看',
)

#: 真·风控特征：限流 / 验证码 / 风险拦截。出现即停手等待冷却。
THROTTLE_MARKERS = (
    'visit.sina.cn',               # 风险拦截跳转
    'unusual traffic',             # 异常流量提示
    '访问过于频繁', '操作过于频繁',
    '请稍后再试',
    '请输入验证码', 'verify_code',
)

#: 两类判据的并集。凡命中即必须停止，只是**原因不同、处置不同**。
BLOCKER_MARKERS = LOGIN_MARKERS + THROTTLE_MARKERS


def is_throttle_status(code):
    """HTTP 状态码是否属于风控信号。"""
    return isinstance(code, int) and code in THROTTLE_CODES


def body_hits_login(body):
    """响应体是否含登录墙特征。命中返回该标记，否则 None。"""
    text = body or ''
    for marker in LOGIN_MARKERS:
        if marker in text:
            return marker
    return None


def body_hits_throttle(body):
    """响应体是否含**真·风控**特征。命中返回该标记，否则 None。

    ⚠️ 只认真风控。登录墙请用 body_hits_login() ——
    两者都会让采集停下，但一个该去配 Cookie，一个该去停手等冷却。
    """
    text = body or ''
    for marker in THROTTLE_MARKERS:
        if marker in text:
            return marker
    return None


def classify_blocker(body):
    """把响应体判为登录墙或风控。

    返回 ('login' | 'throttle', 命中的标记)，都不命中则返回 None。
    登录墙**优先**判定：ok:-100 的响应里同时带 login.php 和 passport 特征，
    但它的本质是"没有登录态"，不是"被限流"。
    """
    marker = body_hits_login(body)
    if marker:
        return 'login', marker
    marker = body_hits_throttle(body)
    if marker:
        return 'throttle', marker
    return None


class SafetyAbort(Exception):
    """触发安全护栏，必须立即停止。"""


class LoginWallAbort(SafetyAbort):
    """熔断，但原因是**登录态失效**（可立即修复，与账号风控无关）。

    单独建类型是为了让循环能给出不同的退出码：
    真风控 → 4（该静默 12 小时）；登录墙 → 6（该去配 Cookie，几分钟就能好）。
    共用一个退出码的话，自动化脚本就分不清"该等"还是"该修"。
    """


class SafetyGuard:

    def __init__(self, db_path, max_per_run=60, max_per_day=300,
                 min_delay=8.0, max_delay=15.0, abort_on_throttle=True,
                 dry_run=False, verbose=True, max_consecutive_search=None):
        self.db_path = db_path
        self.max_per_run = max_per_run
        self.max_per_day = max_per_day
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.abort_on_throttle = abort_on_throttle
        self.dry_run = dry_run
        self.verbose = verbose
        self.max_consecutive_search = (
            MAX_CONSECUTIVE_SEARCH if max_consecutive_search is None
            else max_consecutive_search)

        self.run_count = 0
        self.day_count = 0
        self.consecutive_search = 0
        self.throttle_signals = []
        self.started = datetime.now(CST)

        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(DDL)
        conn.commit()
        today = self.started.strftime('%Y-%m-%d')
        # 只统计 status='request' 的行！
        # 踩过的坑：每个真实请求会写 2 行（1 行 'request' + 1 行状态），
        # 用 COUNT(*) 会把请求数算成约 2 倍，导致日额度被消耗得比预期快一倍多。
        self.day_count = conn.execute(
            "SELECT COUNT(*) FROM request_audit WHERE date=? AND status='request'",
            (today,)).fetchone()[0]
        conn.close()

    # ------------------------------------------------------------ 额度
    def plan(self, n_requests):
        """打印预估用量，供 dry-run 决策。"""
        after_run = self.run_count + n_requests
        after_day = self.day_count + n_requests
        est_lo = n_requests * self.min_delay
        est_hi = n_requests * self.max_delay
        print(f'  [用量预估] 本次计划请求 {n_requests} 次')
        print(f'             本次已用 {self.run_count}/{self.max_per_run}'
              f'，跑完将达 {after_run}')
        print(f'             今日已用 {self.day_count}/{self.max_per_day}'
              f'，跑完将达 {after_day}')
        print(f'             按 {self.min_delay:.0f}~{self.max_delay:.0f}s/次，'
              f'预计耗时 {est_lo/60:.1f}~{est_hi/60:.1f} 分钟')
        over = []
        if after_run > self.max_per_run:
            over.append(f'超出单次上限（{self.max_per_run}）')
        if after_day > self.max_per_day:
            over.append(f'超出今日上限（{self.max_per_day}）')
        if over:
            print(f'             ⚠ 会被拒绝：{"；".join(over)}')
            print(f'             → 请调小 --max-posts / --max-keywords，'
                  f'或改天再跑')
            return False
        print('             额度充足，可以跑')
        return True

    def acquire(self, url):
        """请求前调用：检查额度、限速、以及连续搜索次数。"""
        if self.run_count >= self.max_per_run:
            raise SafetyAbort(
                f'已达单次运行请求上限 {self.max_per_run} 次，主动停止'
                f'（这是保护账号，不是故障）')
        if self.day_count >= self.max_per_day:
            raise SafetyAbort(
                f'已达今日请求上限 {self.max_per_day} 次，主动停止。'
                f'请明天再跑，不要为了赶进度突破上限')

        # 连续搜索次数保护
        if SEARCH_URL_MARKER in (url or ''):
            self.consecutive_search += 1
            if self.consecutive_search > self.max_consecutive_search:
                raise SafetyAbort(
                    f'连续搜索请求已达 {self.consecutive_search} 次（上限 '
                    f'{self.max_consecutive_search}）。连续大量"只搜不看"'
                    f'是真人不会有的模式，极易触发风控。\n'
                    f'      请改用 --interleave（搜索与抓取交替），'
                    f'或减少单轮关键词数量。')
        else:
            self.consecutive_search = 0

        if self.run_count > 0:
            nap = random.uniform(self.min_delay, self.max_delay)
            if self.verbose:
                print(f'      · 限速休眠 {nap:.1f}s')
            time.sleep(nap)
        self.run_count += 1
        self.day_count += 1
        self._audit(url, 'request')

    def record(self, url, status, note=''):
        self._audit(url, status, note)

    def _audit(self, url, status, note=''):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                'INSERT INTO request_audit (ts,date,url,status,note) '
                'VALUES (?,?,?,?,?)',
                (datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
                 datetime.now(CST).strftime('%Y-%m-%d'),
                 (url or '')[:300], str(status), str(note)[:200]))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass                                   # 审计失败不影响主流程

    def check_throttle(self, url, status, body_snippet=''):
        """检查响应是否命中「必须停止」的信号；命中则按配置熔断。

        返回值：命中返回 True，未命中返回 False（abort 开启时命中即抛异常）。
        会区分**登录墙**与**真风控**并给出各自的处置建议——
        这两者的处置完全相反，混报会把人引向错误的排查方向。
        """
        kind = 'throttle'
        hit = None
        hint = ''
        if is_throttle_status(status):
            hit = f'HTTP {status}'
            if status in AMBIGUOUS_CODES:
                hint = (f'\n      注意：HTTP {status} 实测多为"需要登录"而非限流。\n'
                        f'      若你刚刚登录过、或本来就没配 Cookie，'
                        f'先重新扫码登录试一次；\n'
                        f'      确认登录态正常后仍然出现，才按风控静默 12 小时。')
        else:
            cls = classify_blocker(body_snippet)
            if cls:
                kind, marker = cls
                hit = (f'登录哨兵 {marker!r}' if kind == 'login'
                       else f'响应含风控关键词 {marker!r}')
        if not hit:
            return False

        label = '登录墙' if kind == 'login' else '风控信号'
        tag = 'LOGINWALL' if kind == 'login' else 'THROTTLE'
        self.throttle_signals.append((url, hit))
        self._audit(url, f'{tag}:{hit}', label)
        if self.verbose:
            print(f'\n      ⛔ 命中{label}：{hit}')
        if self.abort_on_throttle:
            if kind == 'login':
                raise SafetyAbort(
                    f'检测到登录墙（{hit}）。已立即停止——这**不是风控**。\n'
                    f'      当前请求没有有效登录态：Cookie 未配置或已失效。\n'
                    f'      处置办法（几分钟即可恢复，不必等待）：\n'
                    f'        1) python scripts/login_then_crawl.py 扫码登录\n'
                    f'        2) python scripts/multi_board_tracker.py --probe '
                    f'确认六榜非空\n'
                    f'        3) 确认后再开始长时间采集')
            raise SafetyAbort(
                f'检测到风控信号（{hit}）。已立即停止——继续请求只会加重标记。\n'
                f'      建议：停 12 小时以上再试；期间不要用这个账号做任何自动化操作。\n'
                f'      若反复触发，说明该账号已不适合用于采集，请换小号。{hint}')
        return True

    # ------------------------------------------------------------ 汇总
    def hard_block(self, url, reason):
        """网络层封禁（TLS 握手被拒等）——比 HTTP 风控更严重，立即停止。

        实测：HTTP 403 之后再继续请求，会升级为**全站 TLS 层拒绝**
        （所有微博域名、包括无需登录的接口，都在握手阶段被 Connection closed
        abruptly 掐断），而其他网站正常。这属于出口 IP 被封。
        这种错误绝不能重试——重试只会延长封禁。
        """
        self.throttle_signals.append((url, reason))
        self._audit(url, f'HARDBLOCK:{reason}', '网络层封禁')
        if self.verbose:
            print(f'\n      ⛔⛔ 网络层封禁：{reason}')
        raise SafetyAbort(
            f'检测到网络层封禁（{reason}）。已立即停止。\n'
            f'      这不是接口限流，而是出口被微博在 TLS 阶段拒绝，'
            f'所有微博域名（含无需登录的）都连不上。\n'
            f'      处理办法：\n'
            f'        1) 立刻停止一切微博请求，静默 12~24 小时\n'
            f'        2) 期间不要用这个账号做任何操作，也不要换脚本重试\n'
            f'        3) 恢复后先用 1 次请求试水，正常再逐步放量\n'
            f'        4) 若换用住宅代理改变出口 IP，可显著缩短等待时间\n'
            f'        5) 长期大量采集请用小号，不要用主号')

    def summary(self):
        lines = [
            f'  本次请求数    : {self.run_count}',
            f'  今日累计请求  : {self.day_count} / {self.max_per_day}',
            f'  熔断信号次数  : {len(self.throttle_signals)}（含登录墙）',
            f'  审计表        : {self.db_path} (request_audit)',
        ]
        if self.throttle_signals:
            lines.append('  风控明细:')
            for url, hit in self.throttle_signals[:5]:
                lines.append(f'    - {hit} @ {url[:80]}')
        return '\n'.join(lines)

    def danger_report(self):
        """给用户看的风险提示。"""
        return (
            '  ⚠ 账号风险提示\n'
            '  · 用登录态做自动化采集，任何工具都无法保证账号 100% 不被风控。\n'
            '  · 决定风险高低的是【请求速率】和【行为模式】，不是总量：\n'
            '    慢速长跑比快速冲量安全得多。所以上限可以放宽，\n'
            '    但请务必保持 8~15s 的随机间隔、单线程、不并发。\n'
            '  · 风控熔断照常生效：一见 432 / 需登录跳转 / 验证码就整体停止。\n'
            '  · 出现风控信号后请停手至少 12 小时。\n'
            '  · 仍然建议用不常用的小号。'
        )

# -*- coding: utf-8 -*-
"""护栏离线自检 —— 不发任何网络请求
================================================================================
六榜脚本原先有个缺陷：命中风控后**不会熔断**（只跳出当前队列，睡 30 分钟再开
一轮）。这类问题在实跑之前看不出来——等你从一整夜的日志里发现"风控 37 次"
时，账号已经被标记了。

所以这里用一个假的 curl_cffi 把响应替换成 403 / 拦截页，直接把行为验证掉。
本项目所有 curl_cffi 的导入都是函数内延迟导入，因此无需安装它即可测试。

覆盖 7 项：
  1. 分类榜端点被正确识别为 board 档（限速档位才会生效）
  2. 分类榜请求确实走了 Fetcher  → req_audit 里能查到 kind='board'
  3. HTTP 403           → tick() 抛 SafetyAbort（而不是只跳过当前队列）
  4. 拦截页关键词（200）→ 同样抛 SafetyAbort；且 visit.sina.cn / verify_code
     这两种原先六榜脚本**认不出**的标记现在也能识别
  5. run_loop 收到 SafetyAbort 会退出，不会睡满间隔再开一轮
  6. 六榜全空连续两轮 → 判定登录态异常退出（不空耗整夜）
  7. 回榜时 offboard_upper_ts 被清空；且真风控判据不误伤 "verified" 字段

用法：
  python scripts/selftest_guardrails.py

注意：为提速，本脚本会把 time.sleep 换成空操作（只影响本进程）。
"""
import argparse
import gc
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(ROOT, 'scripts')
sys.path.insert(0, SCRIPT_DIR)

# ============================================================================
# 一、装假的 curl_cffi —— 必须在 import 项目模块之前完成
# ============================================================================
BAND_A = {
    'word': '测试话题A', 'num': 123456, 'realpos': 1,
    'word_scheme': '#测试话题A#', 'category': '测试', 'url': '',
}
BAND_B = {
    'word': '测试话题B', 'num': 234567, 'realpos': 2,
    'word_scheme': '#测试话题B#', 'category': '测试', 'url': '',
}


def _band_body(items):
    return json.dumps({'ok': 1, 'data': {'band_list': items}})


#: url 子串 -> (HTTP 状态码, 响应体)
RESPONSES = {}


class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _fake_get(url, **kwargs):
    for key, (code, body) in RESPONSES.items():
        if key in url:
            return _FakeResp(code, body)
    return _FakeResp(200, '{"ok":1,"data":{"band_list":[]}}')


_fake_req = types.ModuleType('curl_cffi.requests')
_fake_req.get = _fake_get
_fake_pkg = types.ModuleType('curl_cffi')
_fake_pkg.requests = _fake_req
sys.modules['curl_cffi'] = _fake_pkg
sys.modules['curl_cffi.requests'] = _fake_req

import multi_board_tracker as mbt                                  # noqa: E402
from event_crawler import (Blocked, LoginWallBlocked,              # noqa: E402
                           ThrottleBlocked)
from safety import (LoginWallAbort, SafetyAbort, body_hits_login,  # noqa: E402
                    body_hits_throttle, classify_blocker)

# ============================================================================
# 二、隔离环境：临时库 + 屏蔽 sleep
# ============================================================================
TMP = tempfile.mkdtemp(prefix='mbt_selftest_')
mbt.DB_PATH = os.path.join(TMP, 'multi_boards.db')
mbt.COOKIE_FILE = os.path.join(TMP, 'cookie.txt')      # 不存在 → 无 Cookie

_real_sleep = time.sleep
time.sleep = lambda _s: None                           # 仅提速，详见文件头

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f'  {"✓" if cond else "✗"} {name}' + (f'\n      {detail}' if detail else ''))


def make_args(**over):
    a = argparse.Namespace(
        tick=True, status=False, probe=False, probe_again=False,
        per_board=50, no_post=True, no_comment=True,
        max_post_per_round=0, post_candidates=1, min_comments=0,
        max_comment_pages=5, max_req_per_round=400, offboard_misses=2,
        loop_minutes=0, until='', interval=30, wait_network=False,
        max_wait_min=90, quiet=True)
    a.verbose = False
    for k, v in over.items():
        setattr(a, k, v)
    return a


_open_conn = None


_db_seq = [0]


def fresh_db(*boards):
    """新建一个全新的临时库并返回连接。

    这里**不删除旧文件**，而是每次换一个新文件名。
    why：Windows 上只要还有任何一个连接没关（包括 tick() 内部开的），
    os.remove 就会抛 PermissionError [WinError 32]，测试会莫名挂掉。
    换名字比"保证删掉"可靠得多——顺带还能暴露"有没有连接泄漏"。
    """
    global _open_conn
    if _open_conn is not None:
        try:
            _open_conn.close()
        except Exception:                                   # noqa: BLE001
            pass
        _open_conn = None
        gc.collect()
    _db_seq[0] += 1
    mbt.DB_PATH = os.path.join(TMP, f'multi_boards_{_db_seq[0]}.db')
    c = mbt.conn()
    mbt.ensure_boards(c, [(ep, nm, 50) for ep, nm in
                          (boards or mbt.BOARD_ENDPOINTS)])
    _open_conn = c
    return c


def audit_rows(c):
    return c.execute('SELECT kind,status,note FROM req_audit').fetchall()


print('=' * 74)
print('微博热搜追踪爬虫 · 护栏离线自检（不发任何网络请求）')
print('=' * 74)

# ---------------------------------------------------------------- 1. 档位识别
print('\n[1] 分类榜端点是否被识别为 board 档')
check('kind_of 认得 /ajax/statuses/ → board',
      mbt.Fetcher.kind_of('https://weibo.com/ajax/statuses/entertainment')
      == 'board',
      '原先只认 hot_band / hotSearch，分类榜落进 general 档，'
      'DELAY["board"] 从未生效')
check('kind_of 仍是 post 优先（未被 /ajax/statuses/ 抢走）',
      mbt.Fetcher.kind_of('https://m.weibo.cn/statuses/show?id=1') == 'post')
check('kind_of 评论 / 搜索档位不变',
      mbt.Fetcher.kind_of('https://m.weibo.cn/comments/hotflow?id=1') == 'comment'
      and mbt.Fetcher.kind_of(
          'https://m.weibo.cn/api/container/getIndex?x=1') == 'search')

# --------------------------------------------------- 2. 榜单请求进 Fetcher
print('\n[2] 分类榜请求是否走统一取数器（限速 / 计数 / 审计）')
RESPONSES.clear()
RESPONSES['/ajax/statuses/entertainment'] = (200, _band_body([BAND_A, BAND_B]))
RESPONSES['/ajax/statuses/'] = (200, _band_body([]))
c = fresh_db()
st = mbt.tick(make_args())
rows = audit_rows(c)
board_rows = [r for r in rows if r[0] == 'board']
check('六榜请求都写进了 req_audit（kind=board）',
      len(board_rows) == 6,
      f'实际 {len(board_rows)} 条；修复前这条路径一条审计都不写')
check('ThrottleBlocked 是 Blocked 的子类（兼容既有 except 分支）',
      issubclass(ThrottleBlocked, Blocked),
      '不能破坏 event_crawler 里既有的 except Blocked 处理')
check('成功取回的榜数与榜单条数被区分统计',
      st['n_board_ok'] == 6 and st['n_board'] == 2,
      f"n_board_ok={st['n_board_ok']} n_board={st['n_board']}")

# ------------------------------------------------ 3. HTTP 403 → SafetyAbort
print('\n[3] HTTP 403 是否让 tick() 抛 SafetyAbort（核心修复）')
RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (403, '')
fresh_db()
try:
    mbt.tick(make_args())
    check('tick() 抛 SafetyAbort', False, '！没有抛异常 —— 风控被吞掉了')
except SafetyAbort as exc:
    check('tick() 抛 SafetyAbort', True, f'提示信息首行：{str(exc).splitlines()[0]}')
except Exception as exc:                                # noqa: BLE001
    check('tick() 抛 SafetyAbort', False, f'抛的是 {type(exc).__name__}: {exc}')

# ------------------------------------- 4. 拦截页关键词（200）→ SafetyAbort
print('\n[4] 拦截页（HTTP 200）是否被识别 —— 含原先认不出的两种标记')
for marker, note in (('visit.sina.cn', '原先六榜脚本认不出'),
                     ('verify_code', '原先六榜脚本认不出'),
                     ('访问过于频繁', '原有判据'),
                     ('unusual traffic', '原先六榜脚本认不出')):
    RESPONSES.clear()
    RESPONSES['/ajax/statuses/'] = (200, f'<html><body>{marker}</body></html>')
    fresh_db()
    try:
        mbt.tick(make_args())
        check(f'拦截标记 {marker!r} 触发熔断', False, '没抛 SafetyAbort')
    except SafetyAbort:
        check(f'拦截标记 {marker!r} 触发熔断', True, note)

# --------------------------------------- 5. run_loop 收到熔断即退出
print('\n[5] run_loop 是否因 SafetyAbort 退出（不睡满间隔再开一轮）')
RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (403, '')
fresh_db()
calls = {'n': 0}
_real_tick = mbt.tick


def _counting_tick(args, conn_hint=None):
    calls['n'] += 1
    return _real_tick(args, conn_hint)


mbt.tick = _counting_tick
try:
    rc = mbt.run_loop(make_args(interval=0, loop_minutes=10))
finally:
    mbt.tick = _real_tick
check('run_loop 返回熔断退出码 4', rc == 4, f'返回码 {rc}（4 = 安全护栏熔断）')
check('熔断后只跑了 1 轮就退出', calls['n'] == 1,
      f'实际跑了 {calls["n"]} 轮；修复前会一直跑到时间上限')

# ------------------------------------------ 6. 六榜全空 → 停止空跑
print('\n[6] 六榜全空连续两轮是否停止（分类榜无 Cookie 时静默返空）')
RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (200, _band_body([]))
fresh_db()
calls2 = {'n': 0}


def _counting_tick2(args, conn_hint=None):
    calls2['n'] += 1
    return _real_tick(args, conn_hint)


mbt.tick = _counting_tick2
try:
    rc2 = mbt.run_loop(make_args(interval=0, loop_minutes=10))
finally:
    mbt.tick = _real_tick
check('run_loop 返回空榜退出码 5', rc2 == 5, f'返回码 {rc2}（5 = 六榜全空）')
check('空跑 2 轮即停，不会耗一整夜', calls2['n'] == 2, f'实际 {calls2["n"]} 轮')

# -------------------------------- 7. 回榜清上界 + 不误伤 verified
print('\n[7] 回榜清 offboard_upper_ts；风控判据不误伤 verified')
RESPONSES.clear()
RESPONSES['/ajax/statuses/entertainment'] = (200, _band_body([BAND_A]))
RESPONSES['/ajax/statuses/'] = (200, _band_body([]))
c = fresh_db()
mbt.tick(make_args())                                   # 第 1 轮：在榜
tid = c.execute('SELECT topic_id FROM topics').fetchone()[0]
RESPONSES['/ajax/statuses/entertainment'] = (200, _band_body([]))
mbt.tick(make_args())                                   # 第 2 轮：缺席 1 轮
ub = c.execute('SELECT offboard_upper_ts FROM topic_segments '
               'WHERE topic_id=? AND seg_no=1', (tid,)).fetchone()[0]
check('缺席首轮写下榜上界', bool(ub), f'offboard_upper_ts={ub}')
RESPONSES['/ajax/statuses/entertainment'] = (200, _band_body([BAND_A]))
mbt.tick(make_args())                                   # 第 3 轮：又回来了
ub2 = c.execute('SELECT offboard_upper_ts FROM topic_segments '
                'WHERE topic_id=? AND seg_no=1', (tid,)).fetchone()[0]
still = c.execute('SELECT is_active FROM topics WHERE topic_id=?',
                  (tid,)).fetchone()[0]
check('回榜后上界被清空（该段其实还在榜）', still == 1 and ub2 is None,
      f'is_active={still} offboard_upper_ts={ub2}')
check('真风控判据不误伤正文里的 verified',
      body_hits_throttle('{"verified":true,"verified_type":3}') is None)

# -------------------------------- 8. 登录墙 vs 真风控：必须分得开
# 合成一类会把"忘了配 Cookie"报成"命中风控"，而两者的处置完全相反：
# 前者去跑 login_then_crawl.py（几分钟），后者必须静默 12 小时。
print('\n[8] 登录墙与真风控分开识别')

#: 未登录时六榜端点的**真实响应体**（2026-10-05 实测，90 字节）
LOGIN_BODY = ('{"ok":-100,"url":"https://weibo.com/login.php'
              '?url=https%3A%2F%2Fweibo.com%2Fhot%2Fsearch"}')

check('未登录响应被判为登录墙（而非风控）',
      classify_blocker(LOGIN_BODY) == ('login', 'login.php'),
      f'判为 {classify_blocker(LOGIN_BODY)}')
check('verify_code 拦截页仍判为真风控',
      classify_blocker('请输入验证码 verify_code')[0] == 'throttle')
check('登录哨兵不计入 throttle（否则会把未登录误报成风控）',
      body_hits_login(LOGIN_BODY) is not None
      and body_hits_throttle(LOGIN_BODY) is None,
      f'login={body_hits_login(LOGIN_BODY)!r} '
      f'throttle={body_hits_throttle(LOGIN_BODY)!r}')

RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (200, LOGIN_BODY)
c = fresh_db()
f = mbt.Fetcher(cookie='')
f.bind_audit(c)
raised = None
try:
    f.get_json('https://weibo.com/ajax/statuses/entertainment',
               referer='https://weibo.com/hot/search')
except Exception as exc:                                        # noqa: BLE001
    raised = exc
check('取数器把 ok:-100 抛成 LoginWallBlocked',
      type(raised) is LoginWallBlocked,
      f'实际 {type(raised).__name__}: {str(raised)[:56]}')
check('登录墙计入 login_walls 而非 throttle_hits',
      f.login_walls == 1 and f.throttle_hits == 0,
      f'login_walls={f.login_walls} throttle_hits={f.throttle_hits}')

RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (200, LOGIN_BODY)
c = fresh_db()
raised2 = None
try:
    mbt.tick(make_args())
except Exception as exc:                                        # noqa: BLE001
    raised2 = exc
check('tick 遇登录墙抛 LoginWallAbort（可精确区分的类型）',
      type(raised2) is LoginWallAbort, f'实际 {type(raised2).__name__}')
check('LoginWallAbort 继承 SafetyAbort（不破坏既有 except 分支）',
      isinstance(raised2, SafetyAbort))

RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (200, LOGIN_BODY)
c = fresh_db()
rc3 = mbt.run_loop(make_args(interval=0, loop_minutes=10))
check('run_loop 登录墙退出码 6（去配 Cookie，不是等 12 小时）',
      rc3 == 6, f'返回码 {rc3}')

# 对照：真风控仍走 4，说明两类没被搞混
RESPONSES.clear()
RESPONSES['/ajax/statuses/'] = (200, '<html>请输入验证码 verify_code</html>')
c = fresh_db()
rc4 = mbt.run_loop(make_args(interval=0, loop_minutes=10))
check('对照：真风控仍是退出码 4', rc4 == 4, f'返回码 {rc4}')

# ============================================================================
time.sleep = _real_sleep
print('\n' + '=' * 74)
print(f'结果：{len(PASS)} 项通过，{len(FAIL)} 项失败')
if FAIL:
    print('失败项：')
    for n in FAIL:
        print('  ✗', n)
print('=' * 74)
sys.exit(1 if FAIL else 0)

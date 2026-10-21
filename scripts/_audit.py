# -*- coding: utf-8 -*-
"""采集运行全面审查：进程、电源、日志、数据库完整性、乱码、去重。"""
import ctypes
import os
import sqlite3
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
BAD = chr(0xFFFD)  # unicode replacement char（乱码标志）

print('=' * 60)
print('1) 进程存活与电源')
print('=' * 60)
out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe'],
                     capture_output=True).stdout.decode('gbk', errors='replace')
pids = [int(l.split()[1]) for l in out.splitlines()
        if l.startswith('python.exe')]
print('python.exe PID 列表:', pids)
for f, name in [('logs/tracker.pid', 'tracker PID 记录'),
                ('logs/scheduler.lock', '调度器锁 PID')]:
    if os.path.exists(f):
        print(name + ':', open(f).read().strip())


class STATUS(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_byte), ('BatteryFlag', ctypes.c_byte),
                ('BatteryLifePercent', ctypes.c_byte), ('Reserved', ctypes.c_byte),
                ('BatteryLifeTime', ctypes.c_ulong),
                ('BatteryFullLifeTime', ctypes.c_ulong)]


st = STATUS()
ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
print('交流电源:', '已接通 OK' if st.ACLineStatus == 1 else '!! 未接通（电池）',
      '| 电量:', st.BatteryLifePercent, '%')

print()
print('=' * 60)
print('2) 日志新鲜度（距今）')
print('=' * 60)
now = time.time()
for f in ['logs/scheduler.log', 'logs/night_run.log', 'logs/watchdog.log']:
    if os.path.exists(f):
        print(f'{f}: {((now - os.path.getmtime(f)) / 60):.1f} 分钟前最后写入')

print()
print('=' * 60)
print('3) scheduler.log 16:00 之后全部')
print('=' * 60)
for l in open('logs/scheduler.log', encoding='utf-8').read().splitlines():
    if l >= '[2026-10-19 16:00':
        print(l)

print()
print('=' * 60)
print('4) night_run.log：轮次汇总 + 异常关键词 + 尾部')
print('=' * 60)
nl = open('logs/night_run.log', encoding='utf-8').read().splitlines()
print('-- 轮次/汇总行 --')
for l in nl:
    if ('样本' in l and '评论' in l) or '第' in l and '轮' in l and ('完成' in l or '开始' in l):
        print(l)
print('-- 异常关键词扫描 --')
hits = 0
for l in nl:
    if any(k in l for k in ('风控', '熔断', '登录墙', 'Traceback', 'Error', 'ERROR',
                            '失败', '异常', 'BLOCKED', 'HTTP4', 'HTTP5')):
        print('  ' + l)
        hits += 1
if not hits:
    print('  （无异常关键词）')
print('-- 尾部 8 行 --')
for l in nl[-8:]:
    print(l)

print()
print('=' * 60)
print('5) 数据库完整性')
print('=' * 60)
c = sqlite3.connect('data/multi_boards.db')
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")]
print('表:', tables)
for t in tables:
    n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
    print(f'\n表 {t}: {n} 行')
    print(f'  列: {cols}')
    # 乱码 / 空串 / NULL 扫描（全部列）
    for col in cols:
        try:
            bad = c.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" LIKE ?',
                (f'%{BAD}%',)).fetchone()[0]
            empty = c.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" = ""').fetchone()[0]
            nulls = c.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" IS NULL').fetchone()[0]
            if bad or empty or nulls:
                print(f'  !! {col}: 乱码 {bad} / 空串 {empty} / NULL {nulls}')
        except sqlite3.OperationalError:
            pass  # 非文本列比较失败，跳过
    # 时间列按小时分布
    tcol = next((x for x in cols if 'time' in x.lower() or 'created' in x.lower()
                 or x.lower() in ('ts', 'captured_at', 'scraped_at')), None)
    if tcol and n:
        try:
            rows = c.execute(
                f'SELECT "{tcol}", COUNT(*) FROM "{t}" GROUP BY "{tcol}" '
                f'ORDER BY 1 LIMIT 3').fetchall()
            sample = rows[0][0]
            print(f'  时间列 {tcol} 样例: {sample!r}')
            if isinstance(sample, str):
                dist = c.execute(
                    f'SELECT substr("{tcol}",1,13), COUNT(*) FROM "{t}" '
                    f'GROUP BY 1 ORDER BY 1').fetchall()
            else:
                dist = c.execute(
                    f'SELECT datetime("{tcol}","unixepoch","+8 hours") ,COUNT(*) '
                    f'FROM "{t}" GROUP BY 1 ORDER BY 1').fetchall()
            print('  按小时分布:')
            for k, v in dist:
                print(f'    {k}: {v}')
        except Exception as e:
            print('  时间分布查询失败:', e)

print()
print('-- 评论去重 --')
dup = c.execute('SELECT comment_id, COUNT(*) FROM comments '
                'GROUP BY comment_id HAVING COUNT(*)>1').fetchall()
print('重复 comment_id:', len(dup), '条（应为 0）')

print()
print('-- req_audit 响应码分布 --')
try:
    resp = c.execute("SELECT status, COUNT(*) FROM req_audit "
                     "WHERE status != 'request' GROUP BY status "
                     "ORDER BY 2 DESC").fetchall()
    total = c.execute("SELECT COUNT(*) FROM req_audit "
                      "WHERE status='request'").fetchone()[0]
    for s, n in resp[:12]:
        print(f'  {s}: {n}')
    badn = sum(n for s, n in resp
               if s.startswith(('HTTP4', 'HTTP5')) or s in ('BLOCKED', 'RISK'))
    print(f'  总请求 {total}，异常响应 {badn}')
except Exception as e:
    print('  req_audit 查询失败:', e)

print()
print('=' * 60)
print('6) 内容抽样（中文正常性目检）')
print('=' * 60)
try:
    ev_cols = [r[1] for r in c.execute('PRAGMA table_info(events)')] \
        if 'events' in tables else None
    tbl = 'events' if 'events' in tables else tables[0]
    name_col = next((x for x in [r[1] for r in c.execute(f'PRAGMA table_info("{tbl}")')]
                     if 'title' in x or 'name' in x or 'word' in x), None)
    if name_col:
        for (v,) in c.execute(
                f'SELECT "{name_col}" FROM "{tbl}" ORDER BY RANDOM() LIMIT 5'):
            print(' 话题:', v)
    for (v,) in c.execute('SELECT content FROM comments '
                          'ORDER BY RANDOM() LIMIT 3'):
        print(' 评论:', (v or '')[:60])
except Exception as e:
    print(' 抽样失败:', e)
c.close()
print()
print('审查完成')

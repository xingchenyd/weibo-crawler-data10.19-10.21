# -*- coding: utf-8 -*-
"""
三天定时调度器（scheduler.py）
================================================================================
任务：2026-10-19 / 10-20 / 10-21（周一至周三）每天 16:00-22:00 采集微博六分类榜。

设计：
  · 每天 16:00 启动 multi_board_tracker.py（--until 22:00，到点跑完最后一轮退出，
    实际约 22:16 结束），同时启动 watchdog.py 守护（22:40 自动退出）。
  · tracker 进程退出后**立即终止 watchdog** —— 防止 watchdog 在「熔断/登录墙退出」
    后 45 分钟又把采集拉起来（那会违背「命中风控静默 12 小时」的原则）。
  · 采集运行期间持有 SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)：
    阻止系统进入睡眠/Modern Standby（S0 待机会冻结桌面进程），但**不阻止熄屏**
    （不加 ES_DISPLAY_REQUIRED，屏幕可以正常关闭，不影响采集）。
  · 每天 tracker 启动前检查：Cookie 文件存在、交流电源接通（未接电则告警）。
  · 全程日志写 logs/scheduler.log（每条 flush）。三天全部结束后自动退出并写完成标记。

用法：
  python scripts/scheduler.py            # 正常：等到 10-19 16:00 开始
  python scripts/scheduler.py --test     # 先立即跑 2 小时测试，再进入正常调度
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
TRACKER = os.path.join(ROOT, 'scripts', 'multi_board_tracker.py')
WATCHDOG = os.path.join(ROOT, 'scripts', 'watchdog.py')
COOKIE_FILE = os.path.join(ROOT, 'config', 'cookie.txt')
SCHED_LOG = os.path.join(ROOT, 'logs', 'scheduler.log')
RUNLOG = os.path.join(ROOT, 'logs', 'night_run.log')
PIDFILE = os.path.join(ROOT, 'logs', 'tracker.pid')
DONE_MARK = os.path.join(ROOT, 'logs', 'SCHEDULER_DONE')
LOCKFILE = os.path.join(ROOT, 'logs', 'scheduler.lock')

SCHEDULE_DAYS = [(2026, 10, 19), (2026, 10, 20), (2026, 10, 21)]
START_H, START_M = 16, 0          # 每天开始采集时刻
UNTIL_HHMM = '22:00'              # 传给 tracker 的结束时刻
TEST_HOURS = 2                    # --test 模式的测试时长（小时）

TRACKER_ARGS = [
    '--until', UNTIL_HHMM, '--interval', '30', '--per-board', '50',
    '--max-post-per-round', '30', '--max-comment-pages', '5',
    '--max-req-per-round', '400', '--probe-again',
]

# SetThreadExecutionState 常量
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_fh = None


def _pid_alive(pid):
    """检查进程是否仍存活（防双开用）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def acquire_single_instance():
    """防双开：锁文件记录 pid；若锁内 pid 仍存活则拒绝启动。"""
    if os.path.exists(LOCKFILE):
        old = None
        try:
            old = int(open(LOCKFILE, encoding='utf-8').read().strip())
        except Exception:
            old = None
        if old and old != os.getpid() and _pid_alive(old):
            print('[防双开] 检测到已有调度器在运行（PID %d），本次不启动。' % old)
            print('       同一时间只能开一个调度器窗口。')
            print('       若旧窗口已关闭仍出现本提示，删除 logs\\scheduler.lock 后重试。')
            time.sleep(6)
            return False
    with open(LOCKFILE, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCKFILE)
    except OSError:
        pass


def log(msg):
    global _fh
    if _fh is None:
        os.makedirs(os.path.dirname(SCHED_LOG), exist_ok=True)
        _fh = open(SCHED_LOG, 'a', encoding='utf-8')
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    _fh.write(line + '\n')
    _fh.flush()


def keepawake_on():
    """阻止系统睡眠（允许熄屏）。Modern Standby 机器熄屏后桌面进程会被
    桌面活动调节器冻结，ES_SYSTEM_REQUIRED 可阻止进入该状态。"""
    r = ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    log(f'防睡眠开启（允许熄屏）：SetThreadExecutionState -> {r}（0=失败）')


def keepawake_off():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    log('防睡眠已解除，恢复系统默认电源策略')


def ac_power_online():
    """返回 (是否接通交流电, 电池百分比)。读取失败返回 (True, -1) 以免误停。"""
    class STATUS(ctypes.Structure):
        _fields_ = [('ACLineStatus', ctypes.c_byte),
                    ('BatteryFlag', ctypes.c_byte),
                    ('BatteryLifePercent', ctypes.c_byte),
                    ('Reserved', ctypes.c_byte),
                    ('BatteryLifeTime', ctypes.c_ulong),
                    ('BatteryFullLifeTime', ctypes.c_ulong)]
    st = STATUS()
    ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
    if not ok:
        return True, -1
    return st.ACLineStatus == 1, st.BatteryLifePercent


def write_pidfile(pid):
    try:
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, 'w', encoding='utf-8') as f:
            f.write(str(pid))
    except Exception:
        pass


def kill_pid(pid):
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                       capture_output=True, text=True, timeout=20)
    except Exception:
        pass


EXIT_CODE_HINT = {
    0: '正常跑完（含最后一轮）',
    2: '六个分类榜都取不到 —— 检查网络/代理/Cookie',
    3: '缺少 config/cookie.txt 或等待网络超时',
    4: '命中风控已熔断 —— 静默 12 小时以上，期间不要重跑',
    5: '六榜连续两轮全空 —— Cookie 可能已失效，跑 --probe 确认',
    6: '撞到登录墙 —— 重新配置 Cookie（不必等待冷却）',
}


def start_tracker(until_hhmm):
    """启动采集主进程，返回 Popen。"""
    os.makedirs(os.path.dirname(RUNLOG), exist_ok=True)
    fh = open(RUNLOG, 'a', encoding='utf-8')
    fh.write(f'\n\n===== scheduler 于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} '
             f'启动采集（until {until_hhmm}）=====\n')
    fh.flush()
    args = [PY, '-u', TRACKER, '--until', until_hhmm] + TRACKER_ARGS[2:]
    # TRACKER_ARGS[0:2] 是 --until 22:00，上面已按参数单独传，故跳过前两项
    proc = subprocess.Popen(args, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    write_pidfile(proc.pid)
    fh.close()
    return proc


def start_watchdog():
    fh = open(os.path.join(ROOT, 'logs', 'watchdog_stdout.log'), 'a', encoding='utf-8')
    proc = subprocess.Popen([PY, '-u', WATCHDOG], cwd=ROOT,
                            stdout=fh, stderr=subprocess.STDOUT)
    fh.close()
    return proc


def preflight():
    """启动采集前的检查。返回 False 表示不应该启动。"""
    if not os.path.exists(COOKIE_FILE):
        log('❌ config/cookie.txt 不存在 —— 分类榜必须有登录 Cookie，本轮不启动。')
        return False
    ck = ''
    with open(COOKIE_FILE, encoding='utf-8', errors='ignore') as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                ck = ln
                break
    if not ck:
        log('❌ config/cookie.txt 里没有有效 Cookie 行，本轮不启动。')
        return False
    on_ac, pct = ac_power_online()
    if not on_ac:
        log(f'⚠️ 警告：未接通交流电源（电量 {pct}%）。电池模式下熄屏 10 分钟'
            f'系统会睡眠，采集将中断。请立即插上电源！')
    return True


def run_window(until_hhmm, with_watchdog=True):
    """跑一个采集窗口直到 tracker 进程退出。"""
    if not preflight():
        return None
    keepawake_on()
    try:
        proc = start_tracker(until_hhmm)
        log(f'采集已启动 PID={proc.pid}（--until {until_hhmm}）')
        wd = start_watchdog() if with_watchdog else None
        if wd:
            log(f'守护已启动 PID={wd.pid}（tracker 退出后会立即终止守护）')
        rc = proc.wait()
        log(f'采集进程退出，退出码 {rc}：{EXIT_CODE_HINT.get(rc, "未知退出码")}')
        if wd is not None:
            try:
                wd.wait(timeout=5)
            except Exception:
                pass
            kill_pid(wd.pid)
            log('守护进程已终止（防止熔断后被误重启）')
        return rc
    finally:
        keepawake_off()


def wait_until(target):
    """阻塞等到 target 时刻，期间每 10 分钟写一条心跳日志。"""
    while True:
        now = datetime.now()
        remain = (target - now).total_seconds()
        if remain <= 0:
            return
        if remain > 600:
            log(f'等待中：距 {target.strftime("%m-%d %H:%M")} 还有 {remain/60:.0f} 分钟')
            time.sleep(min(600, remain - 60))
        else:
            time.sleep(min(60, max(1, remain)))


def next_day_target(y, m, d):
    return datetime(y, m, d, START_H, START_M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true',
                    help=f'先立即跑 {TEST_HOURS} 小时测试，再进入正常调度')
    args = ap.parse_args()

    if not acquire_single_instance():
        return 0

    log('=' * 64)
    log('三天调度器启动：10-19 / 10-20 / 10-21 每天 16:00 开始，22:00 收工')
    log(f'  python: {PY}')
    log(f'  cookie: {COOKIE_FILE}（存在={os.path.exists(COOKIE_FILE)}）')
    log(f'  本机电源：交流永不睡眠已确认；采集期间另有 SetThreadExecutionState 双保险')

    if args.test:
        end_h = (datetime.now() + timedelta(hours=TEST_HOURS)).strftime('%H:%M')
        log(f'—— 测试模式：立即开始 {TEST_HOURS} 小时测试（至 {end_h}）——')
        run_window(end_h, with_watchdog=False)
        log('—— 测试结束，之后进入正常三天调度 ——')

    for (y, m, d) in SCHEDULE_DAYS:
        target = next_day_target(y, m, d)
        # 该天 22:00 已过（窗口整个结束）→ 跳过，不补跑（避免深夜意外连跑）
        if datetime.now() > target + timedelta(hours=6):
            log(f'跳过 {y}-{m:02d}-{d:02d}：该天采集窗口已结束')
            continue
        wait_until(target)
        log(f'>>> {y}-{m:02d}-{d:02d} 到达 {START_H:02d}:{START_M:02d}，开始当日采集')
        rc = run_window(UNTIL_HHMM, with_watchdog=True)
        log(f'<<< {y}-{m:02d}-{d:02d} 当日采集结束，退出码 {rc}')

    with open(DONE_MARK, 'w', encoding='utf-8') as f:
        f.write(f'三天调度全部完成 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
    log('✅ 三天调度全部完成，调度器退出。')
    release_lock()
    if _fh:
        _fh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
扫码登录导 Cookie（export_cookie_scan.py）
================================================================================
用系统 Edge（channel='msedge'）弹出一个独立窗口打开微博登录页，
等待用户扫码登录；检测到登录字段（SUB/SUBP/SSOLoginState）后自动
提取 Cookie 并写入 config/cookie.txt，然后关闭浏览器退出。

设计要点：
  · 不 taskkill 任何现有浏览器（不动用户正在用的 Chrome/Edge）
  · 不启动任何采集 —— 只导出 Cookie + 探榜验证
  · 检测轮询间隔 5 秒，最长等 10 分钟
  · Cookie 拼装规则与 login_then_crawl.build_header 一致：
    只保留 weibo/sina 域、同名去重、移动端域优先、跳过非 ASCII 值
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from playwright.sync_api import sync_playwright          # noqa: E402

COOKIE_OUT = os.path.join(ROOT, 'config', 'cookie.txt')
LOGIN_MARKERS = ('SUB', 'SUBP', 'SSOLoginState')
TIMEOUT = 600                                            # 最长等 10 分钟


def build_header(cookies):
    """按浏览器规则拼 Cookie 头，与 login_then_crawl.build_header 同规则。"""
    def rank(dom):
        if dom in ('.weibo.cn', 'm.weibo.cn'):
            return 0
        if dom in ('.weibo.com', 'weibo.com'):
            return 1
        return 2 if 'weibo' in (dom or '') else 3
    keep, seen = [], set()
    for c in sorted(cookies, key=lambda c: rank(c.get('domain') or '')):
        dom = c.get('domain') or ''
        if not any(k in dom for k in ('weibo', 'sina')):
            continue
        name = c.get('name')
        if not name or name in seen:
            continue
        try:
            (c.get('value') or '').encode('ascii')
        except UnicodeEncodeError:
            continue
        seen.add(name)
        keep.append(f"{name}={c.get('value', '')}")
    return '; '.join(keep)


def main():
    print('=' * 64, flush=True)
    print('微博扫码登录：即将弹出 Edge 窗口，请在窗口里扫码登录 weibo.com', flush=True)
    print(f'检测到登录后自动导出 Cookie（最长等待 {TIMEOUT}s）', flush=True)
    print('=' * 64, flush=True)

    os.makedirs(os.path.dirname(COOKIE_OUT), exist_ok=True)
    header = None

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='msedge', headless=False,
                                        args=['--start-maximized'])
        except Exception as e:
            print(f'[失败] 无法启动 Edge（{e}），尝试 Chrome…', flush=True)
            try:
                browser = p.chromium.launch(channel='chrome', headless=False)
            except Exception as e2:
                print(f'[失败] Chrome 也不可用：{e2}', flush=True)
                return 1
        context = browser.new_context(no_viewport=True)
        page = context.new_page()
        page.goto('https://weibo.com/login.php', wait_until='domcontentloaded',
                  timeout=60000)
        print('[等待] 浏览器已打开，请扫码登录…', flush=True)

        t0 = time.time()
        announced = False
        while time.time() - t0 < TIMEOUT:
            time.sleep(5)
            try:
                cookies = context.cookies()
            except Exception:
                continue
            names = {c.get('name') for c in cookies}
            # SSOLoginState 是真正登录的标志；SUB/SUBP 游客态也有，不可作为判据
            hit = [m for m in ('SSOLoginState', 'ALF') if m in names]
            elapsed = int(time.time() - t0)
            if hit:
                h = build_header(cookies)
                if h and len(h) > 50:
                    print(f'[{elapsed}s] ✓ 检测到登录字段 {hit}，Cookie {len(h)} 字符', flush=True)
                    # 关键：访问一次移动端 m.weibo.cn，触发 wap SSO 跨端登录，
                    # 否则搜索接口（m.weibo.cn）仍会判未登录
                    print('[同步] 访问 m.weibo.cn 触发移动端 SSO 登录…', flush=True)
                    try:
                        page.goto('https://m.weibo.cn/', wait_until='domcontentloaded',
                                  timeout=45000)
                        time.sleep(6)
                        cookies = context.cookies()
                        h2 = build_header(cookies)
                        if len(h2) >= len(h):
                            h = h2
                        names2 = {c.get('name') for c in cookies}
                        print(f'       同步后 Cookie {len(h)} 字符，'
                              f'域字段含 .weibo.cn 的登录态：'
                              f'{[c.get("domain") for c in cookies if c.get("name") in ("SUB", "ALF")]}',
                              flush=True)
                    except Exception as e:
                        print(f'       移动端同步失败（{e}），按现有 Cookie 继续', flush=True)
                    header = h
                    break
            if not announced:
                print('[提示] 当前是未登录状态。请在弹出的窗口里扫码/输入账号登录微博。', flush=True)
                announced = True
            if elapsed % 60 < 5:
                print(f'[{elapsed}s] 等待登录中… 当前 {len(cookies)} 条 Cookie', flush=True)
        try:
            browser.close()
        except Exception:
            pass

    if not header:
        print('✗ 超时未检测到登录。', flush=True)
        return 2

    with open(COOKIE_OUT, 'w', encoding='utf-8') as fh:
        fh.write(header)
    print(f'✓ Cookie 已写入 {COOKIE_OUT}', flush=True)
    print('  可运行: python scripts/multi_board_tracker.py --probe 验证六榜', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

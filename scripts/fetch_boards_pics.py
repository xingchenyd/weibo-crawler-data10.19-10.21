# -*- coding: utf-8 -*-
"""
给六榜数据集补下载事件配图
================================================================================
背景：export_boards.py 会产出表格，但**不下载图片**；而 export_tracker.py 虽然
有下载图片的能力，读的却是另一个库（data/hot_tracker.db）。

本脚本专门补这一环：从 data/multi_boards.db 的 topic_posts.pics 取图 URL，
下载到 dataset_boards/六榜数据集/事件图/，并把「图片」列回填到 事件表.xlsx。

命名规则与作者既有约定一致：{序号}-{第几张}.jpg
  序号 = 事件表.xlsx 里的「序号」列
  例如 3-1.jpg 表示第 3 条事件的第 1 张图；表格「图片」列填 P3

用法：
  python -u scripts/fetch_boards_pics.py              # 只下缺的
  python -u scripts/fetch_boards_pics.py --max-per-event 5
  python -u scripts/fetch_boards_pics.py --refresh    # 清掉重下
"""
import argparse
import os
import sqlite3
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'multi_boards.db')
OUTDIR = os.path.join(ROOT, 'dataset_boards', '六榜数据集')
IMGDIR = os.path.join(OUTDIR, '事件图')
XLSX = os.path.join(OUTDIR, '事件表.xlsx')

#: 图片 URL 的分隔符 —— 作者用的是竖线（不是逗号）
SEP = '|'


def split_pics(raw):
    if not raw:
        return []
    parts = []
    for chunk in str(raw).split(SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 容错：万一混了逗号分隔
        for u in chunk.split(','):
            u = u.strip()
            if u.startswith('http'):
                parts.append(u)
    return parts


def to_large(url):
    """orj360 / thumbnail 是小图，换 large 拿清晰版。失败则保留原 URL。

    新浪 CDN 的路径形如 .../orj360/xxxx.jpg，把段名换成 large 即可。
    注意：必须是 wx*.sinaimg.cn 域，别的域直接原样返回。
    """
    if 'sinaimg.cn' not in url:
        return url
    for small in ('/orj360/', '/thumbnail/', '/orj50/', '/small/'):
        if small in url:
            return url.replace(small, '/large/')
    return url


def download(url, dst):
    """下载单张图。返回 True/False。"""
    from curl_cffi import requests as creq
    for attempt in range(3):
        try:
            r = creq.get(url, impersonate='chrome', timeout=25,
                         headers={'Referer': 'https://weibo.com/',
                                  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; '
                                                'Win64; x64) AppleWebKit/537.36 '
                                                '(KHTML, like Gecko) '
                                                'Chrome/120.0.0.0 Safari/537.36'})
            if r.status_code == 200 and len(r.content) > 800:
                with open(dst, 'wb') as fh:
                    fh.write(r.content)
                return True
        except Exception:                          # noqa: BLE001
            pass
        time.sleep(1.5 * (attempt + 1))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-per-event', type=int, default=5,
                    help='每个事件最多下几张（默认 5，与老师要求一致）')
    ap.add_argument('--refresh', action='store_true',
                    help='清掉 事件图 目录重新下载')
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f'!! 找不到 {DB}')
        return 1
    if not os.path.exists(XLSX):
        print(f'!! 找不到 {XLSX} —— 请先跑 scripts/export_boards.py')
        return 1

    import shutil
    import openpyxl

    if args.refresh and os.path.isdir(IMGDIR):
        shutil.rmtree(IMGDIR, ignore_errors=True)
    os.makedirs(IMGDIR, exist_ok=True)

    # ---------- 1. 找出「有正文且带图」的话题对应的图 URL ----------
    c = sqlite3.connect(DB)
    rows = c.execute(
        'SELECT word, pics FROM topic_posts '
        "WHERE pics IS NOT NULL AND pics != '' ORDER BY board_id, word"
    ).fetchall()

    by_word = {}
    for word, pics in rows:
        urls = split_pics(pics)
        if not urls:
            continue
        cur = by_word.setdefault(word, [])
        for u in urls:
            if u not in cur:
                cur.append(u)

    print(f'带图的话题：{len(by_word)} 个，共 '
          f'{sum(len(v) for v in by_word.values())} 张候选图')

    # ---------- 2. 读 事件表.xlsx 拿「序号 → 事件名」映射 ----------
    wb = openpyxl.load_workbook(XLSX)
    ws = wb.active
    hdr = [c0.value for c0 in ws[1]]
    try:
        ci_no = hdr.index('序号')
        ci_word = hdr.index('事件名')
    except ValueError:
        print('!! 事件表.xlsx 表头不符合预期，缺「序号」或「事件名」列')
        return 1
    has_pic_col = '图片' in hdr
    ci_pic = hdr.index('图片') if has_pic_col else None
    if not has_pic_col:
        ws.cell(row=1, column=len(hdr) + 1, value='图片')
        ci_pic = len(hdr)
        print('（事件表.xlsx 原无「图片」列，已追加）')

    # ---------- 3. 逐事件下载 ----------
    n_ok, n_fail, n_skip, n_nopic = 0, 0, 0, 0
    for r in range(2, ws.max_row + 1):
        no = ws.cell(row=r, column=ci_no + 1).value
        word = ws.cell(row=r, column=ci_word + 1).value
        if not no or not word:
            continue
        urls = by_word.get(str(word).strip(), [])
        if not urls:
            n_nopic += 1
            continue

        saved = 0
        for k, u in enumerate(urls[:args.max_per_event], start=1):
            dst = os.path.join(IMGDIR, f'{no}-{k}.jpg')
            if os.path.exists(dst) and os.path.getsize(dst) > 800:
                n_skip += 1
                saved += 1
                continue
            if download(to_large(u), dst):
                n_ok += 1
                saved += 1
                print(f'  ✓ {no}-{k}.jpg  ← {str(word)[:26]}')
            else:
                n_fail += 1
                print(f'  ✗ {no}-{k}.jpg  失败  {u[:60]}')
            time.sleep(0.6)                 # 对 CDN 也要客气

        if saved:
            ws.cell(row=r, column=ci_pic + 1, value=f'P{no}')

    wb.save(XLSX)

    print()
    print('=' * 66)
    print(f'  新下载 {n_ok} 张 · 已存在跳过 {n_skip} 张 · 失败 {n_fail} 张')
    print(f'  无图事件 {n_nopic} 个（该帖子本身没配图）')
    print(f'  图片目录：{IMGDIR}')
    print(f'  表格已更新：{XLSX}')
    print('=' * 66)
    return 0


if __name__ == '__main__':
    sys.exit(main())

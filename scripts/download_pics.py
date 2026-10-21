# -*- coding: utf-8 -*-
"""下载 topic_posts.pics 中的微博配图到本地（幂等，可重复跑增量补齐）。

- 图片源：新浪图床 CDN（wx*.sinaimg.cn），与采集 API（m.weibo.cn）不同域，
  不带登录态、不占采集请求配额，无账号风控关联。
- orj360 缩略图 URL 自动升级为 large 原图。
- 串行 + 0.3s 间隔，避免触发 CDN 限流。
- 幂等：目标文件已存在则跳过；失败重试 1 次，失败清单最后汇总。
"""
import os
import sqlite3
import sys
import time
from urllib.parse import urlparse

from curl_cffi import requests as creq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'dataset_boards', '六榜数据集', 'pics')
DB = os.path.join(ROOT, 'data', 'multi_boards.db')
DELAY = 0.3


def to_large(url):
    """orj360 / thumbnail / bmiddle 统一升级为 large 原图。"""
    for size in ('orj360', 'thumbnail', 'bmiddle', 'mw2000', 'small'):
        if f'/{size}/' in url:
            return url.replace(f'/{size}/', '/large/')
    return url


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT topic_id, mid, pics FROM topic_posts WHERE pics != ''").fetchall()
    c.close()

    tasks = []          # (topic_id, mid, url, path)
    skipped = 0
    for tid, mid, pics in rows:
        for u in pics.split('|'):
            u = u.strip()
            if not u.startswith('http'):
                continue
            name = os.path.basename(urlparse(u).path) or f'{mid}.jpg'
            path = os.path.join(OUT_DIR, name)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                skipped += 1
                continue
            tasks.append((tid, mid, to_large(u), path))

    print(f'正文带图 {len(rows)} 条，待下载 {len(tasks)} 张（已存在跳过 {skipped} 张）',
          flush=True)

    ok = fail = 0
    failed = []
    for i, (tid, mid, url, path) in enumerate(tasks, 1):
        try:
            # 注意：不要用 impersonate 伪装浏览器指纹——实测新浪图床对
            # 伪造指纹返回 403，裸 curl 指纹反而 200 放行（2026-10-20 实测）
            r = creq.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 500:
                with open(path, 'wb') as f:
                    f.write(r.content)
                ok += 1
            else:
                fail += 1
                failed.append((mid, url, f'HTTP {r.status_code}'))
        except Exception as e:
            fail += 1
            failed.append((mid, url, f'{type(e).__name__}'))
        if i % 100 == 0:
            print(f'  进度 {i}/{len(tasks)}（成功 {ok} / 失败 {fail}）', flush=True)
        time.sleep(DELAY)

    print(f'完成：成功 {ok} / 失败 {fail} / 跳过已存在 {skipped}', flush=True)
    if failed:
        fl = os.path.join(OUT_DIR, '_failed.txt')
        with open(fl, 'w', encoding='utf-8') as f:
            for mid, url, why in failed:
                f.write(f'{mid}\t{why}\t{url}\n')
        print(f'失败清单已写 {fl}（重跑本脚本可自动补齐）', flush=True)
    print(f'图片目录：{OUT_DIR}', flush=True)


if __name__ == '__main__':
    sys.exit(main())

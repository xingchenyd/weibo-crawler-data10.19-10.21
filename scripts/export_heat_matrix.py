# -*- coding: utf-8 -*-
"""
导出「热度变化表」（矩阵/横表形态）
================================================================================
现有 热度明细.xlsx 是**长表**（一话题 × 一时间点 = 一行），便于透视但不好横向对比。
本脚本产出**横表**：行 = 话题，列 = 时间点，单元格 = 热度值。

两个 Sheet：
  1.「按采样时刻」  列 = 40 个真实采样时刻（10-05 16:23、10-05 16:33 …）
  2.「按上榜后分钟」列 = 上榜后 0/30/60/…/360 分钟（每 30 分钟一格，取最近样本）

用法：
  python -u scripts/export_heat_matrix.py
输出：
  dataset_boards/六榜数据集/热度变化表.xlsx
"""
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'multi_boards.db')
OUT = os.path.join(ROOT, 'dataset_boards', '六榜数据集')

FMT = '%Y-%m-%d %H:%M:%S'
#: 「按上榜后分钟」这一页的时间刻度 —— 每 30 分钟一格
REL_STEPS = list(range(0, 361, 30))
#: 取样本的容差（分钟）—— 半格。我们的实际采样间隔是 20 分钟，30 分钟格位
#: 最近样本的偏差一般 ≤10 分钟，容差 15 分钟内都能匹配上。
REL_TOL = 15

HEAD_FILL = PatternFill('solid', fgColor='2F5597')
HEAD_FONT = Font(color='FFFFFF', bold=True, size=10)
TIME_FILL = PatternFill('solid', fgColor='DEEBF7')
THIN = Side(style='thin', color='BFBFBF')
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def main():
    if not os.path.exists(DB):
        print(f'!! 找不到 {DB}')
        return 1
    os.makedirs(OUT, exist_ok=True)

    c = sqlite3.connect(DB)

    # ---------- 话题基础信息 ----------
    topics = {r[0]: {'word': r[1], 'board': r[2], 'peak': r[3]}
              for r in c.execute("""SELECT t.topic_id, t.word, b.name, t.peak_heat
                                    FROM topics t JOIN boards b ON b.board_id=t.board_id""")}

    # ---------- 采样时刻清单 ----------
    ticks = [r[0] for r in c.execute(
        'SELECT DISTINCT sample_ts FROM heat_samples ORDER BY sample_ts')]
    tick_idx = {ts: i for i, ts in enumerate(ticks)}

    # ---------- 热度数据 ----------
    series = defaultdict(dict)          # tid -> {ts: num}
    first_seen = {}
    for tid, ts, num in c.execute(
            'SELECT topic_id, sample_ts, num FROM heat_samples'):
        if num is None:
            continue
        series[tid][ts] = num
        if tid not in first_seen or ts < first_seen[tid]:
            first_seen[tid] = ts

    order = sorted(series, key=lambda t: (topics.get(t, {}).get('peak') or 0),
                   reverse=True)

    wb = Workbook()

    # ================= Sheet1：按采样时刻 =================
    ws = wb.active
    ws.title = '按采样时刻'
    hdr = ['序号', '事件名', '榜单', '峰值热度', '有效采样点'] + \
          [t[5:16] for t in ticks]                       # MM-DD HH:MM
    ws.append(hdr)
    for i, tid in enumerate(order, 1):
        info = topics.get(tid, {})
        row = series[tid]
        row_data = [i, info.get('word', ''), info.get('board', ''),
                    info.get('peak') or '', len(row)]
        for ts in ticks:
            row_data.append(row.get(ts, ''))
        ws.append(row_data)

    # ================= Sheet2：按上榜后分钟 =================
    ws2 = wb.create_sheet('按上榜后分钟')
    hdr2 = ['序号', '事件名', '榜单', '峰值热度'] + [f'{m}min' for m in REL_STEPS]
    ws2.append(hdr2)
    n_match = 0
    for i, tid in enumerate(order, 1):
        t0 = datetime.strptime(first_seen[tid], FMT)
        hits = {}
        for m in REL_STEPS:
            best, best_err = None, None
            for ts, num in series[tid].items():
                err = abs((datetime.strptime(ts, FMT) - t0).total_seconds() / 60 - m)
                if best_err is None or err < best_err:
                    best, best_err = num, err
            if best is not None and best_err <= REL_TOL:
                hits[m] = best
                n_match += 1
        info = topics.get(tid, {})
        ws2.append([i, info.get('word', ''), info.get('board', ''),
                    info.get('peak') or ''] + [hits.get(m, '') for m in REL_STEPS])

    # ---------- 样式 ----------
    for sheet, ncol in ((ws, len(hdr)), (ws2, len(hdr2))):
        sheet.freeze_panes = 'E2'
        for k in (1, 2, 3):
            sheet.column_dimensions[get_column_letter(k)].width = [6, 30, 8][k - 1]
        sheet.column_dimensions['D'].width = 12
        if sheet is ws:
            sheet.column_dimensions['E'].width = 11
        for j in range(4, ncol + 1):
            sheet.column_dimensions[get_column_letter(j)].width = 12
        for cell in sheet[1]:
            cell.fill = HEAD_FILL
            cell.font = HEAD_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center',
                                       wrap_text=True)
            cell.border = BORDER
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=ncol):
            for cell in row:
                cell.border = BORDER
                if cell.row > 1:
                    cell.alignment = Alignment(horizontal='center')

    # 时间点表头用淡蓝区分
    for j in range(5, len(hdr) + 1):
        ws.cell(1, j).fill = TIME_FILL

    path = os.path.join(OUT, '热度变化表.xlsx')
    wb.save(path)
    print(f'✓ {path}')
    print(f'  按采样时刻：{len(order)} 话题 × {len(ticks)} 个时间点')
    print(f'  按上榜后分钟：{len(order)} 话题 × {len(REL_STEPS)} 个刻度'
          f'（0~360 分钟，每 30 分钟一格）')
    print(f'  相对刻度命中单元格 {n_match} 个')
    return 0


if __name__ == '__main__':
    sys.exit(main())

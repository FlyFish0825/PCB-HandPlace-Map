#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PCB 双页手工贴片定位图生成器（防遮线/防汇聚点重合版）
-----------------------------------
改进点：
- Top 一页，Bottom 一页
- A4 横向
- 顶边/底边标签上下交错排布，减少遮挡
- 左右边标签分列错开
- 长名字自动换行，并优先放在更外侧
- 引线更细，丝印更清晰
- 引线与黑点位于最高图层，不被文字遮挡
- 汇聚点沿板边强制拉开，避免黑点重合
"""

from __future__ import annotations

import argparse
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties, fontManager


@dataclass
class BomPart:
    ref: str
    category: str
    value: str
    footprint: str
    assembly: str
    note: str = ""


@dataclass
class Placement:
    ref: str
    x: float
    y: float
    layer: str
    rotation: float
    footprint: str
    comment: str
    smd: str


@dataclass
class Part:
    ref: str
    category: str
    value: str
    footprint: str
    assembly: str
    x: float
    y: float
    layer: str
    rotation: float
    comment: str
    smd: str


@dataclass
class GerberPath:
    points: List[Tuple[float, float]]
    width: float = 0.12


_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_col_index(cell_ref: str) -> int:
    m = re.match(r"([A-Z]+)", cell_ref.upper())
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def read_xlsx_rows(path: Path, preferred_sheet: Optional[str] = None) -> List[List[str]]:
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        shared: List[str] = []

        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{{{_NS_MAIN}}}si"):
                texts = []
                for t in si.iter(f"{{{_NS_MAIN}}}t"):
                    texts.append(t.text or "")
                shared.append("".join(texts))

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

        rel_map = {}
        for rel in rel_root.findall(f"{{{_NS_PKGREL}}}Relationship"):
            rel_map[rel.attrib["Id"]] = rel.attrib["Target"]

        sheets = []
        sheets_node = wb_root.find(f"{{{_NS_MAIN}}}sheets")
        for sh in sheets_node:
            name = sh.attrib.get("name", "")
            rid = sh.attrib.get(f"{{{_NS_REL}}}id")
            target = rel_map.get(rid, "")
            if not target.startswith("/"):
                target = "xl/" + target.lstrip("/")
            else:
                target = target.lstrip("/")
            sheets.append((name, target))

        chosen = None
        if preferred_sheet:
            for item in sheets:
                if item[0] == preferred_sheet:
                    chosen = item
                    break
        if chosen is None:
            chosen = sheets[0]

        root = ET.fromstring(zf.read(chosen[1]))
        sheet_data = root.find(f"{{{_NS_MAIN}}}sheetData")
        out = []
        for row in sheet_data.findall(f"{{{_NS_MAIN}}}row"):
            values = {}
            max_col = -1
            for c in row.findall(f"{{{_NS_MAIN}}}c"):
                ref = c.attrib.get("r", "A1")
                col = _xlsx_col_index(ref)
                max_col = max(max_col, col)
                ctype = c.attrib.get("t")
                value = ""

                if ctype == "inlineStr":
                    is_node = c.find(f"{{{_NS_MAIN}}}is")
                    if is_node is not None:
                        texts = [t.text or "" for t in is_node.iter(f"{{{_NS_MAIN}}}t")]
                        value = "".join(texts)
                else:
                    v = c.find(f"{{{_NS_MAIN}}}v")
                    raw = "" if v is None or v.text is None else v.text
                    if ctype == "s" and raw:
                        try:
                            value = shared[int(raw)]
                        except Exception:
                            value = raw
                    elif ctype == "b":
                        value = "TRUE" if raw == "1" else "FALSE"
                    else:
                        value = raw

                values[col] = value

            if max_col < 0:
                out.append([])
            else:
                out.append([values.get(i, "") for i in range(max_col + 1)])
        return out


def norm_header(s: str) -> str:
    return re.sub(r"[\s_\-/]+", "", str(s or "")).lower()


def find_col(headers: Sequence[str], aliases: Sequence[str], required=True) -> Optional[int]:
    hmap = {norm_header(h): i for i, h in enumerate(headers)}
    for a in aliases:
        k = norm_header(a)
        if k in hmap:
            return hmap[k]
    for i, h in enumerate(headers):
        nh = norm_header(h)
        for a in aliases:
            na = norm_header(a)
            if na and (na in nh or nh in na):
                return i
    if required:
        raise KeyError(f"找不到列：{aliases}；现有列：{list(headers)}")
    return None


def cell(row: Sequence[str], idx: Optional[int], default="") -> str:
    if idx is None or idx >= len(row):
        return default
    return str(row[idx] if row[idx] is not None else "").strip()


def split_designators(text: str) -> List[str]:
    text = str(text or "").replace("，", ",").replace("、", ",").replace(";", ",")
    items = []
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        for token in re.split(r"\s+", p):
            token = token.strip()
            if token:
                items.append(token)
    return items


def parse_mm(v: str) -> float:
    return float(str(v).strip().lower().replace("mm", "").strip())


def normalize_layer(v: str) -> str:
    s = str(v or "").strip().lower()
    if s in {"t", "top", "toplayer", "front", "f"} or "top" in s:
        return "T"
    if s in {"b", "bottom", "bottomlayer", "back"} or "bottom" in s:
        return "B"
    return "T"


def is_dnp(assembly: str) -> bool:
    s = str(assembly or "").strip().lower()
    return any(k in s for k in ["dnp", "不贴", "不装", "不焊", "nc"])


def ref_prefix_num(ref: str):
    m = re.match(r"^([A-Za-z]+)(\d+)", ref)
    if not m:
        return ref.upper(), None
    return m.group(1).upper(), int(m.group(2))


def canonical_value(v: str) -> str:
    s = str(v or "").strip()
    s = s.replace("Ω", "ohm").replace("Ω", "ohm")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def load_bom(path: Path) -> Dict[str, BomPart]:
    rows = read_xlsx_rows(path, preferred_sheet="最终BOM")
    if not rows:
        raise RuntimeError("BOM 为空")

    headers = rows[0]
    i_cat = find_col(headers, ["分类", "category", "类型", "type"])
    i_ref = find_col(headers, ["位号", "designator", "reference", "refdes", "refs"])
    i_val = find_col(headers, ["参数/型号", "参数", "型号", "value", "comment", "part"])
    i_fp = find_col(headers, ["封装", "footprint", "package"], required=False)
    i_asm = find_col(headers, ["装配", "assembly", "dnp", "贴装"], required=False)
    i_note = find_col(headers, ["备注", "note", "notes"], required=False)

    parts = {}
    for row in rows[1:]:
        refs = split_designators(cell(row, i_ref))
        if not refs:
            continue
        category = cell(row, i_cat, "其他") or "其他"
        value = cell(row, i_val, "") or "未标值"
        footprint = cell(row, i_fp, "")
        assembly = cell(row, i_asm, "贴装")
        note = cell(row, i_note, "")
        for ref in refs:
            parts[ref] = BomPart(
                ref=ref, category=category, value=value,
                footprint=footprint, assembly=assembly, note=note
            )
    return parts


def load_pnp(path: Path) -> Dict[str, Placement]:
    rows = read_xlsx_rows(path)
    if not rows:
        raise RuntimeError("PickAndPlace 为空")

    headers = rows[0]
    i_ref = find_col(headers, ["Designator", "位号", "Reference", "RefDes"])
    i_x = find_col(headers, ["Mid X", "Center X", "X", "坐标X"])
    i_y = find_col(headers, ["Mid Y", "Center Y", "Y", "坐标Y"])
    i_layer = find_col(headers, ["Layer", "层"])
    i_rot = find_col(headers, ["Rotation", "Rot", "角度"], required=False)
    i_fp = find_col(headers, ["Footprint", "封装"], required=False)
    i_comment = find_col(headers, ["Comment", "Value", "参数", "型号"], required=False)
    i_smd = find_col(headers, ["SMD", "贴片"], required=False)

    out = {}
    for row in rows[1:]:
        ref = cell(row, i_ref)
        if not ref:
            continue
        try:
            x = parse_mm(cell(row, i_x))
            y = parse_mm(cell(row, i_y))
        except Exception:
            continue
        try:
            rot = float(cell(row, i_rot, "0") or 0)
        except Exception:
            rot = 0.0
        out[ref] = Placement(
            ref=ref, x=x, y=y, layer=normalize_layer(cell(row, i_layer, "T")),
            rotation=rot, footprint=cell(row, i_fp, ""),
            comment=cell(row, i_comment, ""), smd=cell(row, i_smd, "")
        )
    return out


def join_parts(bom: Dict[str, BomPart], pnp: Dict[str, Placement], include_dnp=False):
    parts = []
    missing_pnp = []
    missing_bom = []

    for ref, b in bom.items():
        if is_dnp(b.assembly) and not include_dnp:
            continue
        p = pnp.get(ref)
        if p is None:
            missing_pnp.append(ref)
            continue
        parts.append(Part(
            ref=ref, category=b.category, value=b.value,
            footprint=b.footprint or p.footprint, assembly=b.assembly,
            x=p.x, y=p.y, layer=p.layer, rotation=p.rotation,
            comment=p.comment, smd=p.smd
        ))

    for ref in pnp:
        if ref not in bom:
            missing_bom.append(ref)

    return parts, sorted(missing_pnp), sorted(missing_bom)


def _gerber_number(raw: str, decimals: int, unit_scale: float) -> float:
    return int(raw) / (10 ** decimals) * unit_scale


def parse_gerber_text(text: str) -> List[GerberPath]:
    decimals = 5
    unit_scale = 1.0
    apertures: Dict[int, float] = {}
    current_ap = 10
    current_width = 0.12
    x = y = 0.0
    paths: List[GerberPath] = []

    fmt_re = re.compile(r"FSL.AX(\d)(\d)Y(\d)(\d)", re.I)
    add_re = re.compile(r"ADD(\d+)([A-Z]),?([^*%]*)", re.I)

    def add_line(p0, p1, width):
        if p0 == p1:
            return
        paths.append(GerberPath([p0, p1], max(0.03, width)))

    def add_arc(p0, p1, ioff, joff, cw, width):
        cx = p0[0] + ioff
        cy = p0[1] + joff
        r0 = math.hypot(p0[0] - cx, p0[1] - cy)
        r1 = math.hypot(p1[0] - cx, p1[1] - cy)
        if r0 < 1e-8 or abs(r0 - r1) > max(0.1, r0 * 0.05):
            add_line(p0, p1, width)
            return
        a0 = math.atan2(p0[1] - cy, p0[0] - cx)
        a1 = math.atan2(p1[1] - cy, p1[0] - cx)
        if cw:
            while a1 >= a0:
                a1 -= 2 * math.pi
        else:
            while a1 <= a0:
                a1 += 2 * math.pi
        da = a1 - a0
        steps = max(8, int(abs(da) * r0 / 0.25))
        pts = []
        for k in range(steps + 1):
            a = a0 + da * k / steps
            pts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a)))
        paths.append(GerberPath(pts, max(0.03, width)))

    interp = "G01"

    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if "FSL" in line:
            m = fmt_re.search(line)
            if m:
                decimals = int(m.group(2))
            continue
        if "MOIN" in line:
            unit_scale = 25.4
            continue
        if "MOMM" in line:
            unit_scale = 1.0
            continue
        m = add_re.search(line)
        if m:
            dcode = int(m.group(1))
            shape = m.group(2).upper()
            params = m.group(3)
            if shape == "C":
                try:
                    diameter = float(params.split("X")[0].split(",")[0]) * unit_scale
                    apertures[dcode] = diameter
                except Exception:
                    pass
            continue
        m = re.fullmatch(r"(?:G54)?D(\d+)\*", line)
        if m:
            current_ap = int(m.group(1))
            current_width = apertures.get(current_ap, current_width)
            continue
        if line.startswith("G01"):
            interp = "G01"
        elif line.startswith("G02"):
            interp = "G02"
        elif line.startswith("G03"):
            interp = "G03"
        if "X" not in line and "Y" not in line:
            continue

        mx = re.search(r"X([+-]?\d+)", line)
        my = re.search(r"Y([+-]?\d+)", line)
        mi = re.search(r"I([+-]?\d+)", line)
        mj = re.search(r"J([+-]?\d+)", line)
        md = re.search(r"D0?([123])\*", line)

        nx = x if mx is None else _gerber_number(mx.group(1), decimals, unit_scale)
        ny = y if my is None else _gerber_number(my.group(1), decimals, unit_scale)
        d = int(md.group(1)) if md else 1

        if d == 2:
            x, y = nx, ny
            continue
        if d == 1:
            p0 = (x, y)
            p1 = (nx, ny)
            width = apertures.get(current_ap, current_width)
            if interp in ("G02", "G03"):
                ioff = 0.0 if mi is None else _gerber_number(mi.group(1), decimals, unit_scale)
                joff = 0.0 if mj is None else _gerber_number(mj.group(1), decimals, unit_scale)
                add_arc(p0, p1, ioff, joff, cw=(interp == "G02"), width=width)
            else:
                add_line(p0, p1, width)
            x, y = nx, ny

    return paths


def find_zip_member(names: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    lower_map = {Path(n).name.lower(): n for n in names}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    for n in names:
        ln = n.lower()
        for c in candidates:
            if ln.endswith(Path(c).suffix.lower()):
                if "outline" in c.lower() and "outline" in ln:
                    return n
                if "topsilk" in c.lower() and ("topsilk" in ln or ln.endswith(".gto")):
                    return n
                if "bottomsilk" in c.lower() and ("bottomsilk" in ln or ln.endswith(".gbo")):
                    return n
    return None


def load_gerbers(zip_path: Path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        outline_name = find_zip_member(names, [
            "Gerber_BoardOutlineLayer.GKO", "BoardOutline.GKO", "Edge_Cuts.gbr",
        ])
        top_name = find_zip_member(names, [
            "Gerber_TopSilkscreenLayer.GTO", "TopSilkscreen.GTO",
        ])
        bottom_name = find_zip_member(names, [
            "Gerber_BottomSilkscreenLayer.GBO", "BottomSilkscreen.GBO",
        ])

        def read_member(name):
            if not name:
                return []
            txt = zf.read(name).decode("utf-8", errors="ignore")
            return parse_gerber_text(txt)

        outline = read_member(outline_name)
        top = read_member(top_name)
        bottom = read_member(bottom_name)

    if not outline:
        raise RuntimeError("Gerber 压缩包中未找到可解析的板框 GKO")
    return outline, top, bottom


def paths_bbox(paths: Sequence[GerberPath]) -> Tuple[float, float, float, float]:
    xs, ys = [], []
    for p in paths:
        for x, y in p.points:
            xs.append(x)
            ys.append(y)
    return min(xs), max(xs), min(ys), max(ys)


def distance(a: Part, b: Part) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def group_key(p: Part):
    return (p.category.strip().lower(), canonical_value(p.value))


def try_phase_groups(parts: List[Part], phase_size=3) -> Optional[List[List[Part]]]:
    if len(parts) < phase_size or len(parts) % phase_size != 0:
        return None
    prefixes = set()
    by_num = {}
    for p in parts:
        prefix, num = ref_prefix_num(p.ref)
        prefixes.add(prefix)
        if num is None:
            return None
        by_num[num] = p
    if len(prefixes) != 1:
        return None

    nums = sorted(by_num)
    n_groups = len(parts) // phase_size
    candidates = []
    for start in nums:
        for step in range(1, max(nums) - min(nums) + 1):
            seq = [start + step * k for k in range(phase_size)]
            if all(n in by_num for n in seq):
                grp = [by_num[n] for n in seq]
                ds = [distance(grp[i], grp[i + 1]) for i in range(len(grp) - 1)]
                geom_penalty = (max(ds) - min(ds)) if ds else 0.0
                score = geom_penalty + 0.02 * step
                candidates.append((score, tuple(seq), grp))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])

    all_nums = set(nums)
    best = None

    def backtrack(used, chosen, score_sum):
        nonlocal best
        if len(used) == len(all_nums):
            if len(chosen) == n_groups:
                if best is None or score_sum < best[0]:
                    best = (score_sum, [c[:] for c in chosen])
            return
        if len(chosen) >= n_groups:
            return
        first = min(all_nums - used)
        for score, seq, grp in candidates:
            sset = set(seq)
            if first not in sset:
                continue
            if sset & used:
                continue
            if best is not None and score_sum + score >= best[0]:
                continue
            backtrack(used | sset, chosen + [grp], score_sum + score)

    backtrack(set(), [], 0.0)
    return None if best is None else best[1]


def spatial_groups(parts: List[Part], max_group_size=4) -> List[List[Part]]:
    if len(parts) <= max_group_size:
        return [sorted(parts, key=lambda p: (p.y, p.x, p.ref))]
    remain = parts[:]
    groups = []
    while remain:
        seed = min(remain, key=lambda p: (p.x, -p.y, p.ref))
        remain.remove(seed)
        group = [seed]
        while remain and len(group) < max_group_size:
            cx = sum(p.x for p in group) / len(group)
            cy = sum(p.y for p in group) / len(group)
            nxt = min(remain, key=lambda p: math.hypot(p.x - cx, p.y - cy))
            group.append(nxt)
            remain.remove(nxt)
        groups.append(sorted(group, key=lambda p: (p.x, p.y, p.ref)))
    return groups


def build_groups(parts: List[Part], phase_size=3, max_group_size=4):
    by_key = defaultdict(list)
    for p in parts:
        by_key[group_key(p)].append(p)

    groups = []
    for _, same_parts in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        phase = try_phase_groups(same_parts, phase_size=phase_size)
        if phase:
            groups.extend(phase)
        else:
            groups.extend(spatial_groups(same_parts, max_group_size=max_group_size))
    return groups


def choose_chinese_font() -> Optional[FontProperties]:
    preferred = [
        "Microsoft YaHei", "Microsoft YaHei UI", "SimHei",
        "Noto Sans CJK SC", "Noto Sans CJK JP",
        "Source Han Sans SC", "WenQuanYi Micro Hei",
        "Arial Unicode MS",
    ]
    names = {f.name: f.fname for f in fontManager.ttflist}
    for name in preferred:
        if name in names:
            return FontProperties(fname=names[name])
    return None


def mirror_x(x: float, bbox) -> float:
    xmin, xmax, _, _ = bbox
    return xmin + xmax - x


def transform_point(x, y, bbox, mirror=False):
    if mirror:
        x = mirror_x(x, bbox)
    return x, y


def transformed_path_points(path: GerberPath, bbox, mirror=False):
    return [transform_point(x, y, bbox, mirror) for x, y in path.points]


def choose_side(cx, cy, bbox):
    xmin, xmax, ymin, ymax = bbox
    dist = {
        "left": abs(cx - xmin),
        "right": abs(xmax - cx),
        "bottom": abs(cy - ymin),
        "top": abs(ymax - cy),
    }
    return min(dist, key=dist.get)


def wrap_token(text: str, max_len: int = 15) -> str:
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s

    # 先尝试在分隔符附近断开
    candidates = []
    for sep in ["-", "_", "/", ",", "x"]:
        for m in re.finditer(re.escape(sep), s):
            idx = m.start() + 1
            if 6 <= idx <= len(s) - 6:
                candidates.append(idx)

    if candidates:
        mid = len(s) / 2
        cut = min(candidates, key=lambda x: abs(x - mid))
        return s[:cut] + "\n" + s[cut:]

    # 否则硬换行
    k = max_len
    return s[:k] + "\n" + s[k:]


def compact_refs(refs: Sequence[str]) -> str:
    refs = list(refs)
    if len(refs) <= 4:
        return " ".join(refs)
    mid = math.ceil(len(refs) / 2)
    return " ".join(refs[:mid]) + "\n" + " ".join(refs[mid:])


def group_label(grp: List[Part]) -> str:
    value = grp[0].value or grp[0].comment or "未标值"
    value = wrap_token(value, max_len=15)
    refs = compact_refs([p.ref for p in grp])
    return f"{value}\n{refs}"


def estimate_label_size(label: str) -> Tuple[float, float, int]:
    lines = label.split("\n")
    longest = max((len(x) for x in lines), default=0)
    nlines = len(lines)
    width = 1.08 * longest + 2.8
    height = 2.8 * nlines + 1.2
    return width, height, longest


def _stagger_lane_for_long_label(longest_chars: int, lane_count: int) -> int:
    if lane_count <= 1:
        return 0
    if longest_chars >= 16:
        return lane_count - 1
    if longest_chars >= 12 and lane_count >= 3:
        return 1
    return 0



def spread_anchor_positions(targets, lo, hi, min_gap):
    """
    将板边汇聚点沿边方向强制拉开，避免黑点几乎重合。
    targets 必须按从小到大排列。
    """
    n = len(targets)
    if n == 0:
        return []
    if n == 1:
        return [min(max(targets[0], lo), hi)]

    span = max(0.0, hi - lo)
    # 如果点太多放不下，则自动缩小最小间距，但仍均匀分开
    gap = min(min_gap, span / max(1, n - 1))

    p = [0.0] * n
    p[0] = min(max(targets[0], lo), hi)

    # 正向推开
    for i in range(1, n):
        p[i] = max(targets[i], p[i - 1] + gap)

    # 整体向左/下回推，防止越界
    if p[-1] > hi:
        shift = p[-1] - hi
        p = [x - shift for x in p]

    # 反向再次保证间距
    for i in range(n - 2, -1, -1):
        p[i] = min(p[i], p[i + 1] - gap)

    # 若最前面越界，则整体移回
    if p[0] < lo:
        shift = lo - p[0]
        p = [x + shift for x in p]

    return p


def allocate_labels(groups, bbox, mirror=False):
    xmin, xmax, ymin, ymax = bbox
    w = xmax - xmin
    h = ymax - ymin

    items_by_side = defaultdict(list)

    for grp in groups:
        pts = [transform_point(p.x, p.y, bbox, mirror) for p in grp]
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        side = choose_side(cx, cy, bbox)
        label = group_label(grp)
        label_w, label_h, longest_chars = estimate_label_size(label)
        items_by_side[side].append({
            "grp": grp,
            "cx": cx,
            "cy": cy,
            "label": label,
            "label_w": label_w,
            "label_h": label_h,
            "longest_chars": longest_chars,
        })

    labels = {}

    # top / bottom: 多行上下交错
    def place_horizontal(side: str, base_out: float, lane_gap: float, lane_count: int):
        items = items_by_side.get(side, [])
        if not items:
            return

        items.sort(key=lambda d: d["cx"])
        span_lo = xmin + w * 0.03
        span_hi = xmax - w * 0.03
        gap = 1.4

        lane_end = [span_lo for _ in range(lane_count)]
        lane_items = [[] for _ in range(lane_count)]

        for item in items:
            width = item["label_w"]
            preferred_lane = _stagger_lane_for_long_label(item["longest_chars"], lane_count)
            candidate_order = [preferred_lane] + [i for i in range(lane_count) if i != preferred_lane]

            chosen_lane = None
            best_x = None
            best_cost = None

            for lane in candidate_order:
                x = max(item["cx"], lane_end[lane] + width / 2)
                x = min(x, span_hi - width / 2)
                overflow = max(0.0, (lane_end[lane] + width / 2) - item["cx"])
                cost = overflow + 0.6 * abs(lane - preferred_lane)
                if best_cost is None or cost < best_cost:
                    chosen_lane = lane
                    best_x = x
                    best_cost = cost

            lane_end[chosen_lane] = best_x + width / 2 + gap
            item["lane"] = chosen_lane
            item["tx"] = best_x
            lane_items[chosen_lane].append(item)

        # 右向左回推，避免被右边挤爆
        for lane in range(lane_count):
            arr = lane_items[lane]
            right_limit = span_hi
            for item in reversed(arr):
                width = item["label_w"]
                x = min(item["tx"], right_limit - width / 2)
                x = max(x, span_lo + width / 2)
                item["tx"] = x
                right_limit = x - width / 2 - gap

        # 汇聚黑点单独做一次“最小间距”排布，避免点挤在一起
        anchor_lo = xmin + w * 0.025
        anchor_hi = xmax - w * 0.025
        anchor_gap = max(2.6, w * 0.045)
        anchor_xs = spread_anchor_positions(
            [item["cx"] for item in items],
            anchor_lo, anchor_hi, anchor_gap
        )

        for item, jx in zip(items, anchor_xs):
            lane = item["lane"]
            if side == "top":
                ty = ymax + base_out + lane * lane_gap
                jy = ymax + base_out * 0.40
            else:
                ty = ymin - base_out - lane * lane_gap
                jy = ymin - base_out * 0.40

            labels[id(item["grp"])] = (
                side,
                (jx, jy),
                (item["tx"], ty),
                item["label"],
            )

    # left / right: 分列错开
    def place_vertical(side: str, base_out: float, lane_gap: float, lane_count: int):
        items = items_by_side.get(side, [])
        if not items:
            return

        items.sort(key=lambda d: d["cy"])
        span_lo = ymin + h * 0.03
        span_hi = ymax - h * 0.03
        gap = 0.8

        lane_end = [span_lo for _ in range(lane_count)]
        lane_items = [[] for _ in range(lane_count)]

        for idx, item in enumerate(items):
            height = item["label_h"]
            preferred_lane = idx % lane_count
            if item["longest_chars"] >= 16:
                preferred_lane = lane_count - 1
            candidate_order = [preferred_lane] + [i for i in range(lane_count) if i != preferred_lane]

            chosen_lane = None
            best_y = None
            best_cost = None

            for lane in candidate_order:
                y = max(item["cy"], lane_end[lane] + height / 2)
                y = min(y, span_hi - height / 2)
                overflow = max(0.0, (lane_end[lane] + height / 2) - item["cy"])
                cost = overflow + 0.5 * abs(lane - preferred_lane)
                if best_cost is None or cost < best_cost:
                    chosen_lane = lane
                    best_y = y
                    best_cost = cost

            lane_end[chosen_lane] = best_y + height / 2 + gap
            item["lane"] = chosen_lane
            item["ty"] = best_y
            lane_items[chosen_lane].append(item)

        for lane in range(lane_count):
            arr = lane_items[lane]
            upper = span_hi
            for item in reversed(arr):
                height = item["label_h"]
                y = min(item["ty"], upper - height / 2)
                y = max(y, span_lo + height / 2)
                item["ty"] = y
                upper = y - height / 2 - gap

        # 左右边的汇聚黑点也强制保留最小纵向间距
        anchor_lo = ymin + h * 0.025
        anchor_hi = ymax - h * 0.025
        anchor_gap = max(2.3, h * 0.065)
        anchor_ys = spread_anchor_positions(
            [item["cy"] for item in items],
            anchor_lo, anchor_hi, anchor_gap
        )

        for item, jy in zip(items, anchor_ys):
            lane = item["lane"]
            if side == "left":
                tx = xmin - base_out - lane * lane_gap
                jx = xmin - base_out * 0.42
            else:
                tx = xmax + base_out + lane * lane_gap
                jx = xmax + base_out * 0.42

            labels[id(item["grp"])] = (
                side,
                (jx, jy),
                (tx, item["ty"]),
                item["label"],
            )

    place_horizontal("top", base_out=max(5.5, h * 0.15), lane_gap=max(4.2, h * 0.11), lane_count=3)
    place_horizontal("bottom", base_out=max(5.5, h * 0.15), lane_gap=max(4.2, h * 0.11), lane_count=3)
    place_vertical("left", base_out=max(5.5, w * 0.17), lane_gap=max(4.0, w * 0.10), lane_count=2)
    place_vertical("right", base_out=max(5.5, w * 0.17), lane_gap=max(4.0, w * 0.10), lane_count=2)

    return labels


def draw_one_side(ax, layer_name: str, parts: List[Part], outline, silk, bbox,
                  mirror: bool, font_prop: Optional[FontProperties],
                  phase_size=3, max_group_size=4):
    groups = build_groups(parts, phase_size=phase_size, max_group_size=max_group_size)
    label_pos = allocate_labels(groups, bbox, mirror=mirror)

    xmin, xmax, ymin, ymax = bbox
    w = xmax - xmin
    h = ymax - ymin

    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    for path in silk:
        pts = transformed_path_points(path, bbox, mirror)
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys,
                linewidth=max(0.34, min(1.15, path.width * 2.45)),
                color="0.18", alpha=0.98, zorder=1)

    for path in outline:
        pts = transformed_path_points(path, bbox, mirror)
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, linewidth=1.35, color="0.00", alpha=1.0, zorder=2)

    for grp in groups:
        side, junction, text_pos, label = label_pos[id(grp)]
        jx, jy = junction
        tx, ty = text_pos

        # 所有辅助引线都放在最上层，绝不被文字白底或丝印盖住
        LINE_Z = 40
        DOT_Z = 41

        for p in grp:
            px, py = transform_point(p.x, p.y, bbox, mirror)
            ax.plot([px, jx], [py, jy],
                    linewidth=0.16, color="0.24", alpha=0.86,
                    zorder=LINE_Z, solid_capstyle="round")
            ax.scatter([px], [py], s=11, facecolors="white",
                       edgecolors="0.02", linewidths=0.48, zorder=DOT_Z)

        ax.scatter([jx], [jy], s=15, marker="o",
                   color="0.00", zorder=DOT_Z)

        # 从汇聚点连到标签附近。线仍在最上层。
        # 终点略停在文字锚点外侧，减少直接穿过本标签文字。
        ex, ey = tx, ty
        if side == "left":
            ex = tx + 0.10
        elif side == "right":
            ex = tx - 0.10
        elif side == "top":
            ey = ty - 0.20
        else:
            ey = ty + 0.20

        ax.plot([jx, ex], [jy, ey],
                linewidth=0.18, color="0.12", alpha=0.90,
                zorder=LINE_Z, solid_capstyle="round")

        if side == "left":
            ha, va, dx, dy = "right", "center", -0.45, 0
        elif side == "right":
            ha, va, dx, dy = "left", "center", 0.45, 0
        elif side == "top":
            ha, va, dx, dy = "center", "bottom", 0, 0.28
        else:
            ha, va, dx, dy = "center", "top", 0, -0.28

        kwargs = dict(
            fontsize=7.1,
            ha=ha,
            va=va,
            linespacing=1.10,
            fontweight="bold",
            zorder=20,
            bbox=dict(boxstyle="round,pad=0.16", facecolor="white",
                      edgecolor="none", alpha=0.94),
        )
        if font_prop is not None:
            kwargs["fontproperties"] = font_prop

        ax.text(tx + dx, ty + dy, label, **kwargs)

    mx = max(18.0, w * 0.42)
    my = max(14.0, h * 0.55)
    ax.set_xlim(xmin - mx, xmax + mx)
    ax.set_ylim(ymin - my, ymax + my)

    title_kwargs = dict(fontsize=12, fontweight="bold")
    if font_prop is not None:
        title_kwargs["fontproperties"] = font_prop
    ax.set_title(layer_name, **title_kwargs)


def draw_two_sheets(parts: List[Part], outline, top_silk, bottom_silk, bbox,
                    out_pdf: Path, out_top_svg: Path, out_top_png: Path,
                    out_bottom_svg: Path, out_bottom_png: Path,
                    bottom_mirror=True, phase_size=3, max_group_size=4):
    font_prop = choose_chinese_font()

    top_parts = [p for p in parts if p.layer == "T"]
    bottom_parts = [p for p in parts if p.layer == "B"]

    title_kwargs = dict(fontsize=16, fontweight="bold", ha="center")
    sub_kwargs = dict(fontsize=9, ha="center")
    footer_kwargs = dict(fontsize=8, ha="center")
    if font_prop is not None:
        title_kwargs["fontproperties"] = font_prop
        sub_kwargs["fontproperties"] = font_prop
        footer_kwargs["fontproperties"] = font_prop

    # Top page
    fig_top = plt.figure(figsize=(11.69, 8.27), dpi=220)
    ax = fig_top.add_axes([0.08, 0.12, 0.84, 0.74])
    draw_one_side(
        ax=ax, layer_name="Top / 正面", parts=top_parts,
        outline=outline, silk=top_silk, bbox=bbox, mirror=False,
        font_prop=font_prop, phase_size=phase_size, max_group_size=max_group_size
    )
    fig_top.text(0.5, 0.962, "PCB 手工贴片定位图", **title_kwargs)
    fig_top.text(0.5, 0.933, f"Top / 正面   |   器件 {len(top_parts)}", **sub_kwargs)
    fig_top.text(0.5, 0.045, "空心圆为贴装中心；同料器件做分组引线；长名字已自动换行与交错排布。", **footer_kwargs)
    fig_top.savefig(out_top_svg, format="svg", pad_inches=0.18)
    fig_top.savefig(out_top_png, format="png", dpi=320, pad_inches=0.18)

    # Bottom page
    fig_bottom = plt.figure(figsize=(11.69, 8.27), dpi=220)
    ax = fig_bottom.add_axes([0.08, 0.12, 0.84, 0.74])
    draw_one_side(
        ax=ax,
        layer_name="Bottom / 反面（翻板视图）" if bottom_mirror else "Bottom / 反面",
        parts=bottom_parts, outline=outline, silk=bottom_silk, bbox=bbox,
        mirror=bottom_mirror, font_prop=font_prop,
        phase_size=phase_size, max_group_size=max_group_size
    )
    fig_bottom.text(0.5, 0.962, "PCB 手工贴片定位图", **title_kwargs)
    fig_bottom.text(
        0.5, 0.933,
        f"{'Bottom / 反面（翻板视图）' if bottom_mirror else 'Bottom / 反面'}   |   器件 {len(bottom_parts)}",
        **sub_kwargs
    )
    fig_bottom.text(0.5, 0.045, "空心圆为贴装中心；同料器件做分组引线；长名字已自动换行与交错排布。", **footer_kwargs)
    fig_bottom.savefig(out_bottom_svg, format="svg", pad_inches=0.18)
    fig_bottom.savefig(out_bottom_png, format="png", dpi=320, pad_inches=0.18)

    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(out_pdf) as pdf:
        pdf.savefig(fig_top, pad_inches=0.18)
        pdf.savefig(fig_bottom, pad_inches=0.18)

    plt.close(fig_top)
    plt.close(fig_bottom)


def write_summary(out_dir: Path, parts: List[Part], missing_pnp: List[str], missing_bom: List[str]):
    lines = []
    lines.append("PCB 双页贴片图生成报告")
    lines.append("=" * 48)
    lines.append(f"参与绘图器件数量: {len(parts)}")
    lines.append(f"Top: {sum(1 for p in parts if p.layer == 'T')}")
    lines.append(f"Bottom: {sum(1 for p in parts if p.layer == 'B')}")
    lines.append("")
    lines.append("BOM 中有、但 Pick&Place 找不到：")
    lines.append("  " + (", ".join(missing_pnp) if missing_pnp else "无"))
    lines.append("")
    lines.append("Pick&Place 中有、但 BOM 找不到：")
    lines.append("  " + (", ".join(missing_bom) if missing_bom else "无"))
    (out_dir / "生成报告.txt").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="生成双页 PCB 手工贴片定位图（Top 一页 + Bottom 一页，标签交错防遮挡）")
    ap.add_argument("--bom", required=True, type=Path, help="BOM xlsx")
    ap.add_argument("--pnp", required=True, type=Path, help="PickAndPlace xlsx")
    ap.add_argument("--gerber", required=True, type=Path, help="Gerber zip")
    ap.add_argument("--out", type=Path, default=Path("output"), help="输出目录")
    ap.add_argument("--include-dnp", action="store_true", help="包含 DNP")
    ap.add_argument("--no-bottom-mirror", action="store_true", help="Bottom 不镜像")
    ap.add_argument("--phase-size", type=int, default=3, help="优先三相/重复电路分组数量，默认 3")
    ap.add_argument("--max-spatial-group", type=int, default=4, help="空间聚类每组最多器件数量，默认 4")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    print("[1/5] 读取 BOM ...")
    bom = load_bom(args.bom)

    print("[2/5] 读取 Pick&Place ...")
    pnp = load_pnp(args.pnp)

    print("[3/5] 合并 BOM + 坐标 ...")
    parts, missing_pnp, missing_bom = join_parts(
        bom, pnp, include_dnp=args.include_dnp
    )
    if not parts:
        raise RuntimeError("没有可绘制器件")

    print("[4/5] 解析 Gerber ...")
    outline, top_silk, bottom_silk = load_gerbers(args.gerber)
    bbox = paths_bbox(outline)

    print("[5/5] 生成双页图 ...")
    out_pdf = args.out / "PCB_双页手工贴片定位图.pdf"
    out_top_svg = args.out / "PCB_贴片定位图_Top.svg"
    out_top_png = args.out / "PCB_贴片定位图_Top.png"
    out_bottom_svg = args.out / "PCB_贴片定位图_Bottom.svg"
    out_bottom_png = args.out / "PCB_贴片定位图_Bottom.png"

    draw_two_sheets(
        parts=parts, outline=outline,
        top_silk=top_silk, bottom_silk=bottom_silk, bbox=bbox,
        out_pdf=out_pdf,
        out_top_svg=out_top_svg, out_top_png=out_top_png,
        out_bottom_svg=out_bottom_svg, out_bottom_png=out_bottom_png,
        bottom_mirror=not args.no_bottom_mirror,
        phase_size=max(2, args.phase_size),
        max_group_size=max(1, args.max_spatial_group)
    )

    write_summary(args.out, parts, missing_pnp, missing_bom)

    print("完成。")
    print(f"PDF: {out_pdf}")
    print(f"Top SVG: {out_top_svg}")
    print(f"Top PNG: {out_top_png}")
    print(f"Bottom SVG: {out_bottom_svg}")
    print(f"Bottom PNG: {out_bottom_png}")


if __name__ == "__main__":
    main()

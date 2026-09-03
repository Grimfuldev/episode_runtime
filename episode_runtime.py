#!/usr/bin/env python3
"""
Episode Runtime Studio
Edit a timeline in a GUI, store everything in JSON, preview/export the infographic.

Run:
  python3 episode_runtime_studio.py
  python3 episode_runtime_studio.py --json e9_example.json
  python3 episode_runtime_studio.py --json e9_example.json --png out.png
"""

from __future__ import annotations

import argparse
import colorsys
import copy
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import matplotlib

# GUI import may fail on headless machines; PNG export still works.
try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
    HAS_TK = True
except Exception:
    HAS_TK = False

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle


TIME_RE = re.compile(r"^\s*(\d{1,3}):(\d{1,2})\s*$")
ROW_RE = re.compile(
    r"^\s*(\d{1,3}:\d{1,2})\s*[-–—]\s*(\d{1,3}:\d{1,2}|_+)?\s*(.*?)\s*$"
)
BLANK_RE = re.compile(r"^__blank_\d+$")


def is_blank_category(name: str) -> bool:
    return not (name or "").strip() or bool(BLANK_RE.match(name or ""))


def next_blank_id(existing: list[str]) -> str:
    used = set()
    for name in existing:
        m = BLANK_RE.match(name or "")
        if m:
            used.add(int(name.split("_")[-1]))
    n = 1
    while n in used:
        n += 1
    return f"__blank_{n}"


def parse_hex_color(text: str) -> Optional[tuple[float, float, float]]:
    raw = (text or "").strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        r, g, b = int(raw[0:2], 16) / 255.0, int(raw[2:4], 16) / 255.0, int(raw[4:6], 16) / 255.0
    except ValueError:
        return None
    return colorsys.rgb_to_hsv(r, g, b)


def hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(1.0, max(0.0, s)), min(1.0, max(0.0, v)))
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


def next_category_color(existing_hexes: list[str]) -> str:
    """Place a neon color in the largest unused hue gap."""
    hues = []
    for hexcol in existing_hexes:
        hsv = parse_hex_color(hexcol)
        if hsv:
            hues.append(hsv[0])
    if not hues:
        return hsv_to_hex(0.83, 0.92, 1.0)
    hues = sorted(h % 1.0 for h in hues)
    best_start, best_gap = hues[0], 0.0
    for i, h in enumerate(hues):
        nxt = hues[(i + 1) % len(hues)]
        gap = (nxt - h) % 1.0
        if gap <= 0:
            gap = 1.0
        if gap > best_gap:
            best_start, best_gap = h, gap
    new_h = (best_start + best_gap / 2.0) % 1.0
    sat = 0.88 + (len(hues) % 3) * 0.04
    val = 0.96 + (len(hues) % 2) * 0.03
    return hsv_to_hex(new_h, sat, min(1.0, val))

def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def starter_json_path() -> Optional[Path]:
    for candidate in (resource_dir() / "default.json", Path(__file__).resolve().parent / "default.json"):
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                text = candidate.read_text(encoding="utf-8-sig").strip()
                if text:
                    return candidate
        except OSError:
            continue
    return None


def split_dotted(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    for sep in ("  ·  ", " · ", " ·", "· ", "·"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return left.strip(), right.strip()
    return raw, ""


def join_dotted(left: str, right: str) -> str:
    left, right = (left or "").strip(), (right or "").strip()
    if left and right:
        return f"{left} · {right}"
    return left or right


def split_mmss(text: str) -> tuple[str, str]:
    sec = to_seconds(text) if text else None
    if sec is None:
        raw = (text or "").strip()
        if ":" in raw:
            a, b = raw.split(":", 1)
            return a.strip(), b.strip()
        return "", ""
    return f"{sec // 60:02d}", f"{sec % 60:02d}"


def join_mmss(minutes: str, seconds: str) -> str:
    m = (minutes or "").strip()
    s = (seconds or "").strip()
    if m == "" and s == "":
        return ""
    try:
        mi = int(m or "0")
        se = int(s or "0")
    except ValueError:
        return ""
    if se > 59:
        se = 59
    if mi < 0:
        mi = 0
    return f"{mi:02d}:{se:02d}"


def normalize_titles(data: dict) -> None:
    titles = data.setdefault("titles", {})
    if "header_left" not in titles and "header" in titles:
        titles["header_left"], titles["header_right"] = split_dotted(titles.get("header", ""))
    if "subtitle_left" not in titles and "subtitle" in titles:
        titles["subtitle_left"], titles["subtitle_right"] = split_dotted(titles.get("subtitle", ""))
    titles["header"] = join_dotted(titles.get("header_left", ""), titles.get("header_right", ""))
    titles["subtitle"] = join_dotted(titles.get("subtitle_left", ""), titles.get("subtitle_right", ""))


def to_seconds(text: str) -> Optional[int]:
    if text is None:
        return None
    raw = str(text).strip().replace("–", "-").replace("—", "-")
    if not raw or set(raw) <= {"_"}:
        return None
    m = TIME_RE.match(raw)
    if not m:
        return None
    minutes, seconds = int(m.group(1)), int(m.group(2))
    if seconds > 59:
        return None
    return minutes * 60 + seconds


def fmt_time(sec: Optional[int]) -> str:
    if sec is None:
        return "______"
    if sec < 0:
        sec = 0
    return f"{sec // 60:02d}:{sec % 60:02d}"


def parse_timeline_string(block: str) -> list[dict]:
    rows = []
    for line in str(block).splitlines():
        line = line.strip()
        if not line:
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        start, end, label = m.group(1), m.group(2), m.group(3).strip()
        if end and set(end) <= {"_"}:
            end = ""
        rows.append({"start": start, "end": end or "", "label": label})
    return rows


def timeline_to_string(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        end = row.get("end") or "______"
        label = row.get("label") or ""
        lines.append(f"{row['start']} - {end} {label}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def resolve_category(label: str, categories: list[str], aliases: Optional[dict] = None) -> str:
    text = (label or "").strip()
    aliases = {k: v for k, v in (aliases or {}).items() if not is_blank_category(k)}
    categories = [c for c in categories if not is_blank_category(c)]
    if not text:
        return categories[0] if categories else "Unknown"
    alias_to_name = {str(v).strip(): k for k, v in aliases.items() if str(v).strip()}
    if text in alias_to_name:
        return alias_to_name[text]
    token = text.split()[0]
    if token in alias_to_name:
        return alias_to_name[token]
    if text in categories:
        return text
    hits = [c for c in categories if c.lower() in text.lower()]
    alias_hits = [n for a, n in alias_to_name.items() if a.lower() in text.lower()]
    pool = hits + alias_hits
    if pool:
        return max(pool, key=len)
    return text


def display_label(name: str, aliases: Optional[dict] = None) -> str:
    alias = str((aliases or {}).get(name, "")).strip()
    return alias or name


@dataclass
class Project:
    data: dict
    path: Optional[Path] = None

    @classmethod
    def default(cls) -> "Project":
        path = starter_json_path()
        if path is None:
            raise FileNotFoundError(
                "default.json not found. Keep it next to the script, or bundle it into the exe with "
                "--add-data default.json;."
            )
        proj = cls.load(path)
        proj.path = None
        return proj

    @classmethod
    def load(cls, path: Path) -> "Project":
        raw = Path(path).read_text(encoding="utf-8-sig").strip()
        if not raw:
            raise ValueError(f"JSON file is empty: {path}")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        if isinstance(data.get("timeline"), list):
            data["timeline"] = timeline_to_string(data["timeline"])
        proj = cls(data, Path(path))
        proj.ensure_aliases()
        proj.ensure_reviewed()
        proj.ensure_order()
        normalize_titles(proj.data)
        proj.set_rows(proj.rows())
        return proj

    def ensure_aliases(self) -> None:
        aliases = self.data.setdefault("category_aliases", {})
        for name in self.data.get("category_colors", {}):
            aliases.setdefault(name, aliases.get(name, ""))

    def named_categories(self) -> list[str]:
        return [c for c in self.categories() if not is_blank_category(c)]

    def ensure_reviewed(self) -> str:
        named = self.named_categories()
        current = self.data.get("reviewed_category")
        if current in named:
            return current
        chosen = named[0] if named else ""
        self.data["reviewed_category"] = chosen
        return chosen

    def reviewed(self) -> str:
        return self.ensure_reviewed()

    def set_reviewed(self, name: str) -> None:
        if name not in self.named_categories():
            return
        self.data["reviewed_category"] = name
        order = [name] + [k for k in self.data.get("category_order", self.categories()) if k != name]
        self.data["category_order"] = order
        self.ensure_order()

    def ensure_order(self) -> list[str]:
        colors = self.data.get("category_colors", {})
        raw = [k for k in self.data.get("category_order", []) if k in colors]
        for key in colors:
            if key not in raw:
                raw.append(key)
        reviewed = self.data.get("reviewed_category")
        named = [k for k in raw if not is_blank_category(k)]
        blanks = [k for k in raw if is_blank_category(k)]
        if reviewed in named:
            named = [reviewed] + [k for k in named if k != reviewed]
        order = named + blanks
        self.data["category_order"] = order
        self.data["category_colors"] = {k: colors[k] for k in order}
        aliases = self.data.setdefault("category_aliases", {})
        self.data["category_aliases"] = {k: aliases.get(k, "") for k in order}
        return order

    def touch_category(self, name: str) -> None:
        if not name or is_blank_category(name) or name not in self.categories():
            return
        if name == self.reviewed():
            self.ensure_order()
            return
        order = [k for k in self.data.get("category_order", self.categories()) if k != name]
        reviewed = self.reviewed()
        if reviewed in order:
            order.insert(order.index(reviewed) + 1, name)
        else:
            order.insert(0, name)
        self.data["category_order"] = order
        self.ensure_order()

    def rows(self) -> list[dict]:
        tl = self.data.get("timeline", "")
        if isinstance(tl, list):
            return [dict(r) for r in tl]
        return parse_timeline_string(tl)

    def set_rows(self, rows: list[dict]) -> None:
        pin_first_start(rows)
        ensure_min_duration(rows)
        self.data["timeline"] = timeline_to_string(rows)

    def categories(self) -> list[str]:
        return list(self.data.get("category_colors", {}).keys())

    def aliases(self) -> dict:
        self.ensure_aliases()
        return self.data.get("category_aliases", {})

    def display_of(self, name: str) -> str:
        return display_label(name, self.aliases())

    def display_values(self) -> list[str]:
        return [self.display_of(name) for name in self.named_categories()]

    def color_for(self, label: str) -> str:
        cat = resolve_category(label, self.categories(), self.aliases())
        colors = self.data.get("category_colors", {})
        return colors.get(cat, next(iter(colors.values()), "#94A3B8"))

    def to_json_dict(self) -> dict:
        out = copy.deepcopy(self.data)
        out["timeline"] = timeline_to_string(self.rows())
        return out

    def save(self, path: Optional[Path] = None) -> Path:
        target = path or self.path
        if target is None:
            raise ValueError("No file path")
        target = Path(target)
        target.write_text(json.dumps(self.to_json_dict(), indent=2) + "\n", encoding="utf-8")
        self.path = target
        return target


def compute_stats(project: Project) -> dict:
    """Duration is end-start style on the clock.

    A block that runs until 04:26, with the next block starting at 04:27,
    lasts until 04:27 on the next start → 04:27 - 00:00 = 4:27.
    The last block uses its own end stamp: 23:39 - 21:29 = 2:10.
    Full runtime is last_end - first_start (00:00 to 23:39 = 23:39).
    """
    rows = project.rows()
    cats = project.categories()
    totals: dict[str, int] = {c: 0 for c in cats}
    complete = []
    parsed = []
    for row in rows:
        a, b = to_seconds(row.get("start")), to_seconds(row.get("end"))
        parsed.append((row, a, b))

    for i, (row, a, b) in enumerate(parsed):
        if a is None:
            continue
        nxt = parsed[i + 1][1] if i + 1 < len(parsed) else None
        if nxt is not None:
            dur = nxt - a
            end_s = nxt - 1 if b is None else b
        elif b is not None and b >= a:
            dur = b - a
            end_s = b
        else:
            continue
        if dur < 0:
            continue
        cat = resolve_category(row.get("label", ""), cats, project.aliases())
        totals.setdefault(cat, 0)
        totals[cat] += dur
        complete.append({**row, "start_s": a, "end_s": end_s if end_s is not None else a, "dur": dur, "category": cat})

    total = sum(totals.values())
    reviewed = project.reviewed()
    content = totals.get(reviewed, 0)
    other = total - content
    last_end = complete[-1]["end_s"] if complete else 0
    first_start = complete[0]["start_s"] if complete else 0
    span = last_end - first_start if complete else 0
    return {
        "rows": complete,
        "totals": totals,
        "total": total,
        "reviewed": reviewed,
        "content": content,
        "other": other,
        "span": span,
    }


def render_figure(project: Project):
    ui = project.data["ui_colors"]
    titles = project.data["titles"]
    stats = compute_stats(project)
    rows = stats["rows"]
    totals = stats["totals"]
    cats = [c for c, v in totals.items() if v > 0] or project.categories()
    n_rows = max(len(rows), 1)
    n_cats = max(len(cats), 1)

    footer_h = 0.46
    footer_gap = 0.14
    footer_reserve = footer_h + footer_gap + 0.08
    # Scale canvas so labels never collide. Extra height is for the footer card.
    height = max(14.5, 7.4 + n_rows * 0.36 + n_cats * 0.12) + footer_reserve
    fig = plt.figure(figsize=(12, height), dpi=160, facecolor=ui["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 12)
    ax.set_ylim(0, height)
    ax.axis("off")
    ax.set_facecolor(ui["bg"])

    def card(x, y, w, h, fc=None):
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.02,rounding_size=0.12",
                linewidth=1,
                edgecolor=ui["line"],
                facecolor=fc or ui["card"],
                zorder=1,
            )
        )

    # Header
    ax.add_patch(Rectangle((0, height - 1.45), 12, 1.45, color=ui["bg"], zorder=0))
    content_color = project.color_for(stats["reviewed"]) if stats["reviewed"] else ui.get("content", "#4ADE80")
    ranked_named = sorted(
        ((n, d) for n, d in totals.items() if d > 0 and not is_blank_category(n) and n != stats["reviewed"]),
        key=lambda kv: kv[1],
        reverse=True,
    )
    other_color = project.color_for(ranked_named[0][0]) if ranked_named else ui.get("other", "#FBBF24")
    ax.add_patch(Rectangle((0, height - 1.45), 0.14, 1.45, color=content_color, zorder=1))
    ax.text(0.45, height - 0.38, titles.get("kicker", ""), color=ui["muted"], fontsize=8.2,
            fontweight="bold", fontfamily="DejaVu Sans", va="center")
    ax.text(0.45, height - 0.82, titles.get("header", ""), color=ui["accent"], fontsize=18.5,
            fontweight="bold", fontfamily="DejaVu Sans", va="center")
    total = stats["total"] or 1
    content, other = stats["content"], stats["other"]
    ax.text(0.45, height - 1.22, titles.get("subtitle", ""), color=ui["muted"],
            fontsize=9, fontfamily="DejaVu Sans", va="center")

    # Stat cards
    top = height - 1.70
    card(0.35, top - 1.75, 5.55, 1.75)
    card(6.10, top - 1.75, 5.55, 1.75)
    tag = project.display_of(stats["reviewed"]) if stats["reviewed"] else "—"
    ax.text(3.125, top - 0.42, f"CONTENT {tag}", color=content_color,
            fontsize=9, fontweight="bold", ha="center", fontfamily="DejaVu Sans")
    ax.text(3.125, top - 0.92, fmt_time(content), color=ui["accent"], fontsize=28, fontweight="bold",
            ha="center", fontfamily="DejaVu Sans", va="center")
    ax.text(3.125, top - 1.42, f"{content / total * 100:.1f}% of total", color=ui["muted"],
            fontsize=10, ha="center", fontfamily="DejaVu Sans")
    ax.text(8.875, top - 0.42, f"NOT CONTENT {tag}", color=other_color,
            fontsize=9, fontweight="bold", ha="center", fontfamily="DejaVu Sans")
    ax.text(8.875, top - 0.92, fmt_time(other), color=ui["accent"], fontsize=28, fontweight="bold",
            ha="center", fontfamily="DejaVu Sans", va="center")
    ax.text(8.875, top - 1.42, f"{other / total * 100:.1f}% of total", color=ui["muted"],
            fontsize=10, ha="center", fontfamily="DejaVu Sans")

    # Runtime mix
    mix_y = top - 3.10
    card(0.35, mix_y, 11.30, 1.18)
    ax.text(0.55, mix_y + 0.90, titles.get("runtime_mix", "RUNTIME MIX"), color=ui["muted"],
            fontsize=8, fontweight="bold", fontfamily="DejaVu Sans")
    reviewed = stats["reviewed"]
    others_by_size = [
        n for n, _d in sorted(
            ((n, d) for n, d in totals.items() if d > 0 and n != reviewed and not is_blank_category(n)),
            key=lambda kv: kv[1],
            reverse=True,
        )
    ]
    pack_order = ([reviewed] if reviewed and totals.get(reviewed, 0) > 0 else []) + others_by_size
    x0, bar_y, bar_w, bar_h = 0.55, mix_y + 0.23, 10.90, 0.42
    for name in pack_order:
        dur = totals[name]
        w = bar_w * (dur / total)
        ax.add_patch(Rectangle((x0, bar_y), w, bar_h, color=project.data["category_colors"].get(name, "#64748B"),
                               linewidth=0, zorder=2))
        if w > 1.15:
            ax.text(x0 + w / 2, bar_y + bar_h / 2, name if name != "Music intro" else "OP",
                    color=ui["bg"], fontsize=7.2, fontweight="bold", ha="center", va="center",
                    fontfamily="DejaVu Sans")
        x0 += w

    # Left stack: category totals + callout + bars
    left_top = mix_y - 0.20
    cat_h = max(1.7, 0.70 + n_cats * 0.34)
    card(0.35, left_top - cat_h, 5.55, cat_h)
    ax.text(0.55, left_top - 0.27, titles.get("category_totals", "CATEGORY TOTALS"),
            color=ui["muted"], fontsize=8, fontweight="bold", fontfamily="DejaVu Sans")
    ranked = sorted(
        ((n, d) for n, d in totals.items() if not is_blank_category(n)),
        key=lambda kv: kv[1],
        reverse=True,
    )
    for i, (name, dur) in enumerate(ranked):
        y = left_top - 0.67 - i * 0.34
        col = project.data["category_colors"].get(name, "#64748B")
        ax.add_patch(Rectangle((0.55, y - 0.08), 0.18, 0.18, color=col, linewidth=0))
        ax.text(0.88, y, name, color=ui["text"], fontsize=10, va="center", fontfamily="DejaVu Sans")
        ax.text(3.55, y, fmt_time(dur), color=ui["accent"], fontsize=10, va="center",
                fontfamily="DejaVu Sans", fontweight="bold")
        ax.text(5.55, y, f"{dur / total * 100:.1f}%", color=ui["muted"], fontsize=10,
                va="center", ha="right", fontfamily="DejaVu Sans")

    tl_bottom = footer_reserve + 0.04
    bars_top = left_top - cat_h - 0.20
    bars_h = max(2.6, bars_top - tl_bottom)
    card(0.35, bars_top - bars_h, 5.55, bars_h)
    ax.text(0.55, bars_top - 0.27, titles.get("where_time_goes", "WHERE THE TIME GOES"),
            color=ui["muted"], fontsize=8, fontweight="bold", fontfamily="DejaVu Sans")
    bar_items = [(n, totals[n]) for n in pack_order]
    maxd = max((d for _, d in bar_items), default=1)
    base_y = bars_top - bars_h + 0.55
    max_h = bars_h - 1.35
    slot = 5.15 / max(len(bar_items), 1)
    for i, (name, dur) in enumerate(bar_items):
        x = 0.55 + i * slot
        bw = min(0.88, slot * 0.72)
        h = max_h * (dur / maxd)
        ax.add_patch(FancyBboxPatch((x, base_y), bw, h, boxstyle="round,pad=0.01,rounding_size=0.06",
                                    linewidth=0, facecolor=project.data["category_colors"].get(name, "#64748B"),
                                    zorder=2))
        ax.text(x + bw / 2, base_y + h + 0.10, fmt_time(dur), color=ui["accent"], fontsize=8,
                ha="center", va="bottom", fontfamily="DejaVu Sans", fontweight="bold")
        ax.text(x + bw / 2, base_y - 0.16, name.replace(" ", "\n"), color=ui["muted"], fontsize=7.0,
                ha="center", va="top", fontfamily="DejaVu Sans")

    # Timeline
    tl_top = mix_y - 0.20
    card(6.10, tl_bottom, 5.55, tl_top - tl_bottom)
    ax.text(6.30, tl_top - 0.27, titles.get("full_timeline", "FULL TIMELINE"),
            color=ui["muted"], fontsize=8, fontweight="bold", fontfamily="DejaVu Sans")
    ax.text(11.40, tl_top - 0.27, "start – end", color=ui["muted"], fontsize=7.5,
            ha="right", fontfamily="DejaVu Sans")
    usable = (tl_top - tl_bottom) - 0.55
    step = usable / max(n_rows, 1)
    step = min(step, 0.40)
    for i, row in enumerate(rows):
        y = tl_top - 0.67 - i * step
        col = project.color_for(row.get("label", ""))
        if i % 2 == 0:
            ax.add_patch(Rectangle((6.22, y - step * 0.38), 5.20, step * 0.86,
                                   color=ui["zebra"], linewidth=0, zorder=2))
        ax.add_patch(Rectangle((6.30, y - 0.07), 0.14, 0.14, color=col, linewidth=0, zorder=3))
        ax.text(6.55, y, f"{fmt_time(row['start_s'])} – {fmt_time(row['end_s'])}",
                color="#CBD5E1", fontsize=min(8.1, 8.1 * step / 0.36), va="center",
                fontfamily="DejaVu Sans Mono", zorder=3)
        ax.text(11.40, y, row.get("label", ""), color=ui["text"],
                fontsize=min(8.1, 8.1 * step / 0.36), va="center", ha="right",
                fontfamily="DejaVu Sans", zorder=3)

    footer = titles.get("footer", "Make your own Timelines: www.github.com....")
    card(0.35, footer_gap, 11.30, footer_h)
    ax.text(6.0, footer_gap + footer_h / 2, footer, color=ui.get("muted", "#93A0B8"),
            fontsize=8.0, ha="center", va="center", fontfamily="DejaVu Sans", zorder=5)

    return fig


# ---------------------------------------------------------------------------
# Timeline banding
# ---------------------------------------------------------------------------

def pin_first_start(rows: list[dict]) -> list[dict]:
    if rows:
        rows[0]["start"] = "00:00"
    return rows


def ensure_min_duration(rows: list[dict]) -> list[dict]:
    """Every completed row lasts at least 1 second (00:22–00:22 → 00:22–00:23)."""
    for i, row in enumerate(rows):
        start = to_seconds(row.get("start"))
        end = to_seconds(row.get("end"))
        if start is None or end is None:
            continue
        if end < start + 1:
            end = start + 1
            row["end"] = fmt_time(end)
        if i + 1 < len(rows):
            nxt = to_seconds(rows[i + 1].get("start"))
            if nxt is None or nxt <= end:
                rows[i + 1]["start"] = fmt_time(end + 1)
    return rows


def min_start_for_index(index: int) -> int:
    return max(0, index * 2)


def squeeze_before(rows: list[dict], index: int, desired_start: int) -> int:
    """Shrink earlier rows only as far as needed. Each prior row keeps ≥1s.
    Minimum start for this row is index * 2 (00:00-01, 00:02-03, ...)."""
    desired_start = max(desired_start, min_start_for_index(index))
    cursor = desired_start
    for i in range(index - 1, -1, -1):
        max_end = cursor - 1
        start_i = to_seconds(rows[i].get("start"))
        end_i = to_seconds(rows[i].get("end"))
        if start_i is None:
            start_i = min_start_for_index(i)
        if end_i is None:
            end_i = start_i + 1
        if start_i + 1 <= max_end:
            rows[i]["start"] = fmt_time(start_i)
            rows[i]["end"] = fmt_time(max_end if end_i > max_end else end_i)
            if to_seconds(rows[i]["end"]) < start_i + 1:
                rows[i]["end"] = fmt_time(start_i + 1)
            cursor = to_seconds(rows[i]["start"])
        else:
            start_i = max(min_start_for_index(i), max_end - 1)
            rows[i]["start"] = fmt_time(start_i)
            rows[i]["end"] = fmt_time(max_end)
            cursor = start_i
    if index > 0:
        prev_end = to_seconds(rows[index - 1].get("end")) or 0
        desired_start = max(desired_start, prev_end + 1)
    return desired_start


def push_after(rows: list[dict], index: int) -> None:
    """If this row now overlaps the next ones, move them forward.
    The first overlapping row is compressed to 1s; later rows keep their
    end when possible and only move start."""
    end = to_seconds(rows[index].get("end"))
    if end is None:
        return
    cursor = end + 1
    first_overlap = True
    for j in range(index + 1, len(rows)):
        start_j = to_seconds(rows[j].get("start"))
        end_j = to_seconds(rows[j].get("end"))
        if start_j is None:
            rows[j]["start"] = fmt_time(cursor)
            return
        if start_j >= cursor:
            return
        rows[j]["start"] = fmt_time(cursor)
        if end_j is None:
            return
        if first_overlap:
            rows[j]["end"] = fmt_time(cursor + 1)
            first_overlap = False
        else:
            rows[j]["end"] = fmt_time(max(end_j, cursor + 1))
        cursor = to_seconds(rows[j]["end"]) + 1


def apply_time_edit(rows: list[dict], index: int, field: str, new_sec: int) -> list[dict]:
    if not rows or index < 0 or index >= len(rows):
        return rows
    pin_first_start(rows)
    old_s = to_seconds(rows[index].get("start")) or 0
    old_e = to_seconds(rows[index].get("end"))

    if field == "start":
        if index == 0:
            new_sec = 0
        else:
            new_sec = squeeze_before(rows, index, new_sec)
        rows[index]["start"] = fmt_time(new_sec)
        if old_e is not None:
            rows[index]["end"] = fmt_time(max(old_e, new_sec + 1))
        push_after(rows, index)
    else:
        start = to_seconds(rows[index].get("start")) or 0
        if new_sec <= start:
            new_sec = start + 1
        rows[index]["end"] = fmt_time(new_sec)
        push_after(rows, index)
    return ensure_min_duration(pin_first_start(rows))


def apply_band(rows: list[dict], index: int, new_start: Optional[int], new_end: Optional[int]) -> list[dict]:
    if new_start is not None:
        apply_time_edit(rows, index, "start", new_start)
    if new_end is not None:
        apply_time_edit(rows, index, "end", new_end)
    return rows


def add_row(rows: list[dict], categories: list[str], default_label: Optional[str] = None) -> list[dict]:
    if rows:
        last = rows[-1]
        last_start = to_seconds(last.get("start")) or 0
        last_end = to_seconds(last.get("end"))
        if last_end is None:
            last_end = last_start + 1
            last["end"] = fmt_time(last_end)
        start = last_end + 1
    else:
        start = 0
    label = default_label or (categories[0] if categories else "")
    rows.append({"start": fmt_time(start), "end": "", "label": label})
    return rows


def delete_row(rows: list[dict], index: int) -> list[dict]:
    if index < 0 or index >= len(rows):
        return rows
    if 0 < index < len(rows) - 1:
        nxt = to_seconds(rows[index + 1].get("start"))
        if nxt is not None:
            rows[index - 1]["end"] = fmt_time(nxt - 1)
    del rows[index]
    return pin_first_start(rows)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def clear_children(widget) -> None:
    for child in widget.winfo_children():
        child.destroy()


def bind_commit(widget, callback) -> None:
    widget.bind("<FocusOut>", lambda _e: callback())
    widget.bind("<Return>", lambda _e: callback())


def _wheel_delta(event) -> int:
    if getattr(event, "num", None) == 4:
        return -1
    if getattr(event, "num", None) == 5:
        return 1
    delta = getattr(event, "delta", 0) or 0
    if sys.platform == "darwin":
        return -int(delta)
    return -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)


class StudioApp:
    def __init__(self, project: Project):
        if not HAS_TK:
            raise RuntimeError("tkinter is not available")
        self.project = project
        self.root = tk.Tk()
        self.root.title("Episode Runtime Studio")
        self.root.geometry("1100x820")
        self.root.minsize(860, 640)
        self._building = False
        self._preview_win = None
        self._preview_fig = None
        self._undo = []
        self._redo = []
        self._after_nav = False
        self._time_focus = None
        self._pending_focus = None
        self._time_widgets = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New", command=self.on_new)
        file_menu.add_command(label="Import JSON…", command=self.on_import)
        file_menu.add_command(label="Export JSON…", command=self.on_export)
        file_menu.add_command(label="Save", command=self.on_save)
        file_menu.add_separator()
        file_menu.add_command(label="Undo", command=self.on_undo, accelerator="Ctrl+Z")
        file_menu.add_command(label="Redo", command=self.on_redo, accelerator="Ctrl+Y")
        file_menu.add_separator()
        file_menu.add_command(label="Open Preview", command=self.on_preview)
        file_menu.add_command(label="Export PNG…", command=self.on_png)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)

        self.main_wrap = ttk.Frame(self.root)
        self.main_wrap.pack(fill="both", expand=True)

        toolbar = ttk.Frame(self.main_wrap, padding=6)
        toolbar.pack(fill="x")
        for text, cmd in [
            ("New", self.on_new),
            ("Import", self.on_import),
            ("Export", self.on_export),
            ("Save", self.on_save),
            ("Undo", self.on_undo),
            ("Redo", self.on_redo),
            ("Preview", self.on_preview),
            ("PNG", self.on_png),
        ]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side="left", padx=3)
        self.root.bind_all("<Control-z>", lambda _e: self.on_undo())
        self.root.bind_all("<Control-Z>", lambda _e: self.on_undo())
        self.root.bind_all("<Control-y>", lambda _e: self.on_redo())
        self.root.bind_all("<Control-Y>", lambda _e: self.on_redo())
        self.root.bind_all("<Control-Shift-Z>", lambda _e: self.on_redo())
        self.root.bind_all("<Button-1>", self._on_global_click, add="+")

        titles = ttk.LabelFrame(self.main_wrap, text="Titles", padding=6)
        titles.pack(fill="x", padx=8, pady=4)
        self.title_vars = {}
        grid = ttk.Frame(titles)
        grid.pack(fill="x")
        ttk.Label(grid, text="kicker").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        kicker = tk.StringVar(value=self.project.data["titles"].get("kicker", ""))
        ttk.Entry(grid, textvariable=kicker).grid(row=0, column=1, columnspan=3, sticky="ew", padx=4, pady=2)
        kicker.trace_add("write", lambda *_a, v=kicker: self._title_changed("kicker", v))
        self.title_vars["kicker"] = kicker

        ttk.Label(grid, text="header").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        h1 = tk.StringVar(value=self.project.data["titles"].get("header_left", ""))
        h2 = tk.StringVar(value=self.project.data["titles"].get("header_right", ""))
        ttk.Entry(grid, textvariable=h1).grid(row=1, column=1, sticky="ew", padx=(4, 2), pady=2)
        ttk.Label(grid, text="·").grid(row=1, column=2, padx=2)
        ttk.Entry(grid, textvariable=h2).grid(row=1, column=3, sticky="ew", padx=(2, 4), pady=2)
        h1.trace_add("write", lambda *_a: self._pair_title("header", h1, h2))
        h2.trace_add("write", lambda *_a: self._pair_title("header", h1, h2))
        self.title_vars["header_left"] = h1
        self.title_vars["header_right"] = h2

        ttk.Label(grid, text="subtitle").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        s1 = tk.StringVar(value=self.project.data["titles"].get("subtitle_left", ""))
        s2 = tk.StringVar(value=self.project.data["titles"].get("subtitle_right", ""))
        ttk.Entry(grid, textvariable=s1).grid(row=2, column=1, sticky="ew", padx=(4, 2), pady=2)
        ttk.Label(grid, text="·").grid(row=2, column=2, padx=2)
        ttk.Entry(grid, textvariable=s2).grid(row=2, column=3, sticky="ew", padx=(2, 4), pady=2)
        s1.trace_add("write", lambda *_a: self._pair_title("subtitle", s1, s2))
        s2.trace_add("write", lambda *_a: self._pair_title("subtitle", s1, s2))
        self.title_vars["subtitle_left"] = s1
        self.title_vars["subtitle_right"] = s2
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        body = ttk.Frame(self.main_wrap)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        cat_frame = ttk.LabelFrame(body, text="Categories", padding=6)
        cat_frame.pack(fill="x")
        cat_head = ttk.Frame(cat_frame)
        cat_head.pack(fill="x")
        ttk.Label(cat_head, text="Review", width=7).pack(side="left")
        ttk.Label(cat_head, text="", width=3).pack(side="left")
        ttk.Label(cat_head, text="Name", width=18).pack(side="left")
        ttk.Label(cat_head, text="Alias", width=14).pack(side="left")
        ttk.Label(cat_head, text="Remove").pack(side="left")
        self.cat_wrap = ttk.Frame(cat_frame)
        self.cat_wrap.pack(fill="x")
        self.cat_canvas = tk.Canvas(self.cat_wrap, highlightthickness=0, height=5 * 38)
        self.cat_scroll = ttk.Scrollbar(self.cat_wrap, orient="vertical", command=self.cat_canvas.yview)
        self.cat_list = ttk.Frame(self.cat_canvas)
        self.cat_list.bind(
            "<Configure>",
            lambda e: self.cat_canvas.configure(scrollregion=self.cat_canvas.bbox("all")),
        )
        self.cat_canvas.create_window((0, 0), window=self.cat_list, anchor="nw")
        self.cat_canvas.configure(yscrollcommand=self.cat_scroll.set)
        self.cat_canvas.pack(side="left", fill="x", expand=True)
        self.cat_scroll.pack(side="right", fill="y")
        ttk.Button(cat_frame, text="+ Add category", command=self.add_category_dialog).pack(anchor="w", pady=4)

        self.tl_frame = ttk.LabelFrame(body, text="Timeline", padding=6)
        self.tl_frame.pack(fill="both", expand=True, pady=6)
        header = ttk.Frame(self.tl_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Start").pack(side="left", padx=(0, 28))
        ttk.Label(header, text="End").pack(side="left", padx=(0, 28))
        ttk.Label(header, text="Category / label").pack(side="left")
        self.tl_canvas = tk.Canvas(self.tl_frame, highlightthickness=0)
        tl_scroll = ttk.Scrollbar(self.tl_frame, orient="vertical", command=self.tl_canvas.yview)
        self.tl_inner = ttk.Frame(self.tl_canvas)
        self.tl_inner.bind(
            "<Configure>",
            lambda e: self.tl_canvas.configure(scrollregion=self.tl_canvas.bbox("all")),
        )
        self.tl_canvas.create_window((0, 0), window=self.tl_inner, anchor="nw")
        self.tl_canvas.configure(yscrollcommand=tl_scroll.set)
        self.tl_canvas.pack(side="left", fill="both", expand=True)
        tl_scroll.pack(side="right", fill="y")
        ttk.Button(body, text="+ Add timestamp", command=self.on_add_row).pack(anchor="w", pady=(0, 8))

        self.stats_var = tk.StringVar(value="")
        ttk.Label(self.main_wrap, textvariable=self.stats_var, padding=8).pack(anchor="w")
        self._bind_global_scroll()

    def _pointer_in(self, widget) -> bool:
        try:
            x, y = widget.winfo_pointerxy()
            left, top = widget.winfo_rootx(), widget.winfo_rooty()
            return left <= x <= left + widget.winfo_width() and top <= y <= top + widget.winfo_height()
        except tk.TclError:
            return False

    def _bind_global_scroll(self) -> None:
        def wheel(event):
            if self._pointer_in(self.tl_frame) or self._pointer_in(self.tl_canvas) or self._pointer_in(self.tl_inner):
                self.tl_canvas.yview_scroll(_wheel_delta(event), "units")
                return "break"
            if self._pointer_in(self.cat_wrap) or self._pointer_in(self.cat_canvas) or self._pointer_in(self.cat_list):
                self.cat_canvas.yview_scroll(_wheel_delta(event), "units")
                return "break"

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, wheel)

    def _title_changed(self, key: str, var: tk.StringVar) -> None:
        self.project.data["titles"][key] = var.get()

    def _pair_title(self, base: str, left: tk.StringVar, right: tk.StringVar) -> None:
        titles = self.project.data["titles"]
        titles[f"{base}_left"] = left.get()
        titles[f"{base}_right"] = right.get()
        titles[base] = join_dotted(left.get(), right.get())

    def sync_title_fields(self) -> None:
        titles = self.project.data["titles"]
        normalize_titles(self.project.data)
        mapping = {
            "kicker": titles.get("kicker", ""),
            "header_left": titles.get("header_left", ""),
            "header_right": titles.get("header_right", ""),
            "subtitle_left": titles.get("subtitle_left", ""),
            "subtitle_right": titles.get("subtitle_right", ""),
        }
        for key, value in mapping.items():
            if key in self.title_vars:
                self.title_vars[key].set(value)

    def refresh(self) -> None:
        y = 0.0
        try:
            y = self.tl_canvas.yview()[0]
        except tk.TclError:
            pass
        self._building = True
        self.rebuild_categories()
        self.rebuild_timeline()
        self._building = False
        self.update_stats_label()
        try:
            self.tl_canvas.yview_moveto(y)
        except tk.TclError:
            pass
        pending = self._pending_focus
        self._pending_focus = None
        if pending:
            self.root.after_idle(lambda p=pending: self._restore_time_focus(p))

    def _snap(self) -> dict:
        return copy.deepcopy(self.project.data)

    def _same(self, a: dict, b: dict) -> bool:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def _push_history(self, before: dict) -> bool:
        if self._same(before, self.project.data):
            return False
        if self._after_nav:
            self._undo = [before]
            self._redo.clear()
            self._after_nav = False
            return True
        self._undo.append(before)
        self._undo = self._undo[-3:]
        self._redo.clear()
        return True

    def _restore(self, data: dict) -> None:
        self.project.data = copy.deepcopy(data)
        self.project.ensure_aliases()
        self.project.ensure_reviewed()
        self.project.ensure_order()
        normalize_titles(self.project.data)
        self.sync_title_fields()
        self.refresh()

    def on_undo(self) -> None:
        if not self._undo:
            return
        current = self._snap()
        self._redo.append(current)
        self._redo = self._redo[-3:]
        self._after_nav = True
        self._restore(self._undo.pop())

    def on_redo(self) -> None:
        if not self._redo:
            return
        current = self._snap()
        self._undo.append(current)
        self._undo = self._undo[-3:]
        self._after_nav = True
        self._restore(self._redo.pop())

    def rebuild_categories(self) -> None:
        clear_children(self.cat_list)
        self.project.ensure_order()
        aliases = self.project.aliases()
        self.review_var = tk.StringVar(value=self.project.reviewed())
        for name, color in self.project.data["category_colors"].items():
            row = ttk.Frame(self.cat_list)
            row.pack(fill="x", pady=3)
            radio = ttk.Radiobutton(
                row,
                variable=self.review_var,
                value=name,
                command=lambda n=name: self.on_review(n),
                state="disabled" if is_blank_category(name) else "normal",
            )
            radio.pack(side="left", padx=(0, 4))
            swatch = tk.Canvas(row, width=22, height=22, highlightthickness=1, highlightbackground="#333")
            swatch.create_rectangle(0, 0, 22, 22, fill=color, outline="")
            swatch.pack(side="left", padx=(0, 6))
            swatch.bind("<Button-1>", lambda _e, n=name: self.pick_color(n))
            name_var = tk.StringVar(value="" if is_blank_category(name) else name)
            ent = ttk.Entry(row, textvariable=name_var, width=18)
            ent.pack(side="left")
            bind_commit(ent, lambda old=name, v=name_var: self.rename_category(old, v.get()))
            alias_var = tk.StringVar(value=aliases.get(name, ""))
            alias_ent = ttk.Entry(row, textvariable=alias_var, width=14)
            alias_ent.pack(side="left", padx=(8, 4))
            bind_commit(alias_ent, lambda n=name, v=alias_var: self.set_alias(n, v.get()))
            ttk.Button(row, text="X", width=2, command=lambda n=name: self.remove_category(n)).pack(side="left")
        self.cat_canvas.update_idletasks()
        self.cat_canvas.configure(scrollregion=self.cat_canvas.bbox("all") or (0, 0, 0, 0))

    def on_review(self, name: str) -> None:
        if is_blank_category(name):
            return
        if self.project.reviewed() == name:
            return
        before = self._snap()
        self.project.set_reviewed(name)
        if self._push_history(before):
            self.refresh()

    def rebuild_timeline(self) -> None:
        clear_children(self.tl_inner)
        self._time_widgets = {}
        self._time_focus = None
        rows = self.project.rows()
        displays = self.project.display_values()
        for i, row in enumerate(rows):
            fr = ttk.Frame(self.tl_inner)
            fr.pack(fill="x", pady=1)
            sm, ss = split_mmss(row.get("start", ""))
            em, es = split_mmss(row.get("end") or "")
            sm_var, ss_var = tk.StringVar(value=sm), tk.StringVar(value=ss)
            em_var, es_var = tk.StringVar(value=em), tk.StringVar(value=es)

            def mmss_entry(parent, var):
                ent = tk.Entry(
                    parent,
                    textvariable=var,
                    width=3,
                    justify="center",
                    highlightthickness=2,
                    highlightbackground="#CBD5E1",
                    highlightcolor="#CBD5E1",
                    relief="solid",
                    bd=1,
                )
                ent.pack(side="left", padx=0)
                return ent

            sm_ent = mmss_entry(fr, sm_var)
            s_colon = ttk.Label(fr, text=":")
            s_colon.pack(side="left", padx=1)
            ss_ent = mmss_entry(fr, ss_var)
            if i == 0:
                sm_var.set("00")
                ss_var.set("00")
                sm_ent.configure(state="disabled")
                ss_ent.configure(state="disabled")
            ttk.Label(fr, text="➜").pack(side="left", padx=3)
            em_ent = mmss_entry(fr, em_var)
            e_colon = ttk.Label(fr, text=":")
            e_colon.pack(side="left", padx=1)
            ee_ent = mmss_entry(fr, es_var)
            s_colon._tl_meta = (i, "start", "m")
            e_colon._tl_meta = (i, "end", "m")
            combo = ttk.Combobox(fr, values=displays, width=22, state="readonly")
            raw = row.get("label") or ""
            cat = resolve_category(raw, self.project.categories(), self.project.aliases())
            combo.set(self.project.display_of(cat) if cat in self.project.categories() else raw)
            combo.pack(side="left", padx=4)
            ttk.Button(fr, text="X", width=2, command=lambda idx=i: self.on_delete_row(idx)).pack(side="left")
            self._bind_time_pair(i, "start", (sm_ent, ss_ent), sm_var, ss_var)
            self._bind_time_pair(i, "end", (em_ent, ee_ent), em_var, es_var)
            combo.bind("<<ComboboxSelected>>", lambda _e, idx=i, c=combo: self.on_edit_label(idx, c.get()))
        self.tl_canvas.update_idletasks()
        self.tl_canvas.configure(scrollregion=self.tl_canvas.bbox("all") or (0, 0, 0, 0))

    def _paint_pair(self, ents, on: bool) -> None:
        color = "#E11D48" if on else "#CBD5E1"
        for ent in ents:
            try:
                ent.configure(highlightbackground=color, highlightcolor=color)
            except tk.TclError:
                pass

    def _bind_time_pair(self, index: int, field: str, ents, m_var, s_var) -> None:
        for part, ent in zip(("m", "s"), ents):
            ent._tl_meta = (index, field, part)
            self._time_widgets[(index, field, part)] = ent
            ent.bind("<FocusIn>", lambda _e, i=index, f=field, es=ents, mv=m_var, sv=s_var: self._activate_time_focus(i, f, es, mv, sv))
            ent.bind("<Return>", lambda _e, i=index, f=field, mv=m_var, sv=s_var: self._commit_time_focus())

    def _activate_time_focus(self, index, field, ents, m_var, s_var) -> None:
        if self._building:
            return
        cur = self._time_focus
        if cur and cur["ents"] == ents:
            self._paint_pair(ents, True)
            return
        if cur:
            self._commit_time_focus()
            try:
                alive = bool(ents) and ents[0].winfo_exists()
            except tk.TclError:
                alive = False
            if not alive:
                return
        self._time_focus = {
            "index": index,
            "field": field,
            "ents": ents,
            "m_var": m_var,
            "s_var": s_var,
        }
        self._paint_pair(ents, True)

    def _commit_time_focus(self) -> None:
        cur = self._time_focus
        if not cur:
            return
        self._time_focus = None
        self._paint_pair(cur["ents"], False)
        changed = self.on_edit_time(cur["index"], cur["field"], join_mmss(cur["m_var"].get(), cur["s_var"].get()))
        if not changed:
            self._pending_focus = None

    def _restore_time_focus(self, pending) -> None:
        ent = self._time_widgets.get(pending)
        if ent is None:
            return
        try:
            ent.focus_set()
        except tk.TclError:
            return

    def _on_global_click(self, event) -> None:
        if self._building or self._time_focus is None:
            return
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return
        except tk.TclError:
            return
        w = event.widget
        meta = getattr(w, "_tl_meta", None)
        cur = self._time_focus
        if w in cur["ents"] or (meta and meta[0] == cur["index"] and meta[1] == cur["field"]):
            return
        if meta:
            self._pending_focus = meta
        self._commit_time_focus()

    def on_edit_time(self, index: int, field: str, value: str) -> bool:
        if self._building:
            return False
        if index == 0 and field == "start":
            return False
        rows = self.project.rows()
        before = self._snap()
        if field == "end" and (value.strip() == "" or set(value.strip()) <= {"_"}):
            if rows[index].get("end") == "":
                return False
            rows[index]["end"] = ""
            self.project.set_rows(rows)
            if self._push_history(before):
                self.refresh()
                return True
            return False
        sec = to_seconds(value)
        if sec is None:
            messagebox.showerror("Invalid time", f"Could not parse '{value}'. Use MM:SS.")
            self.refresh()
            return True
        current = to_seconds(rows[index].get(field))
        if current == sec:
            return False
        apply_band(rows, index, sec if field == "start" else None, sec if field == "end" else None)
        self.project.set_rows(rows)
        self._push_history(before)
        self.refresh()
        return True

    def on_edit_label(self, index: int, label: str) -> None:
        rows = self.project.rows()
        if not (0 <= index < len(rows)):
            return
        if rows[index].get("label") == label.strip():
            return
        before = self._snap()
        rows[index]["label"] = label.strip()
        self.project.set_rows(rows)
        if self._push_history(before):
            self.update_stats_label()

    def on_add_row(self) -> None:
        before = self._snap()
        cats = self.project.categories()
        named = self.project.named_categories()
        default = self.project.display_of(named[0]) if named else ""
        rows = add_row(self.project.rows(), cats, default)
        self.project.set_rows(rows)
        if self._push_history(before):
            self.refresh()

    def on_delete_row(self, index: int) -> None:
        before = self._snap()
        rows = delete_row(self.project.rows(), index)
        self.project.set_rows(rows)
        if self._push_history(before):
            self.refresh()

    def pick_color(self, name: str) -> None:
        current = self.project.data["category_colors"].get(name, "#888888")
        picked = colorchooser.askcolor(color=current, title=f"Color for {name}")
        if picked and picked[1] and picked[1] != current:
            before = self._snap()
            self.project.data["category_colors"][name] = picked[1]
            self.project.touch_category(name)
            if self._push_history(before):
                self.refresh()

    def rename_category(self, old: str, new: str) -> None:
        new = new.strip()
        display_old = "" if is_blank_category(old) else old
        if new == display_old:
            return
        colors = self.project.data["category_colors"]
        if old not in colors:
            return
        if not new:
            if self.category_in_use(old):
                messagebox.showerror(
                    "Category in use",
                    "You cannot empty this category because its being used in the timeline",
                )
                self.refresh()
                return
            new = next_blank_id(list(colors.keys()))
        if new in colors and new != old:
            messagebox.showerror("Exists", f"Category '{new}' already exists.")
            self.refresh()
            return
        before = self._snap()
        colors[new] = colors.pop(old)
        aliases = self.project.aliases()
        aliases[new] = aliases.pop(old, "")
        order = self.project.data.get("category_order", [])
        self.project.data["category_order"] = [new if k == old else k for k in order]
        if old not in order:
            self.project.data["category_order"].append(new)
        if self.project.data.get("reviewed_category") == old:
            if is_blank_category(new):
                self.project.ensure_reviewed()
            else:
                self.project.set_reviewed(new)
        if not is_blank_category(old) and not is_blank_category(new):
            self._relabel_category(old, new)
        if is_blank_category(new):
            self.project.ensure_order()
        else:
            self.project.touch_category(new)
        if self._push_history(before):
            self.refresh()
        else:
            self.refresh()

    def set_alias(self, name: str, alias: str) -> None:
        alias = alias.strip()
        aliases = self.project.aliases()
        old_display = self.project.display_of(name)
        if aliases.get(name, "") == alias:
            return
        before = self._snap()
        aliases[name] = alias
        new_display = self.project.display_of(name)
        if old_display != new_display:
            self._relabel_category(name, name, old_display=old_display, new_display=new_display)
        self.project.touch_category(name)
        if self._push_history(before):
            self.refresh()
        else:
            self.refresh()

    def _relabel_category(self, old_name: str, new_name: str, old_display: Optional[str] = None,
                          new_display: Optional[str] = None) -> None:
        aliases = self.project.aliases()
        old_disp = old_display if old_display is not None else old_name
        new_disp = new_display if new_display is not None else self.project.display_of(new_name)
        rows = self.project.rows()
        for row in rows:
            label = row.get("label") or ""
            cat = resolve_category(label, list(self.project.data["category_colors"].keys()) + [old_name], aliases)
            if cat == old_name or label == old_disp or label.startswith(old_disp + " ") or label.startswith(old_name + " "):
                suffix = ""
                for prefix in (old_disp, old_name):
                    if label == prefix:
                        row["label"] = new_disp
                        break
                    if label.startswith(prefix + " "):
                        suffix = label[len(prefix):]
                        row["label"] = new_disp + suffix
                        break
                else:
                    if cat == old_name:
                        row["label"] = new_disp
        self.project.set_rows(rows)

    def category_in_use(self, name: str) -> bool:
        cats = self.project.categories()
        aliases = self.project.aliases()
        disp = self.project.display_of(name)
        for row in self.project.rows():
            label = row.get("label") or ""
            if not label:
                continue
            if resolve_category(label, cats, aliases) == name:
                return True
            if label == name or label == disp:
                return True
        return False

    def remove_category(self, name: str) -> None:
        if self.category_in_use(name):
            messagebox.showerror(
                "Category in use",
                "Error: A timestamp is currently using this category",
            )
            return
        before = self._snap()
        self.project.data["category_colors"].pop(name, None)
        self.project.aliases().pop(name, None)
        order = self.project.data.get("category_order", [])
        self.project.data["category_order"] = [k for k in order if k != name]
        self.project.ensure_reviewed()
        self.project.ensure_order()
        if self._push_history(before):
            self.refresh()

    def add_category_dialog(self) -> None:
        self.add_category()

    def add_category(self) -> None:
        before = self._snap()
        colors = self.project.data["category_colors"]
        if not colors:
            key = "NewCategory"
        else:
            key = next_blank_id(list(colors.keys()))
        colors[key] = next_category_color(list(colors.values()))
        self.project.aliases()[key] = ""
        order = self.project.data.setdefault("category_order", list(colors.keys()))
        if key not in order:
            order.append(key)
        if not is_blank_category(key) and not self.project.reviewed():
            self.project.set_reviewed(key)
        self.project.ensure_reviewed()
        self.project.ensure_order()
        if self._push_history(before):
            self.refresh()

    def update_stats_label(self) -> None:
        st = compute_stats(self.project)
        total = st["total"] or 1
        tag = self.project.display_of(st["reviewed"]) if st["reviewed"] else "—"
        self.stats_var.set(
            f"Full runtime: {fmt_time(st['total'])}  (100%)"
            f"      Content {tag}: {fmt_time(st['content'])}  ({st['content']/total*100:.1f}%)"
            f"      Not content {tag}: {fmt_time(st['other'])}  ({st['other']/total*100:.1f}%)"
        )

    def close_preview(self) -> None:
        if self._preview_fig is not None:
            plt.close(self._preview_fig)
            self._preview_fig = None
        if self._preview_win is not None:
            try:
                self._preview_win.destroy()
            except tk.TclError:
                pass
            self._preview_win = None

    def on_import(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            self.project = Project.load(Path(path))
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        self._undo.clear()
        self._redo.clear()
        self._after_nav = False
        self.sync_title_fields()
        self.refresh()

    def on_new(self) -> None:
        self.project = Project.default()
        self.project.path = None
        self._undo.clear()
        self._redo.clear()
        self._after_nav = False
        self.sync_title_fields()
        self.refresh()

    def on_export(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        self.project.save(Path(path))
        messagebox.showinfo("Exported", f"Wrote {path}")

    def on_save(self) -> None:
        if self.project.path is None:
            self.on_export()
            return
        self.project.save()
        messagebox.showinfo("Saved", f"Updated {self.project.path}")

    def on_png(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if not path:
            return
        fig = render_figure(self.project)
        fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
        plt.close(fig)
        messagebox.showinfo("PNG", f"Wrote {path}")

    def on_preview(self) -> None:
        self.close_preview()
        fig = render_figure(self.project)
        buf = io.BytesIO()
        fig.savefig(buf, dpi=160, facecolor=fig.get_facecolor(), format="png")
        plt.close(fig)
        buf.seek(0)
        from PIL import Image, ImageTk
        source = Image.open(buf).convert("RGBA")
        src_w, src_h = source.size

        win = tk.Toplevel(self.root)
        win.title(f"Preview  ({src_w}×{src_h} px)")
        win.geometry("980x720")
        self._preview_win = win
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Close Preview", command=self.close_preview).pack(side="left")
        ttk.Label(bar, text=f"Native PNG  {src_w}×{src_h}  ·  scaled to fit").pack(side="left", padx=12)
        holder = ttk.Frame(win, padding=12)
        holder.pack(fill="both", expand=True)
        img_label = ttk.Label(holder)
        img_label.pack(expand=True)
        win.protocol("WM_DELETE_WINDOW", self.close_preview)
        self._preview_photo = None

        def fit(_event=None):
            avail_w = max(holder.winfo_width() - 8, 100)
            avail_h = max(holder.winfo_height() - 8, 100)
            scale = min(avail_w / src_w, avail_h / src_h, 1.0)
            new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
            shown = source.resize(new_size, Image.Resampling.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(shown)
            img_label.configure(image=self._preview_photo)

        holder.bind("<Configure>", fit)
        win.after(50, fit)

    def run(self) -> None:
        self.root.mainloop()


def export_png(project: Project, path: Path) -> None:
    matplotlib.use("Agg", force=False)
    fig = render_figure(project)
    fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Episode Runtime Studio")
    parser.add_argument("--json", type=Path, help="Load this project JSON")
    parser.add_argument("--png", type=Path, help="Render PNG and exit")
    args = parser.parse_args(argv)

    if args.json:
        project = Project.load(args.json)
    else:
        project = Project.default()
    if args.png:
        export_png(project, args.png)
        print(args.png)
        return 0

    if not HAS_TK:
        print("tkinter is missing. Install python3-tk, or use --png to render without a GUI.", file=sys.stderr)
        return 1
    StudioApp(project).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
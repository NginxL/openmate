"""The 今天 page's blocks as the user arranged them: their order, which are hidden, and which take a whole row.

Kept in the extension's own store (meta "today_layout", JSON), changed on the page itself (调整布局). A block this
version knows and the stored layout does not (a newer version added it) takes its default place; one the stored
layout names and this version no longer has is dropped. Whole row / half row only shows from UI version 8 (layout
columns); below that every block is a whole row, and the choice is kept for when the hub draws columns.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from openmate_core.ui import row

KEY = "today_layout"
# (id, title, whole row by default, what it shows)
BLOCKS = (
    ("status", "状态", True, "需要我、日程、飞书、Agent、中转站，各一块，带状态标签"),
    ("needs", "需要我", True, "要你回复、决定或验收的事"),
    ("calendar", "日程", False, "今天的会和待回复的邀请"),
    ("feishu", "飞书今天", False, "今天活跃的群"),
    ("agents", "Agent", False, "等你、可能卡住、在跑的 Agent"),
    ("mesh", "中转站", False, "StarkMesh 各窗口剩多少"),
    ("auto", "AI 代答", False, "今天自动代答了什么（有才显示）"),
    ("todos", "待办", False, "Persona 里的待办（有才显示）"),
    ("shortcuts", "快捷入口", True, "飞书、日程、中转站、Agent 进度的入口"),
    ("bookmarks", "书签", True, "扩展设置里“书签”填的链接"),
)
TITLES = {b[0]: b[1] for b in BLOCKS}
ABOUT = {b[0]: b[3] for b in BLOCKS}
WIDE = {b[0]: b[2] for b in BLOCKS}


class Layout:
    def __init__(self, order: List[str], hidden: List[str], wide: Dict[str, bool]):
        self.order, self.hidden, self.wide = order, hidden, wide

    @classmethod
    def default(cls) -> "Layout":
        return cls([b[0] for b in BLOCKS], [], {})

    @classmethod
    def load(cls, store) -> "Layout":
        raw = store.meta_get(KEY, "") if store is not None else ""
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        known = [b[0] for b in BLOCKS]
        order = [x for x in (d.get("order") or []) if x in known]
        order = list(dict.fromkeys(order))
        for i, x in enumerate(known):  # a block the stored order lacks: after the one before it by default
            if x not in order:
                before = [k for k in known[:i] if k in order]
                order.insert(order.index(before[-1]) + 1 if before else 0, x)
        hidden = [x for x in (d.get("hidden") or []) if x in known]
        wide = {k: bool(v) for k, v in (d.get("wide") or {}).items() if k in known and isinstance(v, bool)}
        return cls(order, hidden, wide)

    def save(self, store) -> None:
        store.meta_set(KEY, json.dumps({"order": self.order, "hidden": self.hidden, "wide": self.wide},
                                       ensure_ascii=False, separators=(",", ":")))

    def shown(self) -> List[str]:
        return [x for x in self.order if x not in self.hidden]

    def is_wide(self, block: str) -> bool:
        return self.wide.get(block, WIDE[block])

    def change(self, what: str, block: str) -> Optional[str]:
        """Apply one edit; the toast to show, or None when there was nothing to do."""
        if block not in self.order:
            return None
        i = self.order.index(block)
        if what == "up" and i > 0:
            self.order[i - 1], self.order[i] = self.order[i], self.order[i - 1]
            return "已上移“%s”" % TITLES[block]
        if what == "down" and i < len(self.order) - 1:
            self.order[i + 1], self.order[i] = self.order[i], self.order[i + 1]
            return "已下移“%s”" % TITLES[block]
        if what == "hide" and block not in self.hidden:
            self.hidden.append(block)
            return "已隐藏“%s”" % TITLES[block]
        if what == "show" and block in self.hidden:
            self.hidden.remove(block)
            return "已显示“%s”" % TITLES[block]
        if what in ("wide", "half"):
            want = what == "wide"
            if self.is_wide(block) == want:
                return None
            self.wide[block] = want
            return "“%s”改成%s" % (TITLES[block], "整行" if want else "半行")
        return None


def edit_view(layout: Layout, version: int, title: str, state: str) -> dict:
    """The page in 调整布局: one row per block, in the page's order, with its menu (UI version 6: a row's actions)
    or, before that, buttons under it."""
    v8 = version >= 8
    rows: List[Dict[str, Any]] = []
    n = len(layout.order)
    for i, b in enumerate(layout.order):
        hidden = b in layout.hidden
        bits = ["已隐藏" if hidden else "显示中"]
        bits.append("整行" if layout.is_wide(b) else "半行" + ("" if v8 else "（hub 升级后生效）"))
        acts = []
        if i > 0:
            acts.append(("上移", "up"))
        if i < n - 1:
            acts.append(("下移", "down"))
        acts.append(("显示", "show") if hidden else ("隐藏", "hide"))
        acts.append(("改成半行", "half") if layout.is_wide(b) else ("改成整行", "wide"))
        r = row(TITLES[b], "%s · %s" % (" · ".join(bits), ABOUT[b]), "%d" % (i + 1), key="lay:" + b)
        if version >= 6:
            r["icon"], r["icon_color"] = ("warning", "gray") if hidden else ("check", "green")
            r["actions"] = [{"title": t, "action": {"id": "layout:%s:%s" % (a, b)}} for t, a in acts]
            rows.append(r)
        else:
            rows.append(r)
            rows += [{"type": "button", "title": t, "action": {"id": "layout:%s:%s" % (a, b)}, "key": "lay:%s:%s" % (a, b)}
                     for t, a in acts]
    how = ("点每一行右边的菜单（手机上左滑或长按）上移、下移、隐藏，或者改成整行、半行。" if version >= 6 else
           "用每一块下面的按钮上移、下移、隐藏。")
    if not v8:
        how += "整行、半行要等 hub 升级到支持多列的版本后才生效，现在每块都占整行。"
    sections: List[Dict[str, Any]] = [
        {"title": "调整布局", "items": [{"type": "text", "style": "caption", "text": how}] + rows},
        {"items": [{"type": "button", "title": "完成", "action": {"id": "layout:done"}, "key": "lay:done"},
                   {"type": "button", "title": "恢复默认", "key": "lay:reset",
                    "action": {"id": "layout:reset", "confirm": "恢复默认的顺序、显示和宽度？"}}]},
    ]
    return {"title": title, "sections": sections, "state": state}

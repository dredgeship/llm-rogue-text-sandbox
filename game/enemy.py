"""敌人/怪物模块。

定义了怪物类型、属性模板（适配新属性体系），以及根据玩家角色
"强化/弱化"后的生成逻辑。
"""

from dataclasses import dataclass, field
from typing import Dict, List
import random

from .character import Passive


# 怪物类型定义：基础模板（攻击/防御/生命/暴击率/连击率）
MONSTER_TYPES = {
    "史莱姆": {"hp": 30, "atk": 5, "def": 1, "crit": 0.05, "combo": 0, "color": (80, 200, 120)},
    "骷髅兵": {"hp": 40, "atk": 8, "def": 3, "crit": 0.1, "combo": 0, "color": (200, 200, 200)},
    "哥布林": {"hp": 25, "atk": 10, "def": 1, "crit": 0.15, "combo": 0.2, "color": (150, 100, 80)},
    "岩石傀儡": {"hp": 90, "atk": 12, "def": 8, "crit": 0, "combo": 0, "color": (140, 130, 120)},
    "暗影刺客": {"hp": 35, "atk": 14, "def": 2, "crit": 0.3, "combo": 0, "color": (120, 80, 160)},
    "火焰魔像": {"hp": 60, "atk": 16, "def": 4, "crit": 0.1, "combo": 0.5, "color": (230, 100, 60)},
}

# 怪物类型之间的克制关系（用于规则混合分配被动时的倾向）
MONSTER_COUNTER = {
    "攻击力": "岩石傀儡",   # 高攻 -> 高防怪
    "防御力": "暗影刺客",   # 高防 -> 高暴击怪
    "生命值": "哥布林",     # 高血 -> 高连击怪
    "暴击率": "火焰魔像",   # 高暴击 -> 高连击怪
    "连击率": "岩石傀儡",   # 高连击 -> 高防怪
}


@dataclass
class Monster:
    """一个具体的怪物实例。"""

    mtype: str
    hp: float
    atk: float
    defense: float
    crit_rate: float = 0.0
    combo_rate: float = 0.0
    color: tuple = (150, 150, 150)
    # 分配的被动技能
    passives: List[Passive] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def describe(self) -> str:
        passive_str = "、".join(str(p) for p in self.passives) if self.passives else "无"
        return (
            f"{self.mtype} [HP {self.hp:.0f} 攻 {self.atk:.0f} "
            f"防 {self.defense:.0f} 暴 {self.crit_rate*100:.0f}% "
            f"连 {self.combo_rate:.1f}] 被动: {passive_str}"
        )


def _pick_counter_monster(stats: Dict[str, float]) -> str:
    """根据玩家最高属性维度挑选克制型怪物。"""
    if not stats:
        return random.choice(list(MONSTER_TYPES.keys()))
    top_key = max(stats.items(), key=lambda kv: _to_float(kv[1]))[0]
    return MONSTER_COUNTER.get(top_key, random.choice(list(MONSTER_TYPES.keys())))


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def create_monster(
    mtype: str,
    stats: Dict[str, float],
    passives: List[Passive],
    level: int = 1,
) -> Monster:
    """创建一只基础怪物，并按玩家属性做强化/弱化（加法平衡）。"""
    base = MONSTER_TYPES[mtype]
    lvl_scale = 1.0 + (level - 1) * 0.35
    player_power = sum(_to_float(v) for v in stats.values()) / max(1, len(stats))
    balance = 1.0 + player_power / 250.0

    hp = base["hp"] * lvl_scale * balance
    atk = base["atk"] * (1 + (lvl_scale - 1) * 0.5) * balance
    defense = base["def"] * lvl_scale
    crit = base["crit"]
    combo = base["combo"]

    return Monster(
        mtype=mtype,
        hp=hp,
        atk=atk,
        defense=defense,
        crit_rate=crit,
        combo_rate=combo,
        color=base["color"],
        passives=list(passives),
    )

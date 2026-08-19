"""角色框架与角色定义。

玩家基于预置框架自由填写属性和被动技能，构建自己的 Build。
所有强化均为加法级别，拒绝指数运算。

属性体系：
- 攻击力：基础攻击
- 防御力：减免伤害
- 生命值：生命上限
- 暴击率：触发暴击的概率（可 >1，超额部分按 1:1 转为暴击伤害）
- 连击率：整数部分=攻击次数，小数部分=额外攻击+1 的概率

被动触发时机：击杀 / 回合 / 攻击 / 受击 / 暴击 / 连击 / 数值 / 死亡 / 复活 / 战斗开始 / 战斗结束
被动可携带条件：概率（30%概率）、生命阈值（生命低于50%）、回合数（第3回合/每2回合）、首回合。
被动解析由 passive_engine 统一处理（条件层 + 效果层）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Union


# 支持的被动触发时机
PASSIVE_TRIGGERS = [
    "击杀",       # 击杀敌人时触发
    "回合",       # 每回合开始时触发
    "攻击",       # 每次攻击时触发
    "受击",       # 受到攻击时触发
    "暴击",       # 打出暴击时触发
    "连击",       # 触发连击时触发
    "数值",       # 直接加数值（永久属性 / 战斗内临时Buff）
    "死亡",       # 单位死亡时触发（可配合复活效果）
    "复活",       # 单位复活时触发
    "战斗开始",   # 战斗开始时触发一次
    "战斗结束",   # 战斗结束时触发
]

# 属性分类
# 普通属性：用于敌人属性预算（攻击/生命/防御）
NORMAL_ATTRS = ["攻击力", "生命值", "防御力"]
# 稀有属性：暴击率/连击率（暴击率按 1 计，连击率按 1 计）
RARE_ATTRS = ["暴击率", "连击率"]
# 全部属性（用于三选一强化属性池）
ALL_ATTRS = NORMAL_ATTRS + RARE_ATTRS


# 关卡与经济系统配置
LEVEL_CONFIG = {
    "level_count": 7,                 # 总关卡数
    "heal_on_enter_ratio": 0.10,      # 进入关卡回复当前生命上限的 10%
    # 多波敌人
    "multiwave_base": 2,              # 波数基础
    "multiwave_per_depth": 0.5,       # 每关深度增加 0.5 波（2 + 深度/2）
    "multiwave_budget_mult": 1.5,     # 多波怪物普通属性之和 = 玩家 × 1.5
    # 战后三选一强化
    "reinforce_choices": 3,           # 三选一
    # 强化配置：普通属性按"当前值百分比"提升（更贴合角色成长），
    # 稀有属性（暴击/连击）基数小、按比例不适合，保持固定加法。
    #   mode = "percent"：value 表示提升当前值的比例（如 0.20 = +20%）
    #   mode = "fixed" ：value 表示固定加法量
    "reinforce_amount": {
        # 普通属性按当前值百分比提升；数值相比初版已翻倍（生命回血除外）
        "攻击力": {"mode": "percent", "value": 0.40},
        "生命值": {"mode": "percent", "value": 0.30},
        "防御力": {"mode": "percent", "value": 0.40},
        "暴击率": {"mode": "fixed", "value": 0.10},
        "连击率": {"mode": "fixed", "value": 0.10},
    },
    "multiwave_reinforce_mult": 1.5,  # 多波模式下强化数值倍率
    # 治疗换算：放弃 X% 强化，回复 Y% 已损失生命值
    "heal_exchange_ratio": 0.5,       # 每放弃 1% 强化回复 0.5% 已损失生命
    # 被动抽取
    "monster_passive_pool": [         # 系统预设的怪物被动池（带触发时机）
        "[受击] 生命低于 50% 时攻击力提升",
        "[死亡] 死亡时 30% 概率复活",
        "[攻击] 攻击时回复伤害的 15%",
        "[受击] 第一回合先手闪避",
        "[受击] 受到攻击时反弹 3 点伤害",
        "[受击] 生命低于 50% 时 30% 概率闪避",
        "[回合] 每回合开始获得 3 点临时护盾",
        "[受击] 获得 1 次无敌",
        "[战斗开始] 偷取敌方 10% 攻击力",
    ],
}


# 角色框架：属性定义 + 被动结构说明
CHARACTER_FRAMEWORK = {
    "名称": "自定义角色",
    "属性": {
        "攻击力": "每次攻击的伤害基础值，加法强化",
        "防御力": "减免每次受到的伤害，加法强化",
        "生命值": "生命上限",
        "暴击率": "暴击概率，可>1，超额部分按1:1转暴击伤害",
        "连击率": "整数=攻击次数，小数=多攻击一次的概率",
    },
    "被动技能": {
        "说明": "每行一条被动。格式：[触发时机|条件] 效果描述",
        "触发时机": PASSIVE_TRIGGERS,
        "可用条件": [
            "概率：'30%概率' 按 30% 概率触发",
            "生命阈值：'生命低于50%时' 生命降到一半以下才触发",
            "回合数：'第3回合' 第3回合触发 / '每2回合' 每两回合触发",
            "首回合：'第一回合' / '先手' 仅第一回合触发",
        ],
        "效果类型": [
            "永久属性加成：直接加攻击力/防御力/生命值/暴击率/连击率",
            "临时Buff：战斗中按条件生效",
            "斩杀：对低血量目标增伤",
            "无敌/护盾：防御性效果",
            "属性转移：如'防御转攻击'",
            "偷取：吸取敌方属性",
            "自定义效果：任意文本描述（无法解析时会在战斗中提示未生效）",
        ],
        "示例": [
            "[攻击] 攻击时 30% 概率附加 2 点额外伤害",
            "[攻击|30%概率] 附加 2 点额外伤害",
            "[击杀] 击杀敌人后攻击力 +2（永久）",
            "[受击|生命低于50%] 反弹 30% 伤害",
            "[回合] 每回合开始获得 3 点临时护盾",
            "[回合|每2回合] 获得 3 点临时护盾",
            "[受击] 生命低于50%时 30%概率闪避",
            "[暴击] 暴击时额外造成 50% 伤害",
            "[连击] 每次连击追加 1 点固定伤害",
            "[受击] 受到攻击时反弹 3 点伤害",
            "[数值] 暴击率 +20",
            "[战斗开始] 偷取敌方 10% 攻击力",
            "[死亡] 死亡时 30% 概率复活",
            "[攻击] 把 30% 防御力转为攻击力",
        ],
    },
}

# 基础属性默认值
DEFAULT_STATS = {"攻击力": 10, "防御力": 0, "生命值": 100, "暴击率": 0, "连击率": 0}


@dataclass
class Passive:
    """一条被动技能。

    trigger: 触发时机（PASSIVE_TRIGGERS 之一）
    effect: 效果描述文本
    name: 被动名字（可选，"名字：效果" 前缀，用于抽取/辨识时引用）
    """

    trigger: str
    effect: str
    name: str = ""

    def __str__(self) -> str:
        # 有名字时输出 "[时机] 名字：效果"，方便怪物抽取时按名字引用
        if self.name:
            return f"[{self.trigger}] {self.name}：{self.effect}"
        return f"[{self.trigger}] {self.effect}"

    @classmethod
    def parse(cls, text: str) -> "Passive":
        """解析用户输入：'[触发时机|条件] 效果描述'。

        支持 [时机|条件] 标签（如 "[受击|生命低于50%]"）。
        支持可选名字前缀：'[时机] 名字：效果'。
        无触发时机时默认 '数值'。条件子串会并入 effect 文本，由 passive_engine
        统一解析（条件层判定概率/阈值/回合数，效果层执行具体效果）。
        """
        t = text.strip()
        if not t:
            return cls(trigger="数值", effect="", name="")
        if t.startswith("[") and "]" in t:
            tag = t[1:t.index("]")].strip()
            effect = t[t.index("]") + 1:].strip()
            # 支持 [时机|条件]：拆分基础时机与条件
            if "|" in tag:
                trigger = tag.split("|", 1)[0].strip()
                cond = tag.split("|", 1)[1].strip()
            else:
                trigger = tag
                cond = ""
            if trigger in PASSIVE_TRIGGERS:
                # 条件并入效果文本，交给 passive_engine 统一解析
                effect = (cond + " " + effect).strip()
                name, body = _split_passive_name(effect)
                return cls(trigger=trigger, effect=body, name=name)
        # 无标签时也尝试剥离名字前缀（如手写 "名字：效果"）
        name, body = _split_passive_name(t)
        return cls(trigger="数值", effect=body, name=name)


def _split_passive_name(text: str):
    """从效果文本中剥离可选的"名字："前缀。

    复用 passive_engine 的 split_name，保持两端解析一致。
    """
    from . import passive_engine
    return passive_engine.split_name(text)


@dataclass
class Character:
    """玩家创建的角色。属性可自由填写（加法强化）。

    current_hp 表示当前生命值，stats["生命值"] 表示生命上限。
    战斗中扣除的是 current_hp；进入关卡/治疗会回复 current_hp。
    """

    name: str = "自定义角色"
    # 基础属性（stats["生命值"] 为生命上限）
    stats: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_STATS))
    # 被动技能列表
    passives: List[Passive] = field(default_factory=list)
    # 当前生命值（初始 = 生命上限）
    current_hp: float = field(default=None)

    def __post_init__(self):
        if self.current_hp is None:
            self.current_hp = self.get("生命值", 100)

    def get(self, key: str, default: float = 0.0) -> float:
        """安全获取某个属性的数值。"""
        val = self.stats.get(key, default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return float(default)

    @property
    def max_hp(self) -> float:
        return self.get("生命值", 100)

    def add_stat(self, key: str, amount: float):
        """加法强化：把 amount 加到属性上。"""
        self.stats[key] = self.get(key) + amount
        if key == "生命值":
            self.current_hp += amount  # 强化生命上限同时补当前生命

    def heal(self, amount: float) -> float:
        """治疗，返回实际回复量（不超过上限）。"""
        if amount <= 0:
            return 0.0
        before = self.current_hp
        self.current_hp = min(self.max_hp, self.current_hp + amount)
        return self.current_hp - before

    def take_damage(self, amount: float) -> float:
        """受到伤害，返回实际扣血量。"""
        before = self.current_hp
        self.current_hp = max(0.0, self.current_hp - amount)
        return before - self.current_hp

    @property
    def alive(self) -> bool:
        return self.current_hp > 0

    @property
    def lost_hp(self) -> float:
        """已损失生命值。"""
        return max(0.0, self.max_hp - self.current_hp)

    def enter_level_heal(self) -> float:
        """进入关卡自动回复当前生命上限的 10%，返回回复量。"""
        ratio = LEVEL_CONFIG["heal_on_enter_ratio"]
        return self.heal(self.max_hp * ratio)

    def reset_to_full(self):
        """满血重置（用于新游戏开始）。"""
        self.current_hp = self.max_hp

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "stats": self.stats,
            "passives": [str(p) for p in self.passives],
            "current_hp": self.current_hp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        stats = dict(DEFAULT_STATS)
        stats.update({k: v for k, v in data.get("stats", {}).items()})
        passives = [
            p if isinstance(p, Passive) else Passive.parse(str(p))
            for p in data.get("passives", [])
        ]
        return cls(
            name=data.get("name", "自定义角色"),
            stats=stats,
            passives=passives,
            current_hp=data.get("current_hp"),
        )

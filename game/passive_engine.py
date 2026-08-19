"""通用被动解析引擎。

把被动文本从"关键词 if-else 匹配"升级为两层结构：

    被动文本 [时机|条件] 效果
       │
       ▼
    Condition（条件层）  →  判定本次是否触发
       │     解析：概率 / 生命阈值 / 回合数 / 首回合 / 无条件
       ▼
    Effect（效果层）     →  解析并执行具体效果
           解析：斩杀 / 无敌 / 属性转移 / 偷取 / 闪避 / 吸血 /
               护盾 / 反弹 / 额外伤害 / 属性叠加 / 回血 / 临时Buff

设计约束：
- 中约定：自然语言为主，关键参数用关键词/括号标注。
- 所有强化保持加法级别，拒绝指数运算。
- 解析失败返回 (False, reason)，供上层做"该被动未生效"提示，绝不静默忽略。

本引擎不依赖 pygame，可纯逻辑单测。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import re

# ---------------------------------------------------------------------------
# 触发时机全集（与 character.PASSIVE_TRIGGERS 同步，此处为引擎内部定义）
# ---------------------------------------------------------------------------
TRIGGER_KILL = "击杀"
TRIGGER_TURN = "回合"
TRIGGER_ATTACK = "攻击"
TRIGGER_HIT = "受击"
TRIGGER_CRIT = "暴击"
TRIGGER_COMBO = "连击"
TRIGGER_STAT = "数值"
TRIGGER_DEATH = "死亡"
TRIGGER_REVIVE = "复活"
TRIGGER_BATTLE_START = "战斗开始"
TRIGGER_BATTLE_END = "战斗结束"

ALL_TRIGGERS = [
    TRIGGER_KILL, TRIGGER_TURN, TRIGGER_ATTACK, TRIGGER_HIT,
    TRIGGER_CRIT, TRIGGER_COMBO, TRIGGER_STAT, TRIGGER_DEATH,
    TRIGGER_REVIVE, TRIGGER_BATTLE_START, TRIGGER_BATTLE_END,
]

# 效果类型枚举（供规则表与效果层使用）
EFF_ATK_UP = "攻击力提升"
EFF_DEF_UP = "防御力提升"
EFF_HP_UP = "生命上限提升"
EFF_CRIT_UP = "暴击率提升"
EFF_COMBO_UP = "连击率提升"
EFF_HEAL = "回血"
EFF_LIFESTEAL = "吸血"
EFF_SHIELD = "护盾"
EFF_REFLECT = "反弹"
EFF_EXTRA_DMG = "额外伤害"
EFF_EXECUTE = "斩杀"
EFF_INVULN = "无敌"
EFF_TRANSFER = "属性转移"
EFF_STEAL = "偷取"
EFF_DODGE = "闪避"
EFF_TEMP_ATK = "临时攻击"
EFF_TEMP_DEF = "临时防御"
EFF_REVIVE = "复活"
EFF_BERSERK = "狂暴"
EFF_THORN = "荆棘"
EFF_EVADE_FIRST = "潜行"
EFF_NONE = "无效"   # 占位：解析失败/无法识别的效果


@dataclass
class Condition:
    """一次被动的触发条件。"""

    trigger: str                       # 基础时机（击杀/回合/攻击/受击/...）
    probability: Optional[float] = None    # 0~1，None 表示无条件触发
    hp_ratio: Optional[float] = None       # 生命阈值（如 0.5 = 生命低于50%）
    hp_direction: str = "below"            # "below" 生命低于 / "above" 生命高于
    turn_kind: str = "once"                # "once" 第N回合 / "every" 每N回合 / None 无条件
    turn_n: Optional[int] = None           # 第N回合 / 每N回合
    first_turn: bool = False               # 是否"第一回合"限定
    raw: str = ""                          # 原始条件文本（debug 用）

    def check(self, unit, ignore_hp: bool = False) -> bool:
        """用单位状态判定条件是否满足。unit 需暴露 turn / hp / max_hp。

        ignore_hp=True 时跳过生命阈值判定（供狂暴等"攻击时持续判断 hp"的效果用）。
        """
        if unit is None:
            return False
        # 概率触发
        if self.probability is not None:
            import random
            if random.random() >= self.probability:
                return False
        # 生命阈值（狂暴类效果可忽略，改由攻击时按当前血量判断）
        if not ignore_hp and self.hp_ratio is not None:
            if unit.max_hp <= 0:
                return False
            ratio = unit.hp / unit.max_hp
            if self.hp_direction == "below" and not (ratio < self.hp_ratio):
                return False
            if self.hp_direction == "above" and not (ratio > self.hp_ratio):
                return False
        # 回合数
        turn = getattr(unit, "turn", 1)
        if self.first_turn and turn > 1:
            return False
        if self.turn_n is not None:
            if self.turn_kind == "once" and turn != self.turn_n:
                return False
            if self.turn_kind == "every" and (turn % self.turn_n) != 0:
                return False
        return True


@dataclass
class Effect:
    """解析后的效果对象。

    kind: 效果类型（EFF_*）
    amount: 数值（伤害/回复/提升量/概率等）
    attr: 涉及的属性名（如"攻击力"/"防御力"/"生命值"/"暴击率"/"连击率"）
    ratio: 比例（吸血比例、斩杀血量阈值等）
    target_self: 是否作用于自己（否则作用于敌人）
    raw: 原始效果文本
    """

    kind: str
    amount: float = 0.0
    attr: Optional[str] = None
    ratio: float = 0.0
    target_self: bool = True
    raw: str = ""
    # 属性叠加是否跨战斗持久（True=加到永久字段并同步回角色；
    # False=战斗内临时叠加，战斗结束自动失效）。
    # 默认 False：未写明永久/临时时按战斗内临时处理，避免"没写明白就默认永久"。
    permanent: bool = False
    # 比例的基数（回血/护盾按生命比例时的基准）：
    #   "max"     按最大生命（默认）
    #   "lost"    按已损失生命
    #   "current" 按当前生命
    ratio_base: str = "max"

    def __repr__(self):
        return f"<Effect {self.kind} amount={self.amount} attr={self.attr} ratio={self.ratio} self={self.target_self}>"


@dataclass
class ParsedPassive:
    """一条完整解析后的被动。"""

    condition: Condition
    effect: Effect
    raw: str = ""
    ok: bool = True
    reason: str = ""
    name: str = ""   # 被动名字（可选，"名字：效果" 前缀中的名字）


# ---------------------------------------------------------------------------
# 数值 / 概率解析辅助
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _first_number(text: str, after_keyword: Optional[str] = None) -> Optional[float]:
    """提取文本中第一个数字。可指定在某个关键词之后开始搜索。"""
    idx = 0
    if after_keyword:
        i = text.find(after_keyword)
        if i == -1:
            return None
        idx = i + len(after_keyword)
    m = _NUM_RE.search(text[idx:])
    if m:
        return float(m.group(1))
    return None


def _parse_percent(text: str) -> Optional[float]:
    """解析百分比为 0~1，无百分号返回 None。"""
    m = _PCT_RE.search(text)
    if m:
        return float(m.group(1)) / 100.0
    return None


def _amount_and_ratio(text: str) -> Tuple[float, float]:
    """从效果文本中提取 (固定值, 比例0~1)。

    - 带 % 的数字 → 比例（除以 100）
    - 不带 % 的数字 → 固定值
    两者可同时存在（如 "反弹 2 点伤害 20% 本次伤害" → (2, 0.2)）。
    """
    amount = 0.0
    ratio = 0.0
    for m in _NUM_RE.finditer(text):
        num = float(m.group(1))
        if text[m.end():m.end() + 1] == "%":
            ratio = num / 100.0
        elif num != 0:
            amount = num
    return amount, ratio


# 生命比例基数（回血/护盾按比例时的基准）
RATIO_BASE_MAX = "max"         # 最大生命（默认）
RATIO_BASE_LOST = "lost"       # 已损失生命
RATIO_BASE_CURRENT = "current" # 当前生命


def _ratio_base(text: str) -> str:
    """从回血/护盾文本中识别比例基数，默认按最大生命。"""
    if any(k in text for k in ("已损失生命", "损失生命", "已损生命", "损失的血量", "失去的生命")):
        return RATIO_BASE_LOST
    if any(k in text for k in ("当前生命", "现有生命")):
        return RATIO_BASE_CURRENT
    return RATIO_BASE_MAX


# 属性别名表：规范名 -> 常见写法（规范名优先，避免别名误匹配时机词）
_ATTR_ALIASES = [
    ("攻击力", ["攻击力", "攻击"]),
    ("防御力", ["防御力", "防御"]),
    ("生命值", ["生命值", "生命上限", "血量上限", "生命"]),
    ("暴击率", ["暴击率", "暴击几率", "暴击"]),
    ("连击率", ["连击率", "连击数", "连击次数"]),
]


def _find_attr(text: str) -> Optional[str]:
    """在文本中查找出现的属性名（支持常用别名）。"""
    for canon, aliases in _ATTR_ALIASES:
        for a in aliases:
            if a in text:
                return canon
    return None


def _find_attr_token(text: str):
    """返回 (规范名, 文本中实际出现的属性写法)。

    用于在别名场景（如"连击数+1"里写的是"连击数"而非"连击率"）定位数字。
    """
    for canon, aliases in _ATTR_ALIASES:
        for a in aliases:
            if a in text:
                return canon, a
    return None, None


# ---------------------------------------------------------------------------
# 条件层解析
# ---------------------------------------------------------------------------
def parse_condition(trigger: str, text: str) -> Condition:
    """解析被动文本中的条件部分（效果文本或 [时机|条件] 中提取出的条件）。

    trigger: 基础时机（如"攻击"）
    text: 效果文本（如"生命低于50%时反弹30%伤害"）
    """
    cond = Condition(trigger=trigger, raw=text)

    # 概率：如 "30%概率" / "30% 概率" / "概率30%"
    if "概率" in text:
        pm = (re.search(r"(\d+(?:\.\d+)?)\s*%\s*概率", text)
              or re.search(r"概率\s*(\d+(?:\.\d+)?)\s*%", text))
        if pm:
            cond.probability = float(pm.group(1)) / 100.0

    # 生命阈值：生命(值)低于/高于 X%   （兼容"生命值低于50%"与"生命低于50%"）
    hp_m = re.search(r"生命(?:值|上限)?(低于|高于|小于|大于|不足|少于)\s*(\d+(?:\.\d+)?)\s*%", text)
    if hp_m:
        cond.hp_direction = "below" if hp_m.group(1) in ("低于", "小于", "不足", "少于") else "above"
        cond.hp_ratio = float(hp_m.group(2)) / 100.0

    # 回合数：第 N 回合 / 每 N 回合
    m_once = re.search(r"第\s*(\d+)\s*回合", text)
    if m_once:
        cond.turn_kind = "once"
        cond.turn_n = int(m_once.group(1))
    m_every = re.search(r"每\s*(\d+)\s*回合", text)
    if m_every:
        cond.turn_kind = "every"
        cond.turn_n = int(m_every.group(1))

    # 第一回合
    if "第一回合" in text or "首回合" in text or "先手" in text:
        cond.first_turn = True

    return cond


def parse_trigger_tag(raw_trigger: str) -> Tuple[str, Optional[str]]:
    """解析 [时机|条件] 标签。

    返回 (基础时机, 条件子串或 None)。
    例："受击|生命低于50%" -> ("受击", "生命低于50%")
        "攻击" -> ("攻击", None)
    也兼容带空格："攻击 | 30%概率"
    """
    raw = raw_trigger.strip()
    if "|" in raw:
        head, _, cond = raw.partition("|")
        return head.strip(), cond.strip()
    return raw, None


# ---------------------------------------------------------------------------
# 效果层：每条规则一个匹配函数，返回 (kind, amount, attr, ratio, target_self)
# 返回值 None 表示未命中；返回的 dict 为效果参数。
# ---------------------------------------------------------------------------
def _rule_dodge(text: str):
    # 闪避：必闪 / X%概率闪避 / 第一回合闪避
    if "闪避" in text:
        p = _parse_percent(text)
        return dict(kind=EFF_DODGE, amount=(p if p is not None else 1.0))
    # "受到的伤害变为0" / "免疫伤害" / "伤害无效" 等免伤表达，等价于必定闪避
    if any(k in text for k in ("变为0", "变成0", "免疫伤害", "伤害无效", "本次伤害归零", "不受伤害", "减免本次伤害")):
        return dict(kind=EFF_DODGE, amount=1.0)
    return None


def _rule_lifesteal(text: str):
    # 吸血：显式"吸血"，或"回复伤害的 X%"这类表达。
    # 比例：按本次造成伤害；固定值：每次攻击固定回复 X 点。两者可并存相加。
    if "吸血" in text or ("回复" in text and "伤害" in text):
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            ratio = 0.15  # 默认 15%
        return dict(kind=EFF_LIFESTEAL, amount=amount, ratio=ratio)
    return None


def _rule_heal(text: str):
    # 回血：固定值（回 X 点）/ 比例（X%，基数可为最大生命/已损失生命/当前生命）。
    if "回复生命" in text or ("回复" in text and "生命" in text) or "回血" in text:
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            return None
        return dict(kind=EFF_HEAL, amount=amount, ratio=ratio,
                    ratio_base=_ratio_base(text))
    return None


def _rule_shield(text: str):
    # 护盾：固定值（获得 X 点）/ 比例（X%，基数可为最大生命/已损失生命/当前生命）。
    if "护盾" in text:
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            return None
        return dict(kind=EFF_SHIELD, amount=amount, ratio=ratio,
                    ratio_base=_ratio_base(text))
    return None


def _rule_reflect(text: str):
    # 反弹：固定值（反弹 X 点伤害）/ 比例（反弹 X% 受到的伤害），可并存相加。
    if "反弹" in text or "反伤" in text or "荆棘" in text:
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            amount = 1.0
        return dict(kind=EFF_REFLECT, amount=amount, ratio=ratio)
    return None


def _rule_extra_dmg(text: str):
    # 额外伤害：固定值（额外造成 X 点）/ 比例（额外造成 X% 本次伤害），可并存相加。
    if "额外" in text and "伤害" in text:
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            amount = 1.0
        return dict(kind=EFF_EXTRA_DMG, amount=amount, ratio=ratio)
    return None


def _rule_execute(text: str):
    # 斩杀：对生命低于 X% 的目标造成额外/固定伤害
    if "斩杀" in text:
        p = _parse_percent(text)
        num = _first_number(text, "斩杀")
        return dict(kind=EFF_EXECUTE, amount=(num if num is not None else 0.0),
                    ratio=(p if p is not None else 0.2))
    return None


def _rule_invuln(text: str):
    if "无敌" in text:
        num = _first_number(text)
        return dict(kind=EFF_INVULN, amount=(num if num is not None else 1))
    return None


def _rule_transfer(text: str):
    # 属性转移：如"防御转攻击"、"把30%防御力转为攻击力"。
    # 比例：转移源属性的 X%；固定值：转移固定 X 点。两者可并存相加。
    if "转" in text:
        # 源属性在"转"之前，目标属性在"转"之后
        zi = text.find("转")
        before = text[:zi]
        src = _find_attr(before)
        m = re.search(r"转(?:换|化|为|成)?(攻击力|防御力|生命值|暴击率|连击率)", text)
        dst = m.group(1) if m else _find_attr(text[zi:])
        if dst == src:
            dst = None
        if src and dst:
            amount, ratio = _amount_and_ratio(text)
            if amount == 0 and ratio == 0:
                ratio = 1.0
            return dict(kind=EFF_TRANSFER, attr=src, amount=amount,
                        ratio=ratio, raw=text)
    return None


def _rule_steal(text: str):
    # 偷取/吸取对方属性：如"偷取敌方10%攻击力"。
    # 比例：偷取该属性的 X%；固定值：偷取固定 X 点。两者可并存相加。
    if "偷取" in text or "吸取" in text or "窃取" in text:
        attr, token = _find_attr_token(text)
        if attr:
            amount, ratio = _amount_and_ratio(text)
            if amount == 0 and ratio == 0:
                ratio = 0.1
            return dict(kind=EFF_STEAL, attr=attr, amount=amount, ratio=ratio)
    return None


def _rule_stat(text: str):
    # 永久/每次/每回合属性叠加（+N）
    attr, token = _find_attr_token(text)
    if attr:
        # 含"生命低于/高于X%"条件的属性提升 = 触发式（狂暴/条件强化）：
        # 不能当作无条件的永久/临时属性叠加，否则会在满血时也生效、数值离谱。
        # 对攻击力提升，视为狂暴（由 EFF_BERSERK 在 hp 条件下按量提升攻击）。
        has_hp_cond = re.search(r"生命(?:值|上限)?(低于|高于|小于|大于|不足|少于)", text)
        if has_hp_cond and attr == "攻击力":
            amount, ratio = _amount_and_ratio(text)
            if amount == 0 and ratio == 0:
                amount = 5.0
            return dict(kind=EFF_BERSERK, amount=amount, ratio=ratio)
        # 显式"永久"才算跨战斗持久；"临时"或未写明默认战斗内临时
        permanent = "永久" in text and "临时" not in text
        num = _first_number(text, token)
        if num is not None:
            return dict(kind=EFF_ATK_UP if attr == "攻击力"
                        else EFF_DEF_UP if attr == "防御力"
                        else EFF_HP_UP if attr == "生命值"
                        else EFF_CRIT_UP if attr == "暴击率"
                        else EFF_COMBO_UP,
                        attr=attr, amount=num, permanent=permanent)
        # 无数字的"XX提升/增强/增加"：给默认加成，保证"攻击力提升"这类官方词条可解析
        if any(k in text for k in ("提升", "增强", "增加", "提高", "强化")):
            default = 0.05 if attr in ("暴击率", "连击率") else 3.0
            return dict(kind=EFF_ATK_UP if attr == "攻击力"
                        else EFF_DEF_UP if attr == "防御力"
                        else EFF_HP_UP if attr == "生命值"
                        else EFF_CRIT_UP if attr == "暴击率"
                        else EFF_COMBO_UP,
                        attr=attr, amount=default, permanent=permanent)
    return None


def _rule_temp_buff(text: str):
    # 临时攻击/防御提升
    if "临时" in text:
        attr, token = _find_attr_token(text)
        if attr == "攻击力":
            num = _first_number(text, token)
            if num is not None:
                return dict(kind=EFF_TEMP_ATK, amount=num)
        if attr == "防御力":
            num = _first_number(text, token)
            if num is not None:
                return dict(kind=EFF_TEMP_DEF, amount=num)
    return None


def _rule_revive(text: str):
    if "复活" in text or "重生" in text:
        p = _parse_percent(text)
        return dict(kind=EFF_REVIVE, ratio=(p if p is not None else 1.0))
    return None


def _rule_berserk(text: str):
    # 狂暴：生命低于/高于阈值时攻击力提升。无数字时默认提升 5 点。
    # 固定值：提升 X 点；比例：按自身攻击力的 X%。可并存相加。
    # 显式写"狂暴"才识别为狂暴；hp 阈值条件由条件层（parse_condition）提供。
    if "狂暴" in text:
        amount, ratio = _amount_and_ratio(text)
        if amount == 0 and ratio == 0:
            amount = 5.0
        return dict(kind=EFF_BERSERK, amount=amount, ratio=ratio)
    return None


# 效果规则表（顺序即匹配优先级，靠前者优先）
_EFFECT_RULES: List[Tuple[str, callable]] = [
    ("闪避", _rule_dodge),
    ("吸血", _rule_lifesteal),
    ("回血", _rule_heal),
    ("护盾", _rule_shield),
    ("反弹", _rule_reflect),
    ("斩杀", _rule_execute),
    ("无敌", _rule_invuln),
    ("属性转移", _rule_transfer),
    ("偷取", _rule_steal),
    ("额外伤害", _rule_extra_dmg),
    ("临时Buff", _rule_temp_buff),
    # 狂暴需放在"属性叠加"之前：狂暴文本通常含"攻击力提升+N"，
    # 若被 _rule_stat 拦截会误判为属性叠加而非狂暴。
    ("狂暴", _rule_berserk),
    ("属性叠加", _rule_stat),
    ("复活", _rule_revive),
]


def parse_effect(text: str) -> Optional[Effect]:
    """解析效果文本，返回 Effect；无法解析返回 None。"""
    for label, fn in _EFFECT_RULES:
        try:
            res = fn(text)
        except Exception:
            continue
        if res is not None:
            res.pop("raw", None)  # 规则返回的 raw 由 Effect 统一填充
            return Effect(**res, raw=text)
    return None


# ---------------------------------------------------------------------------
# 被动名字前缀
# ---------------------------------------------------------------------------
# 名字后的分隔符：中文冒号（或英文冒号+空格），如 "吸血之牙：攻击时回复伤害的 15%"
_NAME_SEP_RE = re.compile(r"^([^:：]{1,12})[:：]\s*(.+)$")

# 名字不能"完全等于"这些效果关键词：若名字恰好是某个效果关键词（如"吸血"、
# "狂暴"），说明它是效果描述而不是名字。名字可以是包含这些词的长词
#（如"吸血之牙"、"狂暴之躯"），因为这些明显是名字而非效果描述。
_NAME_BLOCK_WORDS = frozenset({
    "闪避", "吸血", "回血", "回复", "护盾", "反弹", "反伤", "荆棘",
    "斩杀", "无敌", "转", "偷取", "吸取", "窃取", "额外",
    "临时", "复活", "重生", "狂暴", "获得", "受到", "造成",
    "攻击", "防御", "生命", "暴击", "连击", "提升", "增强", "增加",
    "提高", "强化", "降低", "死亡",
})
# 名字中不允许出现的"条件描述词"（子串匹配）：名字若含这些词，说明它实际是
# 效果描述（如"生命低于30%狂暴：攻击力提升"），而非纯粹的名字（如"吸血之牙"）。
_NAME_COND_WORDS = ("低于", "高于", "小于", "大于", "不足", "少于",
                    "概率", "回合", "首回合", "第一", "先手")


def split_name(effect_text: str) -> Tuple[str, str]:
    """从效果文本中剥离可选的"名字："前缀。

    格式约定（由积木编辑器产出）：'名字：效果'。
    仅当以'名字：'开头、名字不含条件描述词、且冒号后部分能被效果规则解析时，
    才认为前半段是名字，返回 (名字, 剩余效果文本)；否则返回 ("", 原文本)。

    返回 (name, effect_text)。
    """
    m = _NAME_SEP_RE.match(effect_text)
    if not m:
        return "", effect_text
    name, rest = m.group(1).strip(), m.group(2).strip()
    if not name or not rest:
        return "", effect_text
    if name in _NAME_BLOCK_WORDS:
        return "", effect_text
    # 名字若含条件描述词（低于/概率/回合等），说明它是效果描述而非名字
    if any(w in name for w in _NAME_COND_WORDS):
        return "", effect_text
    if parse_effect(rest) is None:
        return "", effect_text
    return name, rest


# ---------------------------------------------------------------------------
# 对外统一接口
# ---------------------------------------------------------------------------
def parse(text: str) -> ParsedPassive:
    """解析整条被动文本（含 [时机] 前缀）。

    格式：[时机|条件] 效果
    - 无 [时机] 时默认"数值"（永久属性）。
    - 效果解析失败时 ok=False，reason 说明原因。
    """
    raw = text.strip()
    if not raw:
        return ParsedPassive(Condition("数值"), Effect(EFF_NONE), raw="",
                             ok=False, reason="空被动")

    trigger = TRIGGER_STAT
    effect_text = raw
    cond_text = ""

    if raw.startswith("[") and "]" in raw:
        tag = raw[1:raw.index("]")]
        effect_text = raw[raw.index("]") + 1:].strip()
        trigger, cond_text = parse_trigger_tag(tag)
        if trigger not in ALL_TRIGGERS:
            return ParsedPassive(
                Condition(TRIGGER_STAT), Effect(EFF_NONE), raw=raw,
                ok=False, reason=f"未知触发时机[{trigger}]，可选：{'/'.join(ALL_TRIGGERS)}")

    # 剥离可选的"名字："前缀（名字只作标识，不影响条件/效果解析）
    name, effect_body = split_name(effect_text)

    # 合并条件：标签条件 + 效果文本内条件
    merged_cond = " ".join(x for x in (cond_text, effect_body) if x)
    condition = parse_condition(trigger, merged_cond)
    condition.raw = raw

    effect = parse_effect(effect_body)
    if effect is None:
        return ParsedPassive(
            condition, Effect(EFF_NONE, raw=effect_body), raw=raw,
            ok=False, reason=f"无法解析效果：'{effect_body}'")

    # 斩杀效果的"生命低于X%"是目标的血量阈值，而非单位自身的触发条件：
    # 不能让 Condition.check 误判为"单位自己血量低于X%才触发"，否则永远不触发。
    if effect.kind == EFF_EXECUTE:
        condition.hp_ratio = None
        condition.hp_direction = "below"

    # [数值] 时机本身就是"直接加数值"，视为永久属性加成
    if trigger == TRIGGER_STAT:
        effect.permanent = True
    # 显式写"永久"也视为永久
    if "永久" in effect_body and "临时" not in effect_body:
        effect.permanent = True

    return ParsedPassive(condition, effect, raw=raw, ok=True, reason="", name=name)


def validate(text: str) -> Tuple[bool, str]:
    """校验单条被动是否可解析。返回 (ok, reason)。"""
    p = parse(text)
    return p.ok, p.reason

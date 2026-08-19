"""关卡与经济系统。

7 个关卡，每关玩家在进入时二选一：单波 或 多波。
每关只出现一种敌人，战斗依次进行（杀完一只打下一只）。

经济规则：
- 普通属性（攻击/生命/防御）用于敌人属性预算。
- 单波：小怪普通属性之和 = 玩家进入关卡时普通属性之和（1:1）。
- 多波：小怪普通属性之和 = 玩家普通属性之和 × 1.5。
- 波数随关卡深度线性增加：波数 = 2 + 深度/2，总属性按波数均分。
- 关卡深度线性提升小怪稀有属性（暴击/连击）与被动强度。
- 被动从"玩家构建的被动"与"系统预设被动池"中抽取。
- 战后三选一强化（5 属性池等概率抽 3）：普通属性按当前值百分比提升，
  稀有属性固定加法，多波模式强化数值上升。
- 提供"放弃强化换治疗"选项：每放弃 X% 强化，回复 Y% 已损失生命值。
- 进入关卡自动回复当前生命上限的 10%。
"""

import random
from typing import List, Tuple, Optional

from .character import (
    Character, Passive, NORMAL_ATTRS, RARE_ATTRS, ALL_ATTRS, LEVEL_CONFIG,
)
from .enemy import Monster, _pick_counter_monster

# 波数随关卡深度的线性增长参数
MULTIWAVE_BASE = LEVEL_CONFIG["multiwave_base"]
MULTIWAVE_PER_DEPTH = LEVEL_CONFIG["multiwave_per_depth"]
MULTIWAVE_BUDGET_MULT = LEVEL_CONFIG["multiwave_budget_mult"]
REINFORCE_CHOICES = LEVEL_CONFIG["reinforce_choices"]
REINFORCE_AMOUNT = LEVEL_CONFIG["reinforce_amount"]
MULTIWAVE_REINFORCE_MULT = LEVEL_CONFIG["multiwave_reinforce_mult"]
HEAL_EXCHANGE_RATIO = LEVEL_CONFIG["heal_exchange_ratio"]
HEAL_ON_ENTER_RATIO = LEVEL_CONFIG["heal_on_enter_ratio"]


def compute_player_normal_sum(character: Character) -> float:
    """计算玩家普通属性之和（攻击 + 生命 + 防御）。"""
    return sum(character.get(a) for a in NORMAL_ATTRS)


def multiwave_count(level: int) -> int:
    """多波模式的波数：随关卡深度线性增加。"""
    return int(MULTIWAVE_BASE + level * MULTIWAVE_PER_DEPTH)


def monster_budget(character: Character, level: int, multiwave: bool) -> Tuple[float, int]:
    """计算本关怪物的普通属性总预算与波数。

    返回 (总普通属性预算, 波数)。单波返回 1 波。
    """
    player_normal = compute_player_normal_sum(character)
    if multiwave:
        budget = player_normal * MULTIWAVE_BUDGET_MULT
        waves = multiwave_count(level)
    else:
        budget = player_normal
        waves = 1
    return budget, waves


def _rare_scale(level: int, max_level: int = 7) -> float:
    """关卡深度线性缩放因子（稀有属性与被动强度）。"""
    return 0.5 + level / max_level  # 第1关约0.64，第7关约1.5


def monster_passive_count(level: int, player_passive_count: int = 0) -> int:
    """根据关卡深度与玩家被动数决定小怪被动数量。

    规则：
    - 1~4 关：怪物被动数 = max(1, 玩家被动数)（与玩家"相同"，无则至少 1 条）
    - 5~6 关：怪物被动数 = max(2, 玩家被动数 + 1)（比玩家多 1 条）
    - 7 关  ：怪物被动数 = max(3, 玩家被动数 + 2)（boss 比玩家多 2 条）

    比玩家多出的部分从玩家身上抽取（见 draw_monster_passives）。
    """
    base = 1
    offset = 0
    if level >= 7:
        base, offset = 3, 2
    elif level >= 5:
        base, offset = 2, 1
    return max(base, player_passive_count + offset)


def draw_monster_passives(
    character: Character, level: int, rng: random.Random,
    count: int = None,
) -> List[Passive]:
    """从"玩家构建的被动"与"系统预设被动池"中抽取怪物被动。

    抽取规则：
    1. 玩家已有的被动全部以"削弱版"出现（数量受 count 与玩家数限制）。
    2. 不足 count 时从玩家原版被动随机抽（"从玩家身上抽取"）。
    3. 还不足从系统被动池抽，最后兜底为狂暴。
    """
    if count is None:
        count = monster_passive_count(level, len(character.passives))

    # 玩家被动 -> 怪物化（削弱版）
    player_flipped = [_flip_for_monster(p) for p in character.passives]
    # 玩家原版被动（用于"从玩家身上抽取"时不削弱的部分）
    player_original = [Passive(p.trigger, p.effect, name=p.name) for p in character.passives]
    # 系统预设被动池（从配置文件读取，可自定义）
    from .passive_pool import load_pool
    system_pool = [Passive.parse(t) for t in load_pool()]

    # 去重辅助（按 str(p) 排重）
    def _unique(items):
        seen, out = set(), []
        for x in items:
            if str(x) not in seen:
                seen.add(str(x))
                out.append(x)
        return out

    picked: List[Passive] = []
    picked_strs: set = set()

    def _try_extend(items, n):
        nonlocal picked, picked_strs
        for x in items:
            if n <= 0:
                break
            if str(x) in picked_strs:
                continue
            picked.append(x)
            picked_strs.add(str(x))
            n -= 1
        return n

    # 步骤 1：先把玩家削弱被动尽量多放进去（不超过 count）
    if player_flipped:
        shuffled = list(player_flipped)
        rng.shuffle(shuffled)
        _try_extend(shuffled, count)
    # 步骤 2：从玩家原版被动补足（不削弱，多出来的部分）
    if len(picked) < count and player_original:
        shuffled = list(player_original)
        rng.shuffle(shuffled)
        _try_extend(shuffled, count - len(picked))
    # 步骤 3：从系统池补足
    if len(picked) < count and system_pool:
        shuffled = list(system_pool)
        rng.shuffle(shuffled)
        _try_extend(shuffled, count - len(picked))
    # 兜底：玩家+系统池全空时给一个狂暴
    if not picked:
        return [Passive("攻击", "狂暴：攻击力提升")]
    return picked


def _flip_for_monster(p: Passive) -> Passive:
    """把玩家被动改写成怪物版被动（对抗性，并削弱数值）。

    基于 passive_engine 解析效果类型，把玩家被动映射为怪物可用的削弱版，
    保证"角色被动分享给敌方单位"，而不是靠少量关键词硬编码覆盖。

    怪物版被动会保留玩家被动设置的名字（若玩家未命名则用效果类型作默认名），
    使怪物在抽取/展示时能按名字直接引用，便于辨识来源。
    """
    from . import passive_engine as pe
    # 名字：优先用玩家命名；未命名则用效果类型关键词作为默认名
    default_names = {
        pe.EFF_LIFESTEAL: "吸血", pe.EFF_REFLECT: "荆棘", pe.EFF_SHIELD: "装甲",
        pe.EFF_HEAL: "汲取", pe.EFF_DODGE: "潜行",
        pe.EFF_EXECUTE: "斩杀", pe.EFF_CRIT_UP: "致命", pe.EFF_COMBO_UP: "连击",
        pe.EFF_INVULN: "无敌", pe.EFF_REVIVE: "复活", pe.EFF_STEAL: "偷取",
        pe.EFF_EXTRA_DMG: "重击", pe.EFF_BERSERK: "狂暴",
    }
    name = (p.name or "").strip()
    pp = pe.parse(str(p))
    if pp.ok:
        kind = pp.effect.kind
        if not name:
            name = default_names.get(kind, "狂暴")

    def M(trigger: str, effect: str) -> Passive:
        # 若默认名恰好就是效果首词（如"装甲：装甲：..."），则不附加名字以避免重复
        actual_name = name
        if actual_name and (effect.startswith(actual_name + "：") or effect.startswith(actual_name + ":")):
            actual_name = ""
        return Passive(trigger, effect, name=actual_name)

    if pp.ok:
        kind = pp.effect.kind
        # 各类效果改写为怪物版（数值更弱，构成对抗而不至于太强）
        if kind == pe.EFF_LIFESTEAL:
            return M("攻击", "吸血：攻击时回复伤害的 10%")
        if kind in (pe.EFF_REFLECT, pe.EFF_THORN):
            return M("受击", "荆棘：受到攻击时反弹 2 点伤害")
        if kind == pe.EFF_SHIELD:
            return M("回合", "装甲：每回合开始获得 2 点临时护盾")
        if kind == pe.EFF_HEAL:
            return M("攻击", "汲取：攻击时回复 2 点生命")
        if kind == pe.EFF_DODGE:
            return M("受击", "潜行：第一回合闪避")
        if kind == pe.EFF_EXECUTE:
            return M("攻击", "斩杀：对生命低于 30% 的目标追加伤害")
        if kind == pe.EFF_CRIT_UP:
            return M("攻击", "致命：攻击更容易暴击")
        if kind == pe.EFF_COMBO_UP:
            return M("攻击", "连击：攻击次数+1")
        if kind == pe.EFF_INVULN:
            return M("受击", "获得 1 次无敌")
        if kind == pe.EFF_REVIVE:
            return M("死亡", "死亡时 30% 概率复活")
        if kind in (pe.EFF_ATK_UP, pe.EFF_TEMP_ATK):
            return M("攻击", "狂暴：攻击力提升")
        if kind == pe.EFF_BERSERK:
            # 狂暴：保留玩家写的生命阈值条件（高于/低于），数值削弱为原值一半。
            # 避免像其他效果那样兜底成无条件 +5，丢失条件与数值。
            amt = pp.effect.amount / 2.0
            rto = pp.effect.ratio / 2.0
            parts = []
            if amt > 0:
                parts.append(f"{amt:g}点")
            if rto > 0:
                parts.append(f"{rto*100:.0f}%")
            if not parts:
                parts.append("5点")
            cond_str = ""
            if pp.condition.hp_ratio is not None:
                d = "低于" if pp.condition.hp_direction != "above" else "高于"
                cond_str = f"生命{d}{pp.condition.hp_ratio*100:.0f}%时 "
            return M("受击", f"狂暴：{cond_str}攻击力提升 {'、'.join(parts)}")
        if kind == pe.EFF_STEAL:
            return M("战斗开始", "偷取敌方 5% 攻击力")
        if kind == pe.EFF_EXTRA_DMG:
            return M("攻击", "重击：额外造成 1 点伤害")
        # 其余类型兜底为狂暴
    return M("攻击", "狂暴：攻击力提升")


def generate_level_wave(
    character: Character, level: int, multiwave: bool,
    rng: random.Random = None, battle_history: Optional[List[dict]] = None,
) -> List[Monster]:
    """生成一个关卡的一波敌人。

    按普通属性预算生成怪物，深度线性调整稀有属性与被动。
    battle_history 仅供 AI 模式使用（离线模式忽略）。
    """
    rng = rng or random
    budget, waves = monster_budget(character, level, multiwave)
    mtype = _pick_counter_monster(character.stats)
    scale = _rare_scale(level)

    # 均分预算到每只怪
    per_wave_normal = budget / waves
    base = _type_base(mtype)

    # 怪物属性分配（普通属性之和 = per_wave_normal 保持不变，但控制血量避免过肉）：
    # - 攻击：相对玩家攻击设定（略低，保证玩家稳定输出）
    # - 生命：按"玩家约 4~6 击击杀"设定，避免血牛
    # - 防御：承担剩余预算，作为差额平衡
    player_atk = max(1.0, character.get("攻击力", 10))
    player_def = character.get("防御力", 0)
    # 模板攻击倾向系数（0.6~1.0）：岩傀儡偏防御(0.6)，刺客偏输出(1.0)
    atk_tendency = 0.6 + 0.4 * (base["atk"] / 20.0)
    atk = player_atk * atk_tendency * (0.8 + 0.2 * scale)

    # 玩家单次对怪物伤害估计（含暴击/连击期望），用于设定血量
    player_crit = max(0.0, character.get("暴击率", 0))
    player_combo = max(0.0, character.get("连击率", 0))
    crit_mult = 2.0 if player_crit > 0 else 1.0
    combo_hits = max(1, int(player_combo) + (1 if (player_combo - int(player_combo)) > 0.5 else 0))
    # 期望单回合伤害 ≈ 攻击 × 暴击期望 × 连击次数 - 怪物防御
    eff_attack = player_atk * (1.0 + player_crit * (crit_mult - 1.0) * 0.5) * combo_hits
    # 怪物血量：玩家约 4~6 击击杀（随深度略增）
    hp = max(15.0, eff_attack * (4 + level * 0.2))

    # 防御承担剩余预算（保持总和约束），但给一个相对合理的上限
    rest_budget = per_wave_normal - atk - hp
    defense = max(0.0, min(rest_budget, player_def + 6))

    monsters = []
    passives = draw_monster_passives(character, level, rng)
    for _ in range(waves):
        monsters.append(Monster(
            mtype=mtype,
            hp=hp,
            atk=atk,
            defense=defense,
            # 稀有属性随深度线性提升
            crit_rate=min(1.0, base["crit"] * scale),
            combo_rate=base["combo"] * scale,
            color=base["color"],
            passives=list(passives),
        ))
    return monsters


def _type_base(mtype: str) -> dict:
    from .enemy import MONSTER_TYPES
    return MONSTER_TYPES[mtype]


# ---------- 战后三选一强化 ----------
def roll_reinforcement(level: int, multiwave: bool, rng: random.Random) -> List[str]:
    """从 5 属性池中随机 3 个属性，每个出现概率均等（1/5）。"""
    return rng.sample(ALL_ATTRS, k=REINFORCE_CHOICES)


def reinforcement_value(attr: str, multiwave: bool, character: Character = None) -> float:
    """计算某个强化属性在本次选择的实际提升量。

    普通属性按角色当前值的百分比提升（percent）；稀有属性保持固定加法（fixed）。
    多波模式统一乘倍率。character 用于按比例计算，不可用时空缺时退回固定值。
    """
    cfg = REINFORCE_AMOUNT.get(attr, {"mode": "fixed", "value": 1.0})
    mode = cfg.get("mode", "fixed")
    value = cfg.get("value", 1.0)
    if mode == "percent" and character is not None:
        base = max(0.0, character.get(attr, 0.0))
        value = base * value
    if multiwave:
        value *= MULTIWAVE_REINFORCE_MULT
    return value


def apply_reinforcement(
    character: Character, attr: str, value: float,
) -> str:
    """把强化数值加到角色属性上（加法），返回描述文本。"""
    character.add_stat(attr, value)
    # 生命值强化时同步提高当前生命
    if attr == "生命值":
        character.stats["生命值"] = character.get("生命值")
    return f"{attr} +{value:.2f}"


# ---------- 治疗换算 ----------
def heal_exchange_amount(character: Character, lost_hp: float, x_percent: float) -> float:
    """放弃 X% 强化，回复 Y% 已损失生命值。返回回复量。"""
    heal = lost_hp * (x_percent / 100.0) * HEAL_EXCHANGE_RATIO
    return heal


def enter_level_heal(character: Character) -> float:
    """进入关卡自动回复当前生命上限的 10%，返回回复量。"""
    heal = character.get("生命值") * HEAL_ON_ENTER_RATIO
    character.stats["生命值"] = character.get("生命值")  # 保持上限不变
    return heal

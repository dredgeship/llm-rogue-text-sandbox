"""战斗逻辑：回合制战斗 + 伤害结算 + 被动触发。

伤害结算公式（单次攻击）：
1. 暴击判定：按暴击率是否触发；暴击率>1 时，整数部分=必定暴击，
   小数部分=额外暴击概率，超额部分按 1:1 转为暴击伤害。
2. 基础伤害 = 攻击力 × 暴击倍率（未暴击 ×1，暴击默认 ×2）
3. 最终伤害 = 基础伤害 - 防御力；≤0 强制 1 点。
4. 连击：连击率整数部分=攻击次数，小数部分=额外攻击一次的概率；
   每次连击攻击独立判暴击、独立结算防御。

被动系统由 passive_engine 统一解析（条件层 + 效果层）：
- 触发时机：击杀/回合/攻击/受击/暴击/连击/数值/死亡/复活/战斗开始/战斗结束
- 可携带条件：概率、生命阈值、回合数、首回合
- 效果：属性叠加/回血/吸血/护盾/反弹/额外伤害/斩杀/无敌/属性转移/
  偷取/临时Buff/复活/狂暴/闪避
- 解析失败的被动在战斗日志中提示"未生效"，绝不静默忽略。
"""

from dataclasses import dataclass, field
import random
from typing import List, Optional, Tuple

from .character import Character, Passive
from .enemy import Monster
from . import passive_engine
from .passive_engine import (Condition, Effect, ParsedPassive,
                             EFF_ATK_UP, EFF_DEF_UP, EFF_HP_UP, EFF_CRIT_UP,
                             EFF_COMBO_UP, EFF_HEAL, EFF_LIFESTEAL,
                             EFF_SHIELD, EFF_REFLECT, EFF_EXTRA_DMG,
                             EFF_EXECUTE, EFF_INVULN,
                             EFF_TRANSFER, EFF_STEAL, EFF_DODGE,
                             EFF_TEMP_ATK, EFF_TEMP_DEF, EFF_REVIVE,
                             EFF_BERSERK)

# 硬性回合上限：回合数到 100 强制失败（即第 100 回合仍可行动，超时判负）。
# 动态估算出的上限不得超过该值；UI 显示取 min(动态上限, HARD_TURN_LIMIT)。
HARD_TURN_LIMIT = 99


def roll_crit(crit_rate: float, rng: random.Random) -> float:
    """根据暴击率计算单次攻击的暴击倍率。

    暴击规则（crit_rate 可 >1）：
    - 0 < crit_rate < 1：按概率触发暴击，暴击伤害为默认 2 倍。
    - crit_rate >= 1：必定暴击，且超额部分按 1:1 转为暴击伤害。
      总暴击倍率 = 1 + crit_rate。
    - crit_rate <= 0：永不暴击，倍率 1.0。
    """
    if crit_rate >= 1.0:
        return 1.0 + crit_rate
    if crit_rate <= 0:
        return 1.0
    return 2.0 if rng.random() < crit_rate else 1.0


@dataclass
class Combatant:
    """战斗单位。"""

    name: str
    atk: float
    defense: float
    max_hp: float
    hp: float
    crit_rate: float = 0.0
    combo_rate: float = 0.0
    passives: List[Passive] = field(default_factory=list)
    is_player: bool = False

    # 战斗内临时状态
    temp_atk: float = 0.0
    temp_def: float = 0.0
    temp_crit: float = 0.0
    temp_combo: float = 0.0
    temp_shield: float = 0.0
    turn: int = 0
    # 闪避
    evade: bool = False
    evade_chance: float = 0.0
    # 吸血：比例（0~1，按本次造成伤害）+ 固定值（每次攻击固定回复量）
    _lifesteal_ratio: float = 0.0
    _lifesteal_amount: float = 0.0
    # 无敌剩余次数（>0 时免疫伤害）
    invuln_turns: int = 0
    # 死亡标志（死亡后是否已进入待复活状态）
    dead: bool = False
    # 复活概率（0~1），死亡时判定
    _revive_chance: float = 0.0
    # 狂暴：生命满足阈值时攻击力提升量。固定值（加法）+ 比例（按自身攻击力）
    _berserk_ratio: float = 0.0
    _berserk_ratio_pct: float = 0.0
    # 狂暴触发的生命阈值与方向（低于/高于）
    _berserk_hp_below: float = 0.0   # 0 表示不启用"低于"
    _berserk_hp_above: float = 0.0   # 0 表示不启用"高于"
    # 死亡报告：记录最后一次受到伤害的来源描述与伤害数值（用于我方/敌方死亡结算）
    last_death_source: str = ""
    last_fatal_dmg: float = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def eff_atk(self) -> float:
        return self.atk + self.temp_atk

    @property
    def eff_def(self) -> float:
        return self.defense + self.temp_def

    @property
    def eff_crit(self) -> float:
        return self.crit_rate + self.temp_crit

    @property
    def eff_combo(self) -> float:
        return self.combo_rate + self.temp_combo

    def heal(self, amount: float) -> float:
        """治疗，返回实际回复量（不超过上限）。"""
        if amount <= 0:
            return 0.0
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        if self.hp > 0:
            self.dead = False
        return self.hp - before

    def take_damage(self, dmg: float, source: str = "") -> float:
        """受到伤害。先被护盾吸收，再扣血。返回实际扣血量。

        source：伤害来源描述（如"哥布林 的攻击"、"反弹"），用于死亡报告；
        血量归零时记录最后一次伤害来源与伤害数值。
        """
        if self.invuln_turns > 0:
            return 0.0  # 无敌：免疫伤害
        absorbed = min(self.temp_shield, dmg)
        self.temp_shield -= absorbed
        remaining = dmg - absorbed
        self.hp -= remaining
        if self.hp <= 0:
            self.hp = 0.0
            self.dead = True
            self.last_fatal_dmg = remaining
            if source:
                self.last_death_source = source
        return dmg

    def add_stat(self, attr: str, amount: float):
        """加法修改属性（用于被动叠加，如攻击时暴击率+1）。"""
        if attr == "攻击力":
            self.atk += amount
        elif attr == "防御力":
            self.defense += amount
        elif attr == "生命值":
            self.max_hp += amount
            self.hp += amount
        elif attr == "暴击率":
            self.crit_rate += amount
        elif attr == "连击率":
            self.combo_rate += amount


class Battle:
    """回合制战斗引擎（含被动触发）。"""

    def __init__(self, character: Character, monsters: List[Monster],
                 rng: random.Random = None,
                 boss_mode: bool = False,
                 auto_turn_limit: bool = True):
        self.rng = rng or random
        self.log: List[str] = []
        self._character = character  # 用于战斗结束后把叠加属性同步回角色
        self.boss_mode = boss_mode  # 最后一关 boss 战：玩家获得正面buff减半、boss受负面减半

        # 回合限制：所有战斗都启用（防止护盾/回血导致死循环）。
        # 用最近 TURNLIMIT_WINDOW 回合的平均伤害估算剩余回合数。
        self.turn_limit: Optional[int] = None
        self._auto_turn_limit = auto_turn_limit
        self._dmg_history: List[float] = []   # 玩家每回合输出伤害记录
        self._post5_limit_set = False         # 第6回合后是否已按算法首次设定上限
        self._first_turn_done = False
        self.timed_out = False

        # 玩家属性
        final_stats = dict(character.stats)
        self.player = Combatant(
            name=character.name,
            atk=final_stats.get("攻击力", 10),
            defense=final_stats.get("防御力", 0),
            max_hp=final_stats.get("生命值", 100),
            hp=character.current_hp,
            crit_rate=final_stats.get("暴击率", 0),
            combo_rate=final_stats.get("连击率", 0),
            passives=character.passives,
            is_player=True,
        )
        self.monsters = [
            Combatant(
                name=m.mtype,
                atk=m.atk,
                defense=m.defense,
                max_hp=m.hp,
                hp=m.hp,
                crit_rate=m.crit_rate,
                combo_rate=m.combo_rate,
                passives=m.passives,
            )
            for m in monsters
        ]

        # 应用永久数值型被动
        self._apply_permanent_passives()
        # 战斗开始触发
        self.log.append("战斗开始！")
        self._append_boss_rules_hint()  # boss 战标记减半规则说明
        self._trigger(self.player, "战斗开始")
        for m in self.monsters:
            self._trigger(m, "战斗开始")

    # ---------- 被动系统 ----------
    def _apply_permanent_passives(self):
        """处理 [数值] 时机或显式"永久"的被动，作为跨战斗永久属性加成。

        未写明永久/临时的属性叠加默认是战斗内临时（由 _execute_effect 处理，
        战斗结束自动失效），避免"没写明白就默认永久"导致的跨战斗滚雪球。
        """
        for unit in [self.player] + self.monsters:
            for p in unit.passives:
                pp = passive_engine.parse(str(p))
                if not pp.ok:
                    continue
                if pp.condition.trigger == "数值" and pp.effect.permanent:
                    self._execute_effect(unit, pp, None, None)

    def _trigger(self, unit: Combatant, trigger: str,
                 target: Optional[Combatant] = None,
                 source: Optional[Combatant] = None) -> bool:
        """触发单位某类时机的所有被动。

        对每个被动：passive_engine 解析 → 条件判定 → 效果执行。
        解析失败或条件不满足的被动会记录到日志（提示未生效），不静默忽略。
        返回是否至少有一个被动成功执行。
        """
        fired = False
        for p in unit.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok:
                # 解析失败：提示未生效
                self.log.append(f"  [被动] {unit.name} 的被动'{p}'无法解析：{pp.reason}（未生效）")
                continue
            if pp.condition.trigger != trigger:
                continue
            if pp.effect.kind == EFF_BERSERK:
                # 狂暴的生命阈值是攻击时持续判断的（见伤害计算），
                # 不能在触发时用单位自身血量拦截，否则满血时狂暴永远不设置。
                # 仅跳过 hp 条件，概率/回合/首回合等仍参与判定。
                cond_ok = pp.condition.check(unit, ignore_hp=True)
            else:
                cond_ok = pp.condition.check(unit)
            if not cond_ok:
                continue
            if self._execute_effect(unit, pp, target, source):
                fired = True
        return fired

    def _execute_effect(self, unit: Combatant, pp: ParsedPassive,
                        target: Optional[Combatant],
                        source: Optional[Combatant]) -> bool:
        """执行单个已解析的被动效果。返回是否成功执行。"""
        eff = pp.effect
        name = unit.name
        # 战斗开始/回合等无 target 时，自动选第一个敌人（玩家→怪物 / 怪物→玩家）
        # 这样首回合型额外伤害、反弹等需要目标的效果能正常触发。
        if target is None:
            target = self._enemy_of(unit)
        try:
            # boss 战减半规则：
            # 1) 玩家获得的正面 buff 效果减半（_halve_self_buff）
            # 2) boss 受到的负面效果减半（_halve_on_target）
            # 次数/布尔型效果（无敌）减半后至少保留 1，避免直接失效。
            amt = _scale_amount(self, eff.kind, unit, eff.amount)
            rto = _scale_ratio(self, eff.kind, unit, eff.ratio)
            amt_int = max(1, int(amt)) if _halve_self_buff(self, unit, eff.kind) else int(amt)

            if eff.kind == EFF_DODGE:
                # 闪避：实际判定在 _attack 中用 _should_dodge 按条件实时进行，
                # 这里只记录提示，不再设置持久字段（避免"第一回合闪避"等条件
                # 闪避被一次触发后错误地永久生效）。
                self.log.append(f"  [被动] {name} 获得闪避（受击时按条件判定）")
                return True

            if eff.kind == EFF_LIFESTEAL:
                # 吸血：比例（按本次造成伤害）+ 固定值（每次攻击固定回复量）
                unit._lifesteal_ratio = max(unit._lifesteal_ratio, rto)
                unit._lifesteal_amount = max(unit._lifesteal_amount, amt)
                parts = []
                if rto > 0:
                    parts.append(f"{rto*100:.0f}%")
                if amt > 0:
                    parts.append(f"{amt:g}点")
                self.log.append(f"  [被动] {name} 获得吸血（{'、'.join(parts) if parts else '默认15%'}）")
                return True

            if eff.kind == EFF_HEAL:
                # 回血：固定值 + 按生命比例（基数=max/lost/current）
                base = self._hp_ratio_base(unit, eff.ratio_base)
                healed = unit.heal(amt + rto * base)
                self.log.append(f"  [被动] {name} 回复 {healed:.1f} 点生命")
                return True

            if eff.kind == EFF_SHIELD:
                # 护盾：固定值 + 按生命比例（基数=max/lost/current）
                base = self._hp_ratio_base(unit, eff.ratio_base)
                unit.temp_shield += amt + rto * base
                self.log.append(f"  [被动] {name} 获得 {amt + rto * base:.0f} 点护盾")
                return True

            if eff.kind == EFF_REFLECT:
                if target is not None:
                    amt_t = _scale_on_target(self, eff.kind, target, eff.amount)
                    target.take_damage(amt_t, source=f"{name} 的反弹")
                    self.log.append(f"  [被动] {name} 反弹 {amt_t:.0f} 点伤害")
                    return True
                return False

            if eff.kind == EFF_EXTRA_DMG:
                if target is not None:
                    amt_t = _scale_on_target(self, eff.kind, target, eff.amount)
                    target.take_damage(amt_t, source=f"{name} 的额外伤害")
                    self.log.append(f"  [被动] {name} 造成 {amt_t:.0f} 点额外伤害")
                    return True
                return False

            if eff.kind == EFF_EXECUTE:
                # 斩杀：目标血量低于阈值时立即死亡（boss 受到的斩杀阈值减半）
                if target is not None and target.max_hp > 0:
                    ratio = _scale_on_target(self, eff.kind, target, eff.ratio)
                    if target.hp / target.max_hp < ratio:
                        target.take_damage(target.hp, source=f"{name} 的斩杀")  # 直接击杀，血量归零
                        self.log.append(f"  [被动] {name} 斩杀，{target.name} 被立即处决！")
                        return True
                return False

            if eff.kind == EFF_INVULN:
                unit.invuln_turns += amt_int
                self.log.append(f"  [被动] {name} 获得 {amt_int} 次无敌")
                return True

            if eff.kind == EFF_TRANSFER:
                # 属性转移：把 unit 的某属性转为另一属性（自身）。固定值 + 比例（按源属性当前值）
                src = eff.attr
                dst = _find_transfer_target(eff.raw, src)
                if src and dst:
                    val = amt + getattr(unit, _attr_field(src), 0.0) * rto
                    if val > 0:
                        _add_stat_attr(unit, src, -val)
                        _add_stat_attr(unit, dst, val)
                        desc = f"{amt:g}点" if amt > 0 and rto <= 0 else \
                               f"{rto*100:.0f}%" if rto > 0 and amt <= 0 else \
                               f"{amt:g}点+{rto*100:.0f}%"
                        self.log.append(f"  [被动] {name} 把 {desc} {src} 转为 {dst}")
                        return True
                return False

            if eff.kind == EFF_STEAL:
                # 偷取/吸取敌方属性给自身（偷取 boss 时减半）。固定值 + 比例（按被偷属性当前值）
                victim = target or source or self._enemy_of(unit)
                if victim is not None:
                    attr = eff.attr
                    # 固定值（boss 减半）+ 比例（boss 减半）
                    amount = _scale_on_target(self, eff.kind, victim, eff.amount) \
                        + getattr(victim, _attr_field(attr), 0.0) \
                        * _scale_on_target(self, eff.kind, victim, eff.ratio)
                    amount = min(amount, max(0.0, getattr(victim, _attr_field(attr), 0.0)))
                    if amount > 0:
                        _add_stat_attr(victim, attr, -amount)
                        _add_stat_attr(unit, attr, amount)
                        self.log.append(f"  [被动] {name} 偷取 {attr} {amount:.1f}")
                        return True
                return False

            if eff.kind in (EFF_ATK_UP, EFF_DEF_UP, EFF_HP_UP, EFF_CRIT_UP, EFF_COMBO_UP):
                if eff.permanent:
                    # 永久加成：加到基础字段，跨战斗同步回角色（玩家永久加成在 boss 战减半）
                    unit.add_stat(eff.attr, amt)
                    self.log.append(f"  [被动] {name} {eff.attr} +{amt}（永久）")
                else:
                    # 战斗内临时叠加：加到 temp 字段，战斗结束自动失效
                    _add_temp_stat(unit, eff.attr, amt)
                    self.log.append(f"  [被动] {name} {eff.attr} +{amt}（本场战斗）")
                return True

            if eff.kind == EFF_TEMP_ATK:
                unit.temp_atk += amt
                self.log.append(f"  [被动] {name} 攻击力临时 +{amt}")
                return True

            if eff.kind == EFF_TEMP_DEF:
                unit.temp_def += amt
                self.log.append(f"  [被动] {name} 防御力临时 +{amt}")
                return True

            if eff.kind == EFF_REVIVE:
                # 复活只能由"死亡"时机触发：非死亡时机（如"回合"/"攻击"）配置复活视为无效，
                # 避免"经非死亡方式触发复活"这种反直觉组合。
                if pp.condition.trigger != "死亡":
                    self.log.append(f"  [被动] {name} 的复活效果只能在[死亡]时机触发，当前为[{pp.condition.trigger}]（未生效）")
                    return False
                unit._revive_chance = max(unit._revive_chance, rto)
                return True

            if eff.kind == EFF_BERSERK:
                # 记录攻击力提升量（固定值 + 比例）与生命阈值方向；
                # 攻击时按当前血量是否满足阈值生效。
                unit._berserk_ratio = max(unit._berserk_ratio, amt)
                unit._berserk_ratio_pct = max(unit._berserk_ratio_pct, rto)
                if pp.condition.hp_direction == "above":
                    unit._berserk_hp_above = max(unit._berserk_hp_above, pp.condition.hp_ratio or 0.0)
                elif pp.condition.hp_ratio is not None:
                    unit._berserk_hp_below = max(unit._berserk_hp_below, pp.condition.hp_ratio or 0.0)
                parts = []
                if amt > 0:
                    parts.append(f"{amt:g}点")
                if rto > 0:
                    parts.append(f"{rto*100:.0f}%")
                self.log.append(f"  [被动] {name} 获得狂暴（攻击力提升 {'、'.join(parts) if parts else '5点'}）")
                return True

        except Exception as e:
            self.log.append(f"  [被动] {name} 被动执行出错：{e}（未生效）")
            return False

        self.log.append(f"  [被动] {name} 的被动'{pp.raw}'未识别到具体效果（未生效）")
        return False

    def _try_revive(self, unit: Combatant):
        """死亡判定：若单位有复活被动，按概率复活并恢复部分生命。"""
        if not unit.dead or unit.hp > 0:
            return False
        if unit._revive_chance > 0 and self.rng.random() < unit._revive_chance:
            unit.hp = unit.max_hp * 0.5
            unit.dead = False
            unit.invuln_turns = 1
            self.log.append(f"  [被动] {unit.name} 复活了！恢复部分生命")
            self._trigger(unit, "复活")
            return True
        return False

    # ---------- 目标选取 ----------
    def _enemy_of(self, unit: Combatant) -> Optional[Combatant]:
        """返回单位的敌方（用于偷取/吸血等需要跨单位操作的场景）。"""
        if unit.is_player:
            alive = [x for x in self.monsters if x.alive]
            return alive[0] if alive else (self.monsters[0] if self.monsters else None)
        return self.player

    def _select_target(self, attacker: Combatant, prefer: Optional[Combatant] = None) -> Optional[Combatant]:
        """为攻击者选取目标。优先 prefer；否则第一个存活敌人。"""
        # 攻击者是玩家：目标是怪物；攻击者是怪物：目标是玩家
        pool = self.monsters if attacker.is_player else [self.player]
        alive = [x for x in pool if x.alive]
        if not alive:
            return None
        if prefer in alive:
            return prefer
        return alive[0]

    # ---------- 伤害结算 ----------
    def _attack(self, attacker: Combatant, defender: Combatant) -> float:
        """一次完整攻击：判定闪避、无敌、暴击、连击、斩杀、吸血、结算防御。

        返回总伤害（若被闪避/无敌则返回 0）。
        """
        # 先触发攻击方的"攻击"被动（如"每次攻击暴击率+1"）
        self._trigger(attacker, "攻击", defender)
        # 触发受击方的"受击"被动（如闪避、反弹）
        self._trigger(defender, "受击", attacker)

        # 无敌判定
        if defender.invuln_turns > 0:
            defender.invuln_turns -= 1
            self.log.append(f"  {defender.name} 处于无敌，免疫这次攻击！")
            return 0.0

        total = 0.0
        hit_count = 0
        crit_hit = False
        # 斩杀结算：攻击方有斩杀被动且目标低血量时，附加斩杀伤害（boss 受到的斩杀减半）
        for p in attacker.passives:
            pp = passive_engine.parse(str(p))
            if pp.ok and pp.condition.trigger == "攻击" and pp.effect.kind == EFF_EXECUTE:
                ratio = _scale_on_target(self, EFF_EXECUTE, defender, pp.effect.ratio)
                if defender.max_hp > 0 and defender.hp / defender.max_hp < ratio:
                    ex = _scale_on_target(self, EFF_EXECUTE, defender, pp.effect.amount)
                    ex = ex if ex > 0 else defender.max_hp * 0.3
                    defender.take_damage(ex, source=f"{attacker.name} 的斩杀")  # 先由护盾吸收，再扣血
                    total += ex
                    self.log.append(f"  [被动] {attacker.name} 斩杀，对 {defender.name} 追加 {ex:.1f} 伤害！")
        # 连击次数（含战斗内临时叠加）
        combo = attacker.eff_combo
        hits = int(combo)
        extra_prob = combo - hits
        if extra_prob > 0 and self.rng.random() < extra_prob:
            hits += 1
        hits = max(1, hits)

        # 第一击判定闪避：实时按受击被动与当前状态（回合/生命）判定，
        # 支持"第一回合闪避""X%概率闪避""必定闪避"等条件写法。
        # 第一击判定：闪避则整次攻击无效（保持单连击下只判定一次，避免概率翻倍）。
        if self._should_dodge(defender):
            self.log.append(f"  {defender.name} 闪避了攻击！")
            return 0.0

        # 攻击循环：第一击已在上方判过闪避，直接结算；后续每击继续判闪避。
        for hit_idx in range(hits):
            if hit_idx > 0 and self._should_dodge(defender):
                self.log.append(f"  {defender.name} 闪避了第 {hit_count+1} 击！")
                hit_count += 1
                continue
            per_crit = roll_crit(attacker.eff_crit, self.rng)
            if per_crit > 1.0:
                crit_hit = True
            base = attacker.eff_atk * per_crit
            dmg = max(1.0, base - defender.eff_def)
            # 狂暴：生命满足阈值（低于/高于）时提升攻击力（固定值 + 按自身攻击力比例）。
            # 未设置任何 hp 阈值时视为无条件生效。
            if (attacker._berserk_ratio > 0 or attacker._berserk_ratio_pct > 0) \
                    and attacker.max_hp > 0:
                hp_ratio = attacker.hp / attacker.max_hp
                if attacker._berserk_hp_below > 0 or attacker._berserk_hp_above > 0:
                    berserk_ok = hp_ratio < attacker._berserk_hp_below \
                        if attacker._berserk_hp_below > 0 else False
                    if attacker._berserk_hp_above > 0 and hp_ratio > attacker._berserk_hp_above:
                        berserk_ok = True
                else:
                    berserk_ok = True  # 无条件狂暴
                if berserk_ok:
                    dmg += attacker._berserk_ratio \
                        + attacker.eff_atk * attacker._berserk_ratio_pct
            defender.take_damage(dmg, source=f"{attacker.name} 的攻击")  # 先由护盾吸收，再扣血
            total += dmg
            hit_count += 1

        # 吸血：固定值 + 按本次造成总伤害比例
        if (attacker._lifesteal_ratio > 0 or attacker._lifesteal_amount > 0) and total > 0:
            heal = attacker._lifesteal_amount + total * attacker._lifesteal_ratio
            attacker.heal(heal)
            self.log.append(f"  [被动] {attacker.name} 吸血回复 {heal:.1f}")

        # 反弹/额外伤害的比例部分：按本次实际伤害实时结算（固定值部分已由
        # 攻击/受击触发时在 _execute_effect 结算，这里只补按比例的部分）。
        # 反弹：defender 受击方按本次受到的伤害比例反弹给 attacker；
        # 额外伤害：attacker 攻击方按本次造成伤害比例追加给 defender。
        reflect_ratio = self._scaled_dmg_ratio(defender, EFF_REFLECT)
        if reflect_ratio > 0 and total > 0:
            r = _scale_on_target(self, EFF_REFLECT, attacker, reflect_ratio)
            r_dmg = total * r
            if r_dmg > 0:
                attacker.take_damage(r_dmg, source=f"{defender.name} 的反弹")
                self.log.append(f"  [被动] {defender.name} 反弹 {r_dmg:.1f} 点伤害")
        extra_ratio = self._scaled_dmg_ratio(attacker, EFF_EXTRA_DMG)
        if extra_ratio > 0 and total > 0:
            r = _scale_on_target(self, EFF_EXTRA_DMG, defender, extra_ratio)
            e_dmg = total * r
            if e_dmg > 0:
                defender.take_damage(e_dmg, source=f"{attacker.name} 的额外伤害")
                self.log.append(f"  [被动] {attacker.name} 造成 {e_dmg:.1f} 点额外伤害")

        # 暴击/连击触发被动
        if crit_hit:
            self._trigger(attacker, "暴击", defender)
        if hit_count > 1:
            self._trigger(attacker, "连击", defender)

        return total

    def _hp_ratio_base(self, unit: Combatant, ratio_base: str) -> float:
        """回血/护盾按比例时的生命基数。

        ratio_base：max=最大生命；lost=已损失生命；current=当前生命。
        """
        if ratio_base == passive_engine.RATIO_BASE_LOST:
            return max(0.0, unit.max_hp - unit.hp)
        if ratio_base == passive_engine.RATIO_BASE_CURRENT:
            return max(0.0, unit.hp)
        return max(0.0, unit.max_hp)

    def _should_dodge(self, defender: Combatant) -> bool:
        """按受击闪避被动与当前状态（回合数/生命）实时判定本次是否闪避。

        逐条解析 defender 的"受击"闪避被动，用当时的状态判定条件（如首回合、
        回合数、生命阈值），满足则按概率（100% 为必定闪避）掷骰。
        这样"第一回合闪避""X%概率闪避"都能正确生效，且不会一次触发后永久闪避。
        """
        cur_turn = max(1, self.player.turn)  # 当前战斗回合（用玩家回合，最准确）
        for p in defender.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok or pp.effect.kind != EFF_DODGE:
                continue
            c = pp.condition
            # 闪避是"受击时"判定，但兼容 AI 可能生成的 [回合] 时机闪避（如"第一回合闪避"），
            # 只要带首回合/回合数条件就按受击闪避处理，避免不生效。
            if c.trigger != "受击":
                if not (c.first_turn or c.turn_n is not None):
                    continue
            # 首回合/回合数条件：用玩家当前回合判定（怪物 turn 计数滞后，不可靠）
            if c.first_turn and cur_turn != 1:
                continue
            if c.turn_n is not None:
                if c.turn_kind == "once" and cur_turn != c.turn_n:
                    continue
                if c.turn_kind == "every" and (cur_turn % c.turn_n) != 0:
                    continue
            # 概率 / 生命阈值条件：手动判定（避开 check 里基于 defender.turn 的回合判定）
            if c.probability is not None and self.rng.random() >= c.probability:
                continue
            if c.hp_ratio is not None:
                if defender.max_hp <= 0:
                    continue
                hr = defender.hp / defender.max_hp
                if c.hp_direction == "below" and not (hr < c.hp_ratio):
                    continue
                if c.hp_direction == "above" and not (hr > c.hp_ratio):
                    continue
            ratio = _scale_amount(self, EFF_DODGE, defender, pp.effect.amount)
            if ratio >= 1.0:
                return True  # 必定闪避
            if ratio > 0 and self.rng.random() < ratio:
                return True
        return False

    def _scaled_dmg_ratio(self, unit: Combatant, kind: str) -> float:
        """返回 unit 满足触发条件、效果为 kind、比例 >0 的被动的最大比例。

        用于反弹/额外伤害的"按本次伤害比例"部分：仅当对应触发时机匹配且条件
        满足时返回比例，否则 0（不做补算）。固定值部分已由 _execute_effect 结算。
        """
        ratio = 0.0
        trigger = "受击" if kind == EFF_REFLECT else "攻击"
        for p in unit.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok or pp.effect.kind != kind:
                continue
            if pp.condition.trigger != trigger:
                continue
            if pp.condition.check(unit):
                ratio = max(ratio, pp.effect.ratio)
        return ratio

    # ---------- 回合流程 ----------
    def player_turn(self, target_name: str) -> List[str]:
        """玩家回合：攻击指定目标，然后处理被动与怪物反击。"""
        self.log.clear()
        self.player.turn += 1

        target = None
        for m in self.monsters:
            if m.name == target_name:
                target = m
                break
        # 目标选择
        target = self._select_target(self.player, target)
        if target is None:
            alive = self.monsters_alive
            if not alive:
                return self.log
            target = alive[0]

        # 回合触发被动（每回合开始）
        self._trigger(self.player, "回合")

        # 实际攻击
        dmg = self._attack(self.player, target)
        # 首回合 log.clear() 会清掉 __init__ 里的说明，这里重新追加保证可见
        if not self._first_turn_done:
            self._first_turn_done = True
            self._append_boss_rules_hint()
        # 记录本回合输出伤害（用于估算回合上限）
        if self._auto_turn_limit:
            self._dmg_history.append(dmg)
        if dmg > 0:
            self.log.append(f"你攻击 {target.name}，造成 {dmg:.1f} 伤害（剩余 {target.hp:.1f}）")
        else:
            self.log.append(f"你攻击 {target.name}，但被闪避或免疫了")

        # 击杀判定 + 死亡触发 + 复活
        if not target.alive:
            self.log.append(f"  {target.name} 被击败！")
            self._trigger(self.player, "击杀")
            # 敌方死亡触发（如自爆类），以及复活
            self._trigger(target, "死亡")
            if not self._try_revive(target):
                self._remove_dead(target)
            self._sync_player_stats()

        if self.battle_over:
            return self.log

        # 怪物反击
        for m in self.monsters_alive:
            if not self.player.alive:
                break
            m.turn += 1
            self._trigger(m, "回合")
            # 重置玩家闪避
            self.player.evade = False
            self.player.evade_chance = 0.0
            # 怪物攻击玩家
            mdmg = self._attack(m, self.player)
            if mdmg > 0:
                self.log.append(f"{m.name} 攻击你，造成 {mdmg:.1f} 伤害（你剩余 {self.player.hp:.1f}）")
            else:
                self.log.append(f"{m.name} 攻击你，但被闪避或免疫了")
            if not self.player.alive:
                break

        # 每回合末基于最近输出窗口的平均伤害动态估算剩余回合上限
        if self._auto_turn_limit:
            self._update_turn_limit()

        # 回合上限判定：超过则玩家超时判负
        if self._check_turn_timeout():
            return self.log

        return self.log

    def _append_boss_rules_hint(self):
        """boss 战：在战斗日志标记减半规则说明。"""
        if not self.boss_mode:
            return
        self.log.append("【boss规则】本场为最终 boss 战：玩家获得的正面 buff 效果减半，"
                        "boss 受到的负面效果（额外伤害/斩杀/偷取等）减半")

    # ---------- 回合限制估算辅助 ----------
    def _remaining_window(self) -> int:
        """返回估算用的剩余回合窗口（至少 1）。"""
        return max(1, self.effective_turn_limit - self.player.turn)

    def _estimate_unit_turn_regen(self, unit: Combatant) -> float:
        """估算敌方单位每回合的净承受量增长（回血 + 护盾，按回合触发折算）。

        折算规则（简化线性估算）：
        - 无条件 / 每回合触发：amount 即为每回合增长。
        - 每 N 回合（turn_kind=every）：折算为 amount / N 每回合。
        - 第 X 回合一次性（turn_kind=once）：仅当 X 落在剩余窗口内时，
          折算为 amount / 窗口长度。
        """
        if unit is None:
            return 0.0
        window = self._remaining_window()
        regen = 0.0
        for p in unit.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok or pp.condition.trigger != "回合":
                continue
            eff = pp.effect
            if eff.kind not in (EFF_HEAL, EFF_SHIELD):
                continue
            amt = eff.amount
            c = pp.condition
            if c.turn_kind == "every" and c.turn_n:
                regen += amt / c.turn_n
            elif c.turn_kind == "once" and c.turn_n:
                if c.turn_n > self.player.turn:
                    regen += amt / window
            else:
                regen += amt
        return regen

    def _estimate_dodge_miss(self, unit: Combatant) -> float:
        """估算单位受击时的期望闪避概率（0~1），用于回合上限估算的命中折扣。

        规则：
        - 无条件闪避：概率直接计入。
        - 第一回合/第N回合闪避：仅在剩余窗口内可能触发，按触发概率 × 窗口占比折算。
        - 必定闪避：概率为 1。
        """
        if unit is None:
            return 0.0
        window = self._remaining_window()
        miss = 0.0
        for p in unit.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok or pp.effect.kind != EFF_DODGE:
                continue
            if pp.condition.trigger != "受击":
                continue
            ratio = min(1.0, max(0.0, pp.effect.amount))
            c = pp.condition
            # 无条件：全程按概率
            if c.first_turn or c.turn_n is not None:
                # 条件闪避：在剩余窗口内触发概率 × 窗口占比
                active = 1.0
                if c.first_turn:
                    active = 1.0 / window if window > 0 else 0.0
                elif c.turn_kind == "once" and c.turn_n:
                    active = (1.0 / window) if c.turn_n > self.player.turn else 0.0
                elif c.turn_kind == "every" and c.turn_n:
                    active = 1.0 / c.turn_n
                miss = max(miss, ratio * active)
            else:
                miss = max(miss, ratio)
        return miss

    def _estimate_player_growth(self) -> float:
        """估算玩家每回合攻击力净增长（回合触发的临时攻击/属性叠加折算）。

        折算为"净均伤"增量（最终伤害 ≈ 攻击 - 防御，故攻击每 +1 约对应 +1 均伤）。
        """
        window = self._remaining_window()
        growth = 0.0
        for p in self.player.passives:
            pp = passive_engine.parse(str(p))
            if not pp.ok or pp.condition.trigger != "回合":
                continue
            eff = pp.effect
            # 只考虑影响攻击力的战斗内成长/削弱
            if eff.kind == EFF_TEMP_ATK:
                amt = eff.amount
            elif eff.kind == EFF_ATK_UP and not eff.permanent and eff.attr == "攻击力":
                amt = eff.amount
            else:
                continue
            c = pp.condition
            if c.turn_kind == "every" and c.turn_n:
                growth += amt / c.turn_n
            elif c.turn_kind == "once" and c.turn_n:
                if c.turn_n > self.player.turn:
                    growth += amt / window
            else:
                growth += amt
        return growth

    def _estimate_enemy_total(self) -> Tuple[float, float, float]:
        """估算所有存活敌人的：剩余承受量、每回合净恢复、闪避期望折扣。

        返回 (total, regen, dodge_factor)：
        - total：HP + 护盾 + 复活期望（一次性额外承受量）
        - regen：每回合净恢复（回血 + 护盾折算之和）
        - dodge_factor：闪避对均伤的期望折扣（0~1，1=完全命中）
        """
        total = 0.0
        regen = 0.0
        dodge_factor = 1.0
        for m in self.monsters:
            if not m.alive:
                continue
            total += m.hp + m.temp_shield
            regen += self._estimate_unit_turn_regen(m)
            # 复活期望：一次死亡时有概率复活到 50% 最大生命，作为一次性额外承受量
            if m._revive_chance > 0 and m.max_hp > 0:
                total += m._revive_chance * m.max_hp * 0.5
            # 闪避期望：按受击闪避被动折算命中折扣。
            # 无条件闪避按概率折扣；"第一回合/第N回合"等条件闪避按剩余窗口折算。
            miss = self._estimate_dodge_miss(m)
            dodge_factor *= (1.0 - miss)
        if total <= 0:
            total = sum((x.hp + x.temp_shield for x in self.monsters), 0.0)
        return total, regen, dodge_factor

    def _update_turn_limit(self):
        """动态设置回合上限。

        规则：
        - 五回合以内（第 1~5 回合）：上限默认为 10（留有余量，防止溢出）。
        - 超过五回合后：分阶段估算——
          净均伤 = 前五回合实际均伤 × 闪避折扣 + 玩家每回合攻击成长 - 敌方每回合净恢复；
          剩余回合数 = 剩余承受量 ÷ 净均伤；
          总上限 = 当前回合数 + 剩余回合数，封顶 99。
        - 方案A：上限以"当前回合数"为基准累加，保证上限永远 ≥ 当前回合 + 1，
          不会出现"越打上限越小、甚至小于当前回合导致突然判负"的收敛问题。
        - 方案B：第 6 回合按算法一次性估算后，上限只在首次设定、或估算值比当前
          上限更大（玩家成长变强）、或当前上限已逼近当前回合时更新；平时保持
          原上限不变，避免因怪物剩余血量减少而让上限逐回合缩水逼近当前回合。
        - 硬性上限 99：回合数到 100 强制失败（见 effective_turn_limit / _check_turn_timeout）。
        """
        if not self.monsters:
            self.turn_limit = None
            return
        import math

        if self.player.turn <= 5:
            # 五回合以内：默认上限 10，防溢出、留余量
            new_limit = 10
            if self.turn_limit != new_limit:
                self.turn_limit = new_limit
                self.log.append(f"  [限时] 前五回合，回合上限默认 {self.turn_limit}")
            return

        total, regen, dodge_factor = self._estimate_enemy_total()
        # 用前五回合（第 1~5 回合）的实际平均伤害作为基础输出
        first5 = self._dmg_history[:5] if self._dmg_history else []
        if len(first5) < 5:
            first5 = first5 + [0.0] * (5 - len(first5))
        base_avg = sum(first5) / 5.0
        # 玩家成长/削弱修正 + 敌方每回合净恢复扣除
        growth = self._estimate_player_growth()
        gross_avg = base_avg * dodge_factor + growth
        net_avg = gross_avg - regen
        if net_avg > 0:
            # 还需回合数 = 剩余承受量 ÷ 净均伤；总上限 = 当前回合 + 还需回合数，封顶 99。
            # 基准用当前回合数而非写死的 5，保证 new_limit 永远 ≥ 当前回合 + 1。
            remain = max(1, math.ceil(total / net_avg))
            new_limit = min(self.player.turn + remain, HARD_TURN_LIMIT)
        else:
            # 无法净输出击杀（回血/护盾增长 ≥ 输出）：给有限兜底上限，防止死循环
            new_limit = 20
        # 方案B：一次性估算后保持稳定。仅在以下情况更新：
        #  - 尚未设定过上限；
        #  - 估算值比当前上限更大（玩家成长变强，允许放宽）；
        #  - 当前上限已经 ≤ 当前回合（异常兜底，强制放宽到至少当前回合 + 1），
        #    防止因历史估算过紧而意外逼近当前回合。
        need_update = (self.turn_limit is None
                       or new_limit > self.turn_limit
                       or self.turn_limit <= self.player.turn)
        if need_update:
            if net_avg > 0:
                new_limit = max(new_limit, self.player.turn + 1)
            self.turn_limit = new_limit
            self._post5_limit_set = True
            if net_avg > 0:
                self.log.append(
                    f"  [限时] 回合上限 {self.turn_limit}"
                    f"（当前回合 {self.player.turn} + 剩余 {total:.0f} ÷ 净均伤 {net_avg:.1f}"
                    f" = 均伤 {base_avg:.1f} 成长 {growth:.1f} 敌恢复 {regen:.1f}）")
            else:
                self.log.append("  [限时] 无法净输出击杀，回合上限设为 20")

    def _check_turn_timeout(self) -> bool:
        """每回合结束判定是否超时。超时则玩家判负并返回 True。"""
        if self.turn_limit is None or self.battle_over:
            return False
        # 实际生效上限 = min(动态上限, 硬性上限 99)；最晚第 100 回合强制失败
        if self.player.turn > self.effective_turn_limit:
            self.timed_out = True
            self.log.append(
                f"  [限时] 超过 {self.effective_turn_limit} 回合未能击杀敌人，挑战失败！")
            return True
        return False

    def _remove_dead(self, unit: Combatant):
        """把已死亡且未复活的单位从战场移除。"""
        if unit in self.monsters:
            self.monsters.remove(unit)

    def end_battle(self):
        """战斗结束触发（在战斗收尾时调用）。"""
        if not getattr(self, "_battle_end_fired", False):
            self._battle_end_fired = True
            self._trigger(self.player, "战斗结束")
            for m in self.monsters:
                self._trigger(m, "战斗结束")

    def _sync_player_stats(self):
        """把战斗内玩家叠加的属性/生命同步回 Character（跨战斗持久）。"""
        if not hasattr(self, "_character"):
            return
        c = self._character
        c.stats["攻击力"] = self.player.atk
        c.stats["防御力"] = self.player.defense
        c.stats["暴击率"] = self.player.crit_rate
        c.stats["连击率"] = self.player.combo_rate
        c.current_hp = max(0.0, self.player.hp)

    # ---------- 查询 ----------
    @property
    def effective_turn_limit(self) -> int:
        """实际生效的回合上限 = min(动态上限, 硬性上限 99)。"""
        if self.turn_limit is None:
            return HARD_TURN_LIMIT
        return min(self.turn_limit, HARD_TURN_LIMIT)

    @property
    def player_alive(self) -> bool:
        return self.player.alive

    @property
    def monsters_alive(self) -> List[Combatant]:
        return [m for m in self.monsters if m.alive]

    @property
    def player_death_reason(self) -> str:
        """生成玩家死亡报告：死于什么伤害、多少数值。"""
        if self.timed_out:
            return "回合数耗尽，超时判负"
        if self.player.alive:
            return ""
        src = self.player.last_death_source
        dmg = self.player.last_fatal_dmg
        if not src:
            # 兜底：未能定位具体来源
            return f"你的生命被耗尽（最后一击 {dmg:.1f} 伤害）"
        return f"被 {src} 造成 {dmg:.1f} 点伤害击败"

    @property
    def battle_over(self) -> bool:
        # 超时判负也视为战斗结束
        return self.timed_out or not self.player_alive or not self.monsters_alive

    @property
    def player_won(self) -> bool:
        # 超时不算胜利
        return (not self.timed_out) and self.player_alive and not self.monsters_alive

    def sync_hp_to_character(self, character: Character):
        """把战斗后的玩家生命同步回角色（跨关卡持续）。"""
        character.current_hp = max(0.0, self.player.hp)


# ---------------------------------------------------------------------------
# 辅助：属性名 <-> Combatant 字段 / 加法修改
# ---------------------------------------------------------------------------
def _attr_field(attr: str) -> str:
    return {
        "攻击力": "atk",
        "防御力": "defense",
        "生命值": "max_hp",
        "暴击率": "crit_rate",
        "连击率": "combo_rate",
    }.get(attr, "atk")


def _add_stat_attr(unit: Combatant, attr: str, amount: float):
    """直接加/减属性（用于属性转移/偷取的底层操作）。"""
    if attr == "生命值":
        unit.max_hp += amount
        unit.hp = min(unit.hp, unit.max_hp)
    else:
        setattr(unit, _attr_field(attr), getattr(unit, _attr_field(attr)) + amount)


def _add_temp_stat(unit: Combatant, attr: str, amount: float):
    """把属性叠加加到战斗内临时字段（战斗结束自动失效）。

    非"永久"的属性叠加被动走这里，避免写入永久字段后经 _sync_player_stats
    同步回角色造成跨战斗滚雪球。
    """
    if attr == "攻击力":
        unit.temp_atk += amount
    elif attr == "防御力":
        unit.temp_def += amount
    elif attr == "暴击率":
        unit.temp_crit += amount
    elif attr == "连击率":
        unit.temp_combo += amount
    # 生命值不做临时叠加（避免战斗内临时提高上限的复杂性）


# boss 战减半规则：
# - 玩家获得的正面 buff 效果减半（作用于自身的正向效果）
# - boss 受到的负面效果减半（作用于敌方目标的伤害/偷取类效果）
_SELF_BUFF_KINDS = {EFF_DODGE, EFF_LIFESTEAL, EFF_HEAL, EFF_SHIELD,
                    EFF_INVULN, EFF_TRANSFER,
                    EFF_ATK_UP, EFF_DEF_UP, EFF_HP_UP, EFF_CRIT_UP, EFF_COMBO_UP,
                    EFF_TEMP_ATK, EFF_TEMP_DEF, EFF_REVIVE, EFF_BERSERK}
# 作用于敌方目标的负面效果（施加给 target，target 为 boss 时减半）
_TARGET_DEBUFF_KINDS = {EFF_REFLECT, EFF_EXTRA_DMG, EFF_EXECUTE, EFF_STEAL}


def _halve_self_buff(battle: "Battle", unit: Combatant, kind: str) -> bool:
    """玩家获得的正面 buff 是否减半：boss 战中，unit 是玩家且为正向效果。"""
    return battle.boss_mode and unit.is_player and kind in _SELF_BUFF_KINDS


def _halve_on_target(battle: "Battle", target: Optional[Combatant], kind: str) -> bool:
    """boss 受到的负面效果是否减半：boss 战中，target 是 boss（怪物）。"""
    return battle.boss_mode and target is not None and not target.is_player \
        and kind in _TARGET_DEBUFF_KINDS


def _scale_amount(battle: "Battle", kind: str, unit: Combatant, amount: float) -> float:
    """玩家获得正面 buff 的数值量减半（含永久加成）。"""
    if _halve_self_buff(battle, unit, kind) and amount:
        return amount * 0.5
    return amount


def _scale_ratio(battle: "Battle", kind: str, unit: Combatant, ratio: float) -> float:
    """玩家获得正面 buff 的比例减半。"""
    if _halve_self_buff(battle, unit, kind) and ratio:
        return ratio * 0.5
    return ratio


def _scale_on_target(battle: "Battle", kind: str,
                     target: Optional[Combatant], value: float) -> float:
    """boss 受到的负面效果数值/比例减半。"""
    if _halve_on_target(battle, target, kind) and value:
        return value * 0.5
    return value


def _find_transfer_target(text: str, src: str) -> Optional[str]:
    """从"防御转攻击"文本中找出目标属性（src 之外的另一个属性）。"""
    # 匹配 "转X" "转换为X" "转成X" 等
    import re
    m = re.search(r"转(?:换|化|为|成)?(攻击力|防御力|生命值|暴击率|连击率)", text)
    if m:
        return m.group(1)
    # 兜底：返回文本中 src 之外的第一个属性
    for a in ("攻击力", "防御力", "生命值", "暴击率", "连击率"):
        if a in text and a != src:
            return a
    return None

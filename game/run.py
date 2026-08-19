"""整局游戏会话管理（7 关卡推进）。

状态机：
- 每一关：玩家先二选一（单波/多波）-> 进入关卡自动回10%生命 -> 生成怪物队列
  -> 依次与每只怪物战斗（杀完一只打下一只）-> 全部击败后三选一强化/治疗。

属性分类：
- 普通属性（攻击/生命/防御）用于怪物预算
- 稀有属性（暴击/连击）随关卡深度线性提升
"""

import random
from typing import List, Optional, Callable

from .character import Character
from .level import (
    generate_level_wave, monster_budget, multiwave_count,
    roll_reinforcement, reinforcement_value, heal_exchange_amount,
    MULTIWAVE_REINFORCE_MULT,
)
from .battle import Battle
from .enemy import Monster


class GameSession:
    """一次完整的 7 关卡肉鸽运行。

    monster_provider: 生成某关怪物队列的可调用对象。
        签名: provider(character, level, multiwave, rng) -> List[Monster]
        默认使用离线经济系统 generate_level_wave；AI 模式传入 AI 生成函数。
    """

    def __init__(self, character: Character, mode: str = "offline",
                 rng: random.Random = None,
                 monster_provider: Optional[Callable] = None):
        from .character import LEVEL_CONFIG
        self.character = character
        self.mode = mode  # offline / ai
        self.rng = rng or random
        self.monster_provider = monster_provider or generate_level_wave
        self.level_count = LEVEL_CONFIG["level_count"]
        self.current_level = 1          # 当前关卡 1..7
        self.state = "choose_wave"      # choose_wave / fighting / choose_reinforce / game_over / victory
        self.multiwave = False          # 本关是否多波
        self.monsters: List[Monster] = []  # 本关怪物队列
        self.current_battle: Optional[Battle] = None
        self.battle_index = 0           # 当前打第几只
        self.wave_count = 1
        self.budget = 0.0
        self.reinforce_options: List[str] = []
        self.message = ""
        self.stats = {  # 记录统计
            "kills": 0,
            "waves_fought": 0,
            "heal_exchanged": 0,
        }
        # 战斗历史：记录每场已结束战斗的摘要，供最后一关 AI 针对性布置
        self.battle_history: List[dict] = []
        # 战斗日志：按关记录每一场的完整战斗日志（供通关结算画面展示）
        self.battle_logs: List[dict] = []   # [{level, multiwave, battle_index, log: List[str], result}]
        # 通关结算画面数据（第 7 关通关后展示）
        self.summary_player_stats: dict = {}
        self.summary_player_passives: List[str] = []
        self.summary_battle_logs: List[str] = []   # 汇总全部战斗日志（带分隔）
        self.victory_hint = ""   # 整关通关后的提示（区分单波/多波）
        # 进入第 1 关也回 10% 生命
        self.character.enter_level_heal()

    # ---------- 关卡推进 ----------
    @property
    def is_final_level(self) -> bool:
        return self.current_level >= self.level_count

    def select_wave_mode(self, multiwave: bool):
        """玩家在关卡开始时选择单波/多波，并生成本关怪物队列。"""
        if self.state != "choose_wave":
            return
        self.multiwave = multiwave
        # 进入关卡自动回复当前生命上限的 10%
        heal = self.character.enter_level_heal()
        self.message = f"进入第 {self.current_level} 关，回复 {heal:.1f} 生命"

        self.budget, self.wave_count = monster_budget(
            self.character, self.current_level, multiwave)
        # 通过 provider 生成怪物（默认离线经济，AI 模式由 AI 生成）。
        # AI 模式下传入战斗历史，供第 7 关针对性布置。
        if self.mode == "ai":
            self.monsters = self.monster_provider(
                self.character, self.current_level, multiwave, self.rng,
                battle_history=list(self.battle_history))
        else:
            self.monsters = self.monster_provider(
                self.character, self.current_level, multiwave, self.rng)
        self.battle_index = 0
        if not self.monsters:
            # 生成怪物异常（如 AI 返回空）：不允许跳过战斗进入奖励
            self.message = f"第 {self.current_level} 关未能生成敌人，请重新选择波次"
            return
        self.state = "fighting"
        self._start_next_fight()

    def _start_next_fight(self):
        """开始与下一只怪物战斗（依次进行）。"""
        if self.battle_index < len(self.monsters):
            m = self.monsters[self.battle_index]
            # 最后一关为 boss 战：玩家获得正面 buff 减半、boss 受负面效果减半。
            # 回合限制应用到所有战斗（防止护盾/回血等导致死循环）。
            boss = self.is_final_level
            self.current_battle = Battle(
                self.character, [m], rng=self.rng,
                boss_mode=boss, auto_turn_limit=True)
            self.state = "fighting"
        else:
            # 全部击败：
            # - 最后一关（第 7 关）直接通关胜利，不再进入战后强化/奖励。
            # - 其余关卡进入战后强化。
            if self.is_final_level:
                # 最终关通关：直接胜利，不再进入奖励
                if self.multiwave:
                    self.victory_hint = f"多波通关！第 7 关全部 {self.wave_count} 波敌人全灭，你通关了整局游戏！"
                else:
                    self.victory_hint = "单波通关！你击败了最终的精英敌人，通关了整局游戏！"
                self._prepare_summary()
                self.current_battle = None
                self.state = "victory"
            else:
                self._enter_reinforcement()

    def player_attack_current(self, idx: int):
        """在战斗中攻击当前怪物的指定技能槽（索引）。"""
        if self.state != "fighting" or not self.current_battle:
            return
        alive = self.current_battle.monsters_alive
        if not alive:
            return
        target = alive[min(idx, len(alive) - 1)]
        self.current_battle.player_turn(target.name)
        self.stats["waves_fought"] += 1

        if self.current_battle.battle_over:
            self._record_battle()
            if self.current_battle.player_won:
                self.stats["kills"] += 1
                # 同步生命并打下一只
                self.current_battle.sync_hp_to_character(self.character)
                self.battle_index += 1
                self._start_next_fight()
            else:
                # 玩家死亡
                self.current_battle.sync_hp_to_character(self.character)
                self.state = "game_over"

    def _record_battle(self):
        """记录一场已结束战斗的摘要与完整日志（供第 7 关 AI 参考与通关结算展示）。"""
        b = self.current_battle
        if b is None:
            return
        # 本场战斗的怪物信息
        enemy_desc = []
        for m in b.monsters:
            p_list = "；".join(str(p) for p in m.passives) if m.passives else "无被动"
            enemy_desc.append(
                f"{m.name}(攻{m.atk:.0f}/防{m.defense:.0f}/血{m.max_hp:.0f}/暴{m.crit_rate:.0f}/连{m.combo_rate:.1f}/被动[{p_list}])"
            )
        # 玩家是否存活、剩余生命、击杀情况
        won = b.player_won
        result = "胜利" if won else "失败"
        self.battle_history.append({
            "level": self.current_level,
            "multiwave": self.multiwave,
            "battle_index": self.battle_index + 1,
            "won": won,
            "player_hp_remain": round(max(0.0, b.player.hp), 1),
            "player_max_hp": round(b.player.max_hp, 1),
            "player_atk": round(b.player.eff_atk, 1),
            "player_turn": b.player.turn,
            "enemies": "；".join(enemy_desc),
            "result": result,
        })
        # 记录完整战斗日志（供通关结算画面展示）
        self.battle_logs.append({
            "level": self.current_level,
            "multiwave": self.multiwave,
            "battle_index": self.battle_index + 1,
            "result": result,
            "log": list(getattr(b, "log", [])),
        })

    def _prepare_summary(self):
        """通关后构建结算数据：玩家属性/被动 + 汇总全部战斗日志。"""
        c = self.character
        self.summary_player_stats = dict(c.stats)
        self.summary_player_passives = [str(p) for p in c.passives]
        # 汇总全部战斗日志，按关分块
        lines = []
        for log_entry in self.battle_logs:
            hdr = (f"第 {log_entry['level']} 关 · "
                   f"{'多波' if log_entry['multiwave'] else '单波'} · "
                   f"战斗 {log_entry['battle_index']} · {log_entry['result']}")
            lines.append(hdr)
            lines.append("=" * 30)
            for l in log_entry["log"]:
                lines.append(l)
            lines.append("")
        self.summary_battle_logs = lines

    # ---------- 战后强化 ----------
    def _enter_reinforcement(self):
        """进入三选一强化（或治疗换算）。"""
        self.current_battle = None
        self.reinforce_options = roll_reinforcement(
            self.current_level, self.multiwave, self.rng)
        # 整关通关提示：区分单波 / 多波
        if self.multiwave:
            self.victory_hint = f"多波胜利！第 {self.current_level} 关全部 {self.wave_count} 波敌人已全灭，奖励升级（强化数值 x{MULTIWAVE_REINFORCE_MULT:.1f}）"
        else:
            self.victory_hint = f"单波胜利！你干净利落地击败了第 {self.current_level} 关的精英敌人。"
        self.state = "choose_reinforce"

    def reinforcement_desc(self, attr: str) -> str:
        """返回某强化选项的描述（含数值，多波加成）。"""
        val = reinforcement_value(attr, self.multiwave, self.character)
        mult = f" (多波 x{MULTIWAVE_REINFORCE_MULT:.1f})" if self.multiwave else ""
        return f"{attr} +{val:.2f}{mult}"

    def reinforcement_value_of(self, attr: str) -> float:
        return reinforcement_value(attr, self.multiwave, self.character)

    def choose_reinforcement(self, attr: str):
        """选择某项强化并进入下一关。"""
        val = self.reinforcement_value_of(attr)
        self.character.add_stat(attr, val)
        self.message = f"强化 {attr} +{val:.2f}"
        self._advance_level()

    def choose_heal_exchange(self, x_percent: float = 100.0):
        """放弃强化，按比例回复已损失生命值。

        回血后直接进入下一关（choose_wave，开始新关卡），不限制次数。
        注意：放弃治疗只能在打完本关所有怪物后的奖励环节触发，
        不会跳过本关战斗；它只是用治疗替代强化收益。
        """
        lost = self.character.lost_hp
        heal = heal_exchange_amount(self.character, lost, x_percent)
        actual = self.character.heal(heal)
        self.stats["heal_exchanged"] += actual
        self.message = f"放弃强化，回复 {actual:.1f} 生命"
        # 回血后直接进入下一关（若已是最后一关则通关）
        self._advance_level()

    def _advance_level(self):
        """进入下一关或结束。"""
        if self.is_final_level:
            self.state = "victory"
        else:
            self.current_level += 1
            self.state = "choose_wave"

    # ---------- 查询 ----------
    @property
    def enemies_remaining(self) -> int:
        return max(0, len(self.monsters) - self.battle_index)

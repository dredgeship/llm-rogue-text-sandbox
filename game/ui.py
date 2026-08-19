"""UI 模块：主菜单、角色设计、模式选择、关卡推进、战斗、战后强化。

使用 Pygame 实现。交互：
- 主菜单 / 模式 / 选关波次 / 强化：鼠标点击按钮
- 角色设计：键盘输入框（点击文本框后输入）
- 战斗：键盘 [1] 攻击当前怪物
"""

import os
import threading
import time
from typing import List, Optional, Tuple

# 启用 SDL 的系统输入法候选框（IME UI），必须在 import pygame 之前设置。
os.environ.setdefault("SDL_IME_SHOW_UI", "1")

import pygame

from .character import Character, CHARACTER_FRAMEWORK, DEFAULT_STATS, Passive, PASSIVE_TRIGGERS
from .run import GameSession
from .ai_design import ai_monster_provider, load_api_config, save_api_config
from .level import heal_exchange_amount
from . import passive_engine

# 颜色
WHITE = (245, 245, 245)
BLACK = (20, 20, 30)
GRAY = (120, 120, 130)
DARK = (40, 42, 54)
ACCENT = (90, 160, 255)
RED = (220, 90, 90)
GREEN = (90, 200, 130)
YELLOW = (240, 200, 90)

SCREEN_W = 1000
SCREEN_H = 700

# 被动系统完整说明（用于"被动说明"弹窗，供设计页与被动池页共用）
PASSIVE_HELP_LINES = [
    "【格式】每行一条： [触发时机|条件] 名字：效果",
    "  例: [受击|生命低于50%] 铁壁：反弹 30% 伤害",
    "  名字可选（冒号前缀），供怪物抽取/辨识时按名字引用，可留空。",
    "  时机写在方括号内；条件用 | 与时机分隔，或直接写进效果文本。",
    "",
    "【触发时机】",
    "  击杀/回合/攻击/受击/暴击/连击/数值/死亡/战斗开始/战斗结束",
    "  复活是效果类型而非触发时机，复活被动只能配在 [死亡] 时机下。",
    "  数值 = 直接加数值（默认永久）；其余时机按事件触发。",
    "",
    "【可用条件】",
    "  概率: '30%概率' 按 30% 概率触发",
    "  生命: '生命低于50%' 半血以下才触发（也可高于）",
    "  回合: '第3回合' 特定回合 / '每2回合' 每两回合",
    "  首回合: '第一回合' / '先手'",
    "",
    "【效果类型与示例】",
    "  永久属性: [数值] 攻击力+10（默认永久）",
    "  临时Buff: [回合] 攻击力+5（本场战斗，未写永久默认临时）",
    "  回血: [回合] 回复 20 点生命",
    "  吸血: [攻击] 攻击时回复伤害的 15%",
    "  护盾: [回合] 获得 5 点临时护盾",
    "  反弹: [受击] 反弹 3 点伤害",
    "  闪避/免伤: [受击] 闪避 / [受击] 免疫伤害 / 受到的伤害变为0",
    "  额外伤害: [攻击] 额外造成 2 点伤害",
    "  斩杀: [攻击] 对生命低于 30% 的目标斩杀",
    "  无敌: [受击] 获得 1 次无敌",
    "  属性转移: [攻击] 把 30% 防御力转为攻击力",
    "  偷取: [战斗开始] 偷取敌方 10% 攻击力",
    "  复活: [死亡] 死亡时 30% 概率复活",
    "  狂暴: [受击] 生命低于 50% 时攻击力提升 / 生命高于 70% 时攻击力提升",
    "",
    "【提示】未写明永久/临时的属性叠加，默认本场战斗临时。",
    "  无法解析的被动会在战斗中提示'未生效'，不静默忽略。",
]


def update_ime_rect(rect):
    """把输入法（IME）候选框定位到指定矩形附近。

    SDL 2.28+ 支持 set_text_input_rect，让系统把中文输入法候选框
    显示在输入框的位置，避免候选框显示在错误角落或完全看不到。
    """
    try:
        pygame.key.start_text_input()
        pygame.key.set_text_input_rect(pygame.Rect(rect))
    except Exception:
        pass


class Button:
    def __init__(self, rect, text, font):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.font = font

    def draw(self, surface):
        hover = self.rect.collidepoint(pygame.mouse.get_pos())
        color = ACCENT if hover else DARK
        pygame.draw.rect(surface, color, self.rect, border_radius=8)
        pygame.draw.rect(surface, WHITE, self.rect, width=2, border_radius=8)
        label = self.font.render(self.text, True, WHITE)
        surface.blit(label, label.get_rect(center=self.rect.center))

    def clicked(self, pos) -> bool:
        return self.rect.collidepoint(pos)


class InputBox:
    """文本框：点击激活后输入。

    - 未激活时显示当前值（灰色描边），点击激活后（蓝色描边）。
    - 激活后自动清空原值，方便直接输入新值；
      输入普通字符则替换原值，回车/点击外部则确认。
    - 也支持删除键逐个删除。
    """

    def __init__(self, rect, font, default=""):
        self.rect = pygame.Rect(rect)
        self.font = font
        self.text = default
        self._initial = default   # 记录初始值，用于显示
        self.active = False
        self._just_activated = False
        self._textinput_seen = False  # 本帧是否已收到 TEXTINPUT 事件

    # ---------- 剪贴板（Ctrl+C/V/X/A） ----------
    @staticmethod
    def _init_scrap():
        """确保剪贴板子系统已初始化。"""
        if not pygame.scrap.get_init():
            try:
                pygame.scrap.init()
            except pygame.error:
                return False
        return True

    def _copy_all(self):
        """复制全部文本到系统剪贴板。"""
        if self.text and self._init_scrap():
            try:
                pygame.scrap.put(pygame.SCRAP_TEXT, self.text.encode("utf-8"))
            except pygame.error:
                pass

    def _paste(self):
        """从系统剪贴板粘贴文本到输入框。"""
        if not self._init_scrap():
            return
        try:
            data = pygame.scrap.get(pygame.SCRAP_TEXT)
        except pygame.error:
            return
        if not data:
            return
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            text = str(data)
        text = "".join(ch for ch in text if ch.isprintable())
        if text:
            self._insert_text(text)

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
            self._just_activated = self.active
            self._textinput_seen = False
            if self.active:
                update_ime_rect(self.rect)  # 激活时把 IME 候选框定位到输入框
        elif event.type == pygame.TEXTINPUT and self.active:
            # 文本输入（含中文 IME 确认后的字符）
            self._textinput_seen = True
            text = getattr(event, "text", "")
            if text:
                self._insert_text(text)
        elif event.type == pygame.KEYDOWN and self.active:
            # 复制/粘贴/剪切/全选快捷键
            if event.mod & pygame.KMOD_CTRL:
                if event.key == pygame.K_c:
                    self._copy_all()
                elif event.key == pygame.K_v:
                    self._paste()
                elif event.key == pygame.K_x:
                    self._copy_all()
                    self.text = ""
                elif event.key == pygame.K_a:
                    pass  # 无选区系统，全选无实际效果（保留文本）
                return  # 消费 Ctrl 组合键，不落入普通输入
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key == pygame.K_RETURN:
                self.active = False
            elif event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN,
                               pygame.K_TAB, pygame.K_ESCAPE):
                pass  # 忽略导航/控制键
            elif event.unicode and event.unicode.isprintable() and not self._just_activated \
                    and not self._textinput_seen:
                # 仅在没有任何 TEXTINPUT 事件的环境（如部分 Linux）作为兜底，
                # 否则会与 TEXTINPUT 重复插入同一个字符（打一个字符出现两三个）。
                self.text += event.unicode

    def _insert_text(self, text: str):
        """插入文本；激活后首次输入替换原值。"""
        if self._just_activated:
            self.text = text
            self._just_activated = False
        else:
            self.text += text

    def draw(self, surface):
        color = ACCENT if self.active else GRAY
        pygame.draw.rect(surface, DARK, self.rect, border_radius=6)
        pygame.draw.rect(surface, color, self.rect, width=2, border_radius=6)
        show = self.text if self.text else "..." if self.active else str(self._initial or "...")
        txt = self.font.render(show, True, WHITE)
        surface.blit(txt, (self.rect.x + 6, self.rect.y + 4))
        if self.active:
            update_ime_rect(self.rect)  # 激活期间持续把 IME 候选框锚定到输入框


class TextArea:
    """多行文本框：支持光标移动、点击定位、光标处插入/删除。

    - 每行一条被动词条，按 [回车] 换行。
    - 方向键上下移动光标行，左右移动光标列；[Home]/[End] 到行首/行尾。
    - 点击定位光标到最近字符。
    - 激活时在光标位置绘制闪烁光标，IME 候选框锚定到光标行。
    """

    # 可显示的最大行数（超出部分向上滚动）
    MAX_VISIBLE = 8
    LINE_H = 22

    def __init__(self, rect, font):
        self.rect = pygame.Rect(rect)
        self.font = font
        self.lines = [""]
        self.active = False
        self._textinput_seen = False  # 本帧是否已收到 TEXTINPUT 事件
        self._line_index = 0           # 当前光标所在行
        self._cursor = 0               # 当前光标列（该行内的字符索引）
        self._scroll = 0               # 顶部偏移行数（用于多行滚动）
        self._blink = 0                # 光标闪烁计时

    # ---------- 光标操作 ----------
    def _clamp_line(self):
        """确保当前行在有效范围内。"""
        self._line_index = max(0, min(self._line_index, len(self.lines) - 1))

    def _clamp_cursor(self):
        self._cursor = max(0, min(self._cursor, len(self.lines[self._line_index])))

    def _move_up(self):
        if self._line_index > 0:
            self._line_index -= 1
            self._cursor = min(self._cursor, len(self.lines[self._line_index]))

    def _move_down(self):
        if self._line_index < len(self.lines) - 1:
            self._line_index += 1
            self._cursor = min(self._cursor, len(self.lines[self._line_index]))

    def _move_to_line(self, index: int):
        self._line_index = max(0, min(index, len(self.lines) - 1))
        self._cursor = min(self._cursor, len(self.lines[self._line_index]))

    def _cursor_screen_y(self) -> int:
        """光标行在屏幕上的 Y 坐标（相对当前滚动偏移）。"""
        return self.rect.y + 6 + (self._line_index - self._scroll) * self.LINE_H

    def _ensure_cursor_visible(self):
        """滚动窗口，确保光标行可见。"""
        if self._line_index < self._scroll:
            self._scroll = self._line_index
        elif self._line_index >= self._scroll + self.MAX_VISIBLE:
            self._scroll = self._line_index - self.MAX_VISIBLE + 1

    def _scroll_wheel(self, dy: int):
        """按滚轮增量滚动可视区（dy 为正表示向上滚动，即显示更靠前的行）。"""
        max_scroll = max(0, len(self.lines) - self.MAX_VISIBLE)
        self._scroll = max(0, min(self._scroll - dy, max_scroll))

    def _click_to_cursor(self, pos):
        """把点击位置映射为最近的光标行列。"""
        x, y = pos
        line = self._scroll + (y - (self.rect.y + 6)) // self.LINE_H
        line = max(0, min(line, len(self.lines) - 1))
        # 找出该行中离点击 x 最近的字符索引
        text = self.lines[line]
        cursor = len(text)
        best_dx = 10**9
        for i in range(len(text) + 1):
            w = self.font.size(text[:i])[0]
            dx = abs((self.rect.x + 6 + w) - x)
            if dx < best_dx:
                best_dx = dx
                cursor = i
        self._line_index = line
        self._cursor = cursor
        self._ensure_cursor_visible()

    # ---------- 事件 ----------
    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
            if self.active:
                self._click_to_cursor(event.pos)
                self._blink = 0
                update_ime_rect(self._cursor_rect())  # 候选框定位到光标
        elif event.type == pygame.MOUSEWHEEL:
            # 滚轮滚动编辑区（鼠标悬停其上才生效）
            if self.rect.collidepoint(pygame.mouse.get_pos()):
                self._scroll_wheel(event.y)
        elif event.type == pygame.TEXTINPUT and self.active:
            self._textinput_seen = True
            text = getattr(event, "text", "")
            if text:
                self._insert_text(text)
        elif event.type == pygame.KEYDOWN and self.active:
            self._blink = 0
            if event.key == pygame.K_BACKSPACE:
                self._backspace()
            elif event.key == pygame.K_DELETE:
                self._delete()
            elif event.key == pygame.K_RETURN:
                self._insert_newline()
            elif event.key == pygame.K_LEFT:
                if self._cursor > 0:
                    self._cursor -= 1
                elif self._line_index > 0:
                    self._line_index -= 1
                    self._cursor = len(self.lines[self._line_index])
            elif event.key == pygame.K_RIGHT:
                line = self.lines[self._line_index]
                if self._cursor < len(line):
                    self._cursor += 1
                elif self._line_index < len(self.lines) - 1:
                    self._line_index += 1
                    self._cursor = 0
            elif event.key == pygame.K_UP:
                self._move_up()
            elif event.key == pygame.K_DOWN:
                self._move_down()
            elif event.key == pygame.K_HOME:
                self._cursor = 0
            elif event.key == pygame.K_END:
                self._cursor = len(self.lines[self._line_index])
            elif event.key == pygame.K_TAB or event.key == pygame.K_ESCAPE:
                pass  # 忽略
            elif event.unicode and event.unicode.isprintable() and not self._textinput_seen:
                # 仅在没有任何 TEXTINPUT 事件的环境作为兜底，
                # 否则会与 TEXTINPUT 重复插入同一个字符（打一个字符出现两三个）。
                self._insert_text(event.unicode)
            self._ensure_cursor_visible()

    # ---------- 编辑操作 ----------
    def _insert_text(self, text: str):
        line = self.lines[self._line_index]
        self.lines[self._line_index] = line[:self._cursor] + text + line[self._cursor:]
        self._cursor += len(text)

    def _insert_newline(self):
        line = self.lines[self._line_index]
        head, tail = line[:self._cursor], line[self._cursor:]
        self.lines[self._line_index] = head
        self.lines.insert(self._line_index + 1, tail)
        self._line_index += 1
        self._cursor = 0
        self._ensure_cursor_visible()

    def _backspace(self):
        if self._cursor > 0:
            line = self.lines[self._line_index]
            self.lines[self._line_index] = line[:self._cursor - 1] + line[self._cursor:]
            self._cursor -= 1
        elif self._line_index > 0:
            # 删除换行：把当前行拼到上一行末尾
            prev = self.lines[self._line_index - 1]
            cur = self.lines[self._line_index]
            self._cursor = len(prev)
            self.lines[self._line_index - 1] = prev + cur
            del self.lines[self._line_index]
            self._line_index -= 1

    def _delete(self):
        line = self.lines[self._line_index]
        if self._cursor < len(line):
            self.lines[self._line_index] = line[:self._cursor] + line[self._cursor + 1:]
        elif self._line_index < len(self.lines) - 1:
            # 删除换行：把下一行拼到当前行末尾
            nxt = self.lines[self._line_index + 1]
            self.lines[self._line_index] = line + nxt
            del self.lines[self._line_index + 1]

    # ---------- 绘制 ----------
    def _cursor_rect(self):
        """返回光标位置对应的矩形（用于 IME 候选框定位）。

        高度取整行高，宽度覆盖到输入框右缘，避免候选框太小或超出输入框。
        """
        line = self.lines[self._line_index]
        w = self.font.size(line[:self._cursor])[0]
        x = self.rect.x + 6 + w
        y = self._cursor_screen_y()
        width = max(2, self.rect.right - 6 - x)
        return (x, y, width, self.LINE_H)

    def draw(self, surface):
        color = ACCENT if self.active else GRAY
        pygame.draw.rect(surface, DARK, self.rect, border_radius=6)
        pygame.draw.rect(surface, color, self.rect, width=2, border_radius=6)
        y = self.rect.y + 6
        for i in range(self._scroll, min(self._scroll + self.MAX_VISIBLE, len(self.lines))):
            txt = self.font.render(self.lines[i] or "", True, WHITE)
            surface.blit(txt, (self.rect.x + 6, y))
            y += self.LINE_H
        # 绘制光标
        if self.active:
            self._blink += 1
            # 每 30 帧切换一次闪烁
            if (self._blink // 20) % 2 == 0:
                rx, ry, rw, rh = self._cursor_rect()
                pygame.draw.rect(surface, WHITE, (rx, ry, max(1, rw), rh))
            update_ime_rect(self._cursor_rect())  # 候选框锚定到光标位置

    def get_text(self) -> str:
        return "\n".join(l for l in self.lines if l.strip())

    def set_text(self, text: str):
        self.lines = text.split("\n") if text else [""]
        self._line_index = 0
        self._cursor = 0
        self._scroll = 0


# ---------------------------------------------------------------------------
# 积木式被动编辑器（Scratch 风格列表式积木）
# ---------------------------------------------------------------------------
# 效果类型选项（与 passive_engine._EFFECT_RULES 的标签一致）
BLOCK_EFFECT_OPTIONS = [
    "属性叠加", "闪避", "吸血", "回血", "护盾", "反弹", "斩杀",
    "无敌", "属性转移", "偷取", "额外伤害", "临时Buff",
    "复活", "狂暴",
]
# 效果类型 -> 是否需要"属性"下拉（True 时显示属性下拉）
BLOCK_EFFECT_NEEDS_ATTR = {
    "属性叠加": True, "属性转移": True, "偷取": True, "临时Buff": True,
}
# 效果类型 -> 是否需要"数值"输入（绝大多数都需要）
BLOCK_EFFECT_NEEDS_VALUE = {}
# 效果类型 -> 数值输入框的提示占位文本
# 支持"固定值+比例"的效果提示如 "数值 或 比例%(可叠加)"。
BLOCK_EFFECT_VALUE_HINT = {
    "属性叠加": "数值", "闪避": "概率%", "吸血": "数值或比例%", "回血": "数值或比例%",
    "护盾": "数值或比例%", "反弹": "数值或比例%", "斩杀": "血量%", "无敌": "次数",
    "属性转移": "数值或比例%", "偷取": "数值或比例%", "额外伤害": "数值或比例%",
    "临时Buff": "数值", "复活": "概率%", "狂暴": "数值或比例%",
}
# 效果类型 -> 临时Buff 时属性只允许攻击/防御
BLOCK_TEMPBUFF_ATTRS = ["攻击力", "防御力"]
# 条件选项：(key, 显示文案, 条件文本片段)。片段含 {pct} 时由块内 cond_pct 填充。
BLOCK_COND_OPTIONS = [
    ("none", "无条件", ""),
    ("first", "首回合", "首回合"),
    ("prob_30", "30%概率", "30%概率"),
    ("prob_50", "50%概率", "50%概率"),
    ("hp_below", "生命低于X%", "生命低于{pct}%"),
    ("hp_above", "生命高于X%", "生命高于{pct}%"),
    ("turn_once", "第X回合", "第{pct}回合"),
    ("turn_every", "每X回合", "每{pct}回合"),
]
# 属性下拉选项（属性相关效果可选）
BLOCK_ATTR_OPTIONS = ["攻击力", "防御力", "生命值", "暴击率", "连击率"]
# 需要"条件数值"输入框的条件 key（选择后该行出现数值输入框）
# （hp_below/hp_above 填百分比，turn_once/turn_every 填回合数）
BLOCK_COND_NEEDS_PCT = {"hp_below", "hp_above", "turn_once", "turn_every"}
# 效果 kind -> 积木效果类型（反向映射，用于把已解析被动拆回积木）
_KIND_TO_BLOCK_EFFECT = {
    passive_engine.EFF_ATK_UP: "属性叠加",
    passive_engine.EFF_DEF_UP: "属性叠加",
    passive_engine.EFF_HP_UP: "属性叠加",
    passive_engine.EFF_CRIT_UP: "属性叠加",
    passive_engine.EFF_COMBO_UP: "属性叠加",
    passive_engine.EFF_DODGE: "闪避",
    passive_engine.EFF_LIFESTEAL: "吸血",
    passive_engine.EFF_HEAL: "回血",
    passive_engine.EFF_SHIELD: "护盾",
    passive_engine.EFF_REFLECT: "反弹",
    passive_engine.EFF_EXECUTE: "斩杀",
    passive_engine.EFF_INVULN: "无敌",
    passive_engine.EFF_TRANSFER: "属性转移",
    passive_engine.EFF_STEAL: "偷取",
    passive_engine.EFF_EXTRA_DMG: "额外伤害",
    passive_engine.EFF_TEMP_ATK: "临时Buff",
    passive_engine.EFF_TEMP_DEF: "临时Buff",
    passive_engine.EFF_REVIVE: "复活",
    passive_engine.EFF_BERSERK: "狂暴",
}


class Dropdown:
    """轻量下拉框：点击展开选项浮层，点击选项选中。"""

    ITEM_H = 22

    def __init__(self, rect, options, font):
        self.rect = pygame.Rect(rect)
        self.font = font
        self.options = list(options)
        self.index = 0
        self.open = False

    @property
    def value(self):
        return self.options[self.index]

    def set_value(self, v):
        self.index = self.options.index(v) if v in self.options else 0

    def popup_rect(self):
        h = len(self.options) * self.ITEM_H + 6
        return pygame.Rect(self.rect.x, self.rect.bottom, self.rect.w, h)

    def clicked(self, pos):
        return self.rect.collidepoint(pos)

    def option_at(self, pos):
        pr = self.popup_rect()
        if not pr.collidepoint(pos):
            return None
        idx = (pos[1] - (pr.y + 3)) // self.ITEM_H
        if 0 <= idx < len(self.options):
            return idx
        return None

    def draw(self, surface, expanded=False, focus_color=ACCENT):
        if expanded:
            # 展开浮层
            pr = self.popup_rect()
            pygame.draw.rect(surface, DARK, pr, border_radius=6)
            pygame.draw.rect(surface, ACCENT, pr, width=2, border_radius=6)
            for i, opt in enumerate(self.options):
                y = pr.y + 3 + i * self.ITEM_H
                if i == self.index:
                    pygame.draw.rect(surface, (60, 90, 150), (pr.x + 1, y, pr.w - 2, self.ITEM_H))
                t = self.font.render(opt, True, WHITE if i != self.index else YELLOW)
                surface.blit(t, (pr.x + 6, y + 2))
        pygame.draw.rect(surface, focus_color if self.open else GRAY, self.rect, border_radius=5)
        pygame.draw.rect(surface, WHITE, self.rect, width=1, border_radius=5)
        label = self.font.render(self.value, True, WHITE)
        surface.blit(label, (self.rect.x + 6, self.rect.y + 3))


class PassiveBlockEditor:
    """列表式积木被动编辑器。

    每行一个被动积木块，用下拉框选择触发时机 / 条件 / 效果类型，填写参数，
    支持新增、删除、上下移动排序。与被动池页、角色设计页共用同一组件。

    每条积木数据（Block）：
      {"trigger": 触发时机, "condition": 条件key, "effect": 效果类型,
       "attr": 属性名, "value": 数值字符串, "permanent": 是否永久}
    支持文本 <-> 积木双向转换，产出文本兼容 passive_engine 解析。
    """

    ROW_H = 40
    FOOTER_H = 24   # 底部条数/滚轮提示高度

    def __init__(self, rect, font, small_font):
        self.rect = pygame.Rect(rect)
        self.font = font
        self.small_font = small_font
        self.blocks: List[dict] = []
        self.scroll = 0
        self._input_rows = {}   # 行号 -> InputBox（数值输入）
        self._name_rows = {}    # 行号 -> InputBox（名字输入）
        self._cond_pct_rows = {}  # 行号 -> InputBox（条件百分比输入）
        self._active_drop = None  # (row, kind) 当前展开的下拉
        self._active_input = None # 当前激活数值输入的行号
        self._active_name = None  # 当前激活名字输入的行号
        self._active_cond_pct = None  # 当前激活条件百分比输入的行号

    # ---------- 布局辅助 ----------
    @property
    def rows_visible(self) -> int:
        return max(1, (self.rect.h - self.FOOTER_H - 6) // self.ROW_H)

    def _row_rect(self, row):
        y = self.rect.y + 3 + (row - self.scroll) * self.ROW_H
        return pygame.Rect(self.rect.x + 3, y, self.rect.w - 6, self.ROW_H - 4)

    def _name_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.x, r.y + 4, 100, 30)
    def _trigger_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.x + 98, r.y + 4, 82, 30)
    def _cond_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.x + 186, r.y + 4, 100, 30)
    def _cond_pct_rect(self, row):
        # 条件百分比输入框（条件为"生命低于/高于X%"时显示）
        r = self._row_rect(row)
        return pygame.Rect(r.x + 292, r.y + 4, 44, 30)
    def _effect_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.x + 342, r.y + 4, 96, 30)
    def _attr_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.x + 444, r.y + 4, 80, 30)
    def _dst_attr_rect(self, row):
        # 属性转移的目标属性下拉（只在属性转移效果时使用）
        r = self._row_rect(row)
        return pygame.Rect(r.x + 530, r.y + 4, 78, 30)
    def _value_rect(self, row):
        r = self._row_rect(row)
        if 0 <= row < len(self.blocks) and self.blocks[row].get("effect") == "属性转移":
            # 属性转移时数值框移到目标属性之后
            return pygame.Rect(r.x + 614, r.y + 4, 82, 30)
        return pygame.Rect(r.x + 530, r.y + 4, 88, 30)
    def _up_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.right - 102, r.y + 4, 30, 30)
    def _down_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.right - 68, r.y + 4, 30, 30)
    def _del_rect(self, row):
        r = self._row_rect(row)
        return pygame.Rect(r.right - 34, r.y + 4, 30, 30)

    def _add_btn_rect(self):
        return pygame.Rect(self.rect.x + 6, self.rect.bottom - self.FOOTER_H + 2, 130, 20)

    # ---------- 数据操作 ----------
    def set_blocks(self, blocks: List[dict]):
        self.blocks = list(blocks)
        self._sync_inputs()
        self.scroll = 0

    def get_blocks(self) -> List[dict]:
        return list(self.blocks)

    def add_block(self):
        self.blocks.append({
            "name": "", "trigger": "攻击", "condition": "none", "effect": "属性叠加",
            "attr": "攻击力", "dst_attr": "防御力", "cond_pct": "50",
            "value": "", "permanent": False,
        })
        self._sync_inputs()
        # 滚动到最后一行
        self.scroll = max(0, len(self.blocks) - self.rows_visible)

    def _sync_inputs(self):
        """按当前积木行数重建数值/名字/条件百分比输入框。"""
        self._input_rows = {}
        self._name_rows = {}
        self._cond_pct_rows = {}
        for i in range(len(self.blocks)):
            self._input_rows[i] = InputBox(
                self._value_rect(i), self.small_font, self.blocks[i]["value"])
            self._name_rows[i] = InputBox(
                self._name_rect(i), self.small_font, self.blocks[i].get("name", ""))
            self._cond_pct_rows[i] = InputBox(
                self._cond_pct_rect(i), self.small_font, self.blocks[i].get("cond_pct", "50"))

    # ---------- 文本互转 ----------
    @staticmethod
    def _value_num(value: str, default: float) -> float:
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_value_pair(value: str) -> Tuple[float, float]:
        """解析用户输入的数值，返回 (固定值, 比例百分数)。

        支持三种写法：'2'（固定值）、'20%'（比例）、'2+20%'（两者并存相加）。
        """
        amount, ratio_pct, _ = PassiveBlockEditor._parse_value_pair_full(value)
        return amount, ratio_pct

    @staticmethod
    def _parse_value_pair_full(value: str) -> Tuple[float, float, str]:
        """解析用户输入的数值，返回 (固定值, 比例百分数, 比例基数)。

        基数由百分比后缀决定：'%损'=已损失生命、'%现'=当前生命、'%'=最大生命(默认)。
        例：'2+20%损' -> (2, 20, 'lost')；'20%现' -> (0, 20, 'current')。
        """
        amount = 0.0
        ratio_pct = 0.0
        base = "max"
        for part in (value or "").split("+"):
            part = part.strip()
            if not part:
                continue
            if part.endswith("%损"):
                ratio_pct = PassiveBlockEditor._to_float(part[:-2])
                base = "lost"
            elif part.endswith("%现"):
                ratio_pct = PassiveBlockEditor._to_float(part[:-2])
                base = "current"
            elif part.endswith("%"):
                ratio_pct = PassiveBlockEditor._to_float(part[:-1])
            else:
                try:
                    amount = float(part)
                except (TypeError, ValueError):
                    pass
        return amount, ratio_pct, base

    @staticmethod
    def _to_float(text: str) -> float:
        try:
            return float(text.strip())
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fmt_pair(amount: float, ratio_pct: float, base_suffix: str = "") -> str:
        """把 (固定值, 比例百分数) 拼成"固定值+比例%"字符串（用于积木回填显示）。

        base_suffix：比例基数后缀（"损"=已损失生命、"现"=当前生命、""=最大生命）。
        """
        parts = []
        if amount > 0:
            parts.append(f"{amount:g}")
        if ratio_pct > 0:
            parts.append(f"{ratio_pct:g}%{base_suffix}")
        return "+".join(parts)

    def _build_effect(self, block: dict) -> str:
        e = block["effect"]
        attr = block.get("attr", "攻击力")
        val = self._value_num(block.get("value", ""), 0.0)
        amount, ratio_pct, base = self._parse_value_pair_full(block.get("value", ""))
        ratio = ratio_pct / 100.0

        def num(d):
            return self._value_num(block.get("value", ""), d)

        def base_word():
            """比例基数的中文描述：最大生命/已损失生命/当前生命。"""
            return {"lost": "已损失生命", "current": "当前生命"}.get(base, "最大生命")

        def desc():
            """拼效果参数描述：仅固定 / 仅比例 / 两者。"""
            if amount > 0 and ratio_pct > 0:
                return f"{amount:g} 点，额外 {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"{ratio_pct:g}%"
            return f"{amount if amount > 0 else 'X':g} 点"

        if e == "属性叠加":
            suffix = "（永久）" if block.get("permanent") else ""
            return f"{attr}+{num(1):g}{suffix}"
        if e == "闪避":
            return f"闪避" if val <= 0 else f"闪避 {val:g}%"
        if e == "吸血":
            # 比例=按本次造成伤害；固定值=每次攻击固定回复
            if ratio_pct > 0 and amount > 0:
                return f"吸血：攻击时回复 {amount:g} 点 + 本次伤害的 {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"吸血：攻击时回复本次伤害的 {ratio_pct:g}%"
            if amount > 0:
                return f"吸血：攻击时固定回复 {amount:g} 点"
            return f"吸血：攻击时回复伤害的 {num(15):g}%"
        if e == "回血":
            if ratio_pct > 0 and amount > 0:
                return f"回复 {amount:g} 点 + {base_word()}的 {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"回复{base_word()}的 {ratio_pct:g}%"
            return f"回复 {num(10):g} 点生命"
        if e == "护盾":
            if ratio_pct > 0 and amount > 0:
                return f"获得 {amount:g} 点 + {base_word()} {ratio_pct:g}% 的临时护盾"
            if ratio_pct > 0:
                return f"获得{base_word()} {ratio_pct:g}% 的临时护盾"
            return f"获得 {num(3):g} 点临时护盾"
        if e == "反弹":
            if ratio_pct > 0 and amount > 0:
                return f"反弹 {amount:g} 点 + 本次受到的伤害 {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"反弹本次受到的伤害 {ratio_pct:g}%"
            return f"反弹 {num(3):g} 点伤害"
        if e == "斩杀":
            return f"对生命低于 {num(30):g}% 的目标斩杀"
        if e == "无敌":
            return f"获得 {num(1):g} 次无敌"
        if e == "属性转移":
            dst = block.get("dst_attr") or "防御力"
            if dst == attr:
                dst = {"攻击力": "防御力", "防御力": "攻击力", "生命值": "防御力",
                       "暴击率": "连击率", "连击率": "暴击率"}.get(attr, "防御力")
            if amount > 0 and ratio_pct > 0:
                return f"把 {amount:g} 点 + {ratio_pct:g}% 的 {attr}转为{dst}"
            if ratio_pct > 0:
                return f"把 {ratio_pct:g}% {attr}转为{dst}"
            if amount > 0:
                return f"把 {amount:g} 点 {attr}转为{dst}"
            return f"把 {num(30):g}% {attr}转为{dst}"
        if e == "偷取":
            if amount > 0 and ratio_pct > 0:
                return f"偷取敌方 {amount:g} 点 + {ratio_pct:g}% 的 {attr}"
            if ratio_pct > 0:
                return f"偷取敌方 {ratio_pct:g}% {attr}"
            if amount > 0:
                return f"偷取敌方 {amount:g} 点 {attr}"
            return f"偷取敌方 {num(10):g}% {attr}"
        if e == "额外伤害":
            if ratio_pct > 0 and amount > 0:
                return f"额外造成 {amount:g} 点 + 本次伤害 {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"额外造成本次伤害的 {ratio_pct:g}%"
            return f"额外造成 {num(2):g} 点伤害"
        if e == "临时Buff":
            return f"临时 {attr}+{num(5):g}"
        if e == "复活":
            return f"死亡时 {num(30):g}% 概率复活"
        if e == "狂暴":
            if ratio_pct > 0 and amount > 0:
                return f"狂暴：攻击力提升 {amount:g} 点 + {ratio_pct:g}%"
            if ratio_pct > 0:
                return f"狂暴：攻击力提升 {ratio_pct:g}%"
            return f"狂暴：攻击力提升 {num(5):g}"
        return ""

    def build_text_lines(self) -> List[str]:
        """把所有积木转成被动文本行（含 [时机|条件] 前缀与可选名字前缀）。"""
        cond_text = {k: c for k, _, c in BLOCK_COND_OPTIONS}
        lines = []
        for b in self.blocks:
            trigger = b["trigger"]
            c = cond_text.get(b.get("condition", "none"), "")
            if "{pct}" in c:
                pct = int(float(b.get("cond_pct", "50") or 50))
                c = c.format(pct=min(99, max(1, pct)))
            effect_text = self._build_effect(b)
            if not effect_text.strip():
                continue
            name = (b.get("name") or "").strip()
            body = f"{name}：{effect_text}" if name else effect_text
            if c:
                lines.append(f"[{trigger}|{c}] {body}")
            else:
                lines.append(f"[{trigger}] {body}")
        return lines

    @staticmethod
    def parse_block(text: str) -> Optional[dict]:
        """把一条被动文本拆回积木字段；无法解析返回 None。"""
        pp = passive_engine.parse(text)
        if not pp.ok:
            return None
        cond = pp.condition
        eff = pp.effect
        effect = _KIND_TO_BLOCK_EFFECT.get(eff.kind, "属性叠加")
        attr = eff.attr or "攻击力"
        # 数值：支持"固定值+比例"的效果回填为 "固定值+比例%" 格式；
        # 回血/护盾的比例可带基数后缀（损=已损失生命、现=当前生命、无=最大生命）；
        # 纯比例效果（闪避/斩杀/复活）取比例*100，纯数值取 amount。
        if eff.kind in (passive_engine.EFF_HEAL, passive_engine.EFF_SHIELD):
            base_suffix = {"lost": "损", "current": "现"}.get(eff.ratio_base, "")
            val = PassiveBlockEditor._fmt_pair(eff.amount, eff.ratio * 100, base_suffix)
        elif eff.kind in (passive_engine.EFF_REFLECT, passive_engine.EFF_EXTRA_DMG,
                          passive_engine.EFF_LIFESTEAL, passive_engine.EFF_STEAL,
                          passive_engine.EFF_TRANSFER, passive_engine.EFF_BERSERK):
            val = PassiveBlockEditor._fmt_pair(eff.amount, eff.ratio * 100)
        elif eff.kind in (passive_engine.EFF_DODGE, passive_engine.EFF_EXECUTE,
                          passive_engine.EFF_REVIVE):
            val = (eff.ratio * 100) if eff.ratio else 0.0
        else:
            val = eff.amount
        # 条件反向
        condition = "none"
        cond_pct = "50"
        if cond.first_turn:
            condition = "first"
        elif cond.probability is not None:
            pct = int(round(cond.probability * 100))
            condition = f"prob_{pct}" if pct in (30, 50) else "none"
        elif cond.hp_ratio is not None:
            pct = int(round(cond.hp_ratio * 100))
            condition = "hp_below" if cond.hp_direction != "above" else "hp_above"
            cond_pct = str(min(99, max(1, pct)))
        elif cond.turn_n is not None:
            condition = "turn_every" if cond.turn_kind == "every" else "turn_once"
            cond_pct = str(max(1, int(cond.turn_n)))
        # 属性转移：从原文提取目标属性
        dst_attr = "防御力"
        if effect == "属性转移" and eff.raw:
            import re
            m = re.search(r"转(?:换|化|为|成)?(攻击力|防御力|生命值|暴击率|连击率)", eff.raw)
            if m and m.group(1) != attr:
                dst_attr = m.group(1)
        return {
            "name": pp.name,
            # 复活只能由"死亡"触发：回填时强制时机为"死亡"
            "trigger": "死亡" if effect == "复活" else cond.trigger,
            "condition": condition,
            "cond_pct": cond_pct,
            "effect": effect,
            "attr": attr,
            "dst_attr": dst_attr,
            # val 可能是字符串（固定值+比例格式），也可能是数值；字符串直接使用
            "value": val if isinstance(val, str) else (f"{val:g}" if val else ""),
            "permanent": bool(eff.permanent),
        }

    def set_text(self, text: str):
        """从被动文本列表载入积木（每行一条）。"""
        blocks = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            b = self.parse_block(line)
            if b is not None:
                blocks.append(b)
        if not blocks:
            blocks = [{"name": "", "trigger": "攻击", "condition": "none", "effect": "属性叠加",
                       "attr": "攻击力", "dst_attr": "防御力", "cond_pct": "50",
                       "value": "", "permanent": False}]
        self.set_blocks(blocks)

    def get_text(self) -> str:
        """积木转文本（用于保存/收集，与 TextArea.get_text 对齐）。"""
        return "\n".join(self.build_text_lines())

    # ---------- 事件 ----------
    def _dropdowns(self, row):
        """返回该行的下拉：(kind, Dropdown)。"""
        b = self.blocks[row]
        need_attr = BLOCK_EFFECT_NEEDS_ATTR.get(b["effect"], False)
        attr_options = BLOCK_ATTR_OPTIONS
        if b["effect"] == "临时Buff":
            attr_options = BLOCK_TEMPBUFF_ATTRS
        dds = []
        # 复活效果只能由"死亡"触发：
        # 1) 时机下拉在效果为复活时只提供"死亡"；
        # 2) 效果下拉在时机非"死亡"时不显示"复活"（避免组合出非死亡触发复活）。
        trigger_options = ["死亡"] if b["effect"] == "复活" else PASSIVE_TRIGGERS
        dds.append(("trigger", Dropdown(self._trigger_rect(row), trigger_options, self.small_font)))
        dds[-1][1].set_value(b["trigger"] if b["effect"] != "复活" else "死亡")
        cond_keys = [k for k, _, _ in BLOCK_COND_OPTIONS]
        cond_key = b["condition"] if b["condition"] in cond_keys else "none"
        dds.append(("condition", Dropdown(self._cond_rect(row),
                                          [label for _, label, _ in BLOCK_COND_OPTIONS], self.small_font)))
        dds[-1][1].set_value([label for _, label, _ in BLOCK_COND_OPTIONS][cond_keys.index(cond_key)])
        effect_options = BLOCK_EFFECT_OPTIONS if b["trigger"] == "死亡" else \
            [e for e in BLOCK_EFFECT_OPTIONS if e != "复活"]
        dds.append(("effect", Dropdown(self._effect_rect(row), effect_options, self.small_font)))
        dds[-1][1].set_value(b["effect"] if b["effect"] in effect_options else "属性叠加")
        if need_attr:
            dds.append(("attr", Dropdown(self._attr_rect(row), attr_options, self.small_font)))
            dds[-1][1].set_value(b["attr"])
            if b["effect"] == "属性转移":
                # 属性转移：第二个下拉为目标属性（dst_attr）
                dds.append(("dst_attr", Dropdown(self._dst_attr_rect(row), BLOCK_ATTR_OPTIONS, self.small_font)))
                dds[-1][1].set_value(b.get("dst_attr", "防御力"))
        return dds

    def handle_click(self, pos):
        # 底部"添加被动"按钮
        if self._add_btn_rect().collidepoint(pos):
            self.add_block()
            return True
        # 行内按钮与下拉 / 输入框
        for row in range(self.scroll, min(self.scroll + self.rows_visible, len(self.blocks))):
            r = self._row_rect(row)
            if not r.collidepoint(pos):
                continue
            # 先处理行操作按钮
            if self._up_rect(row).collidepoint(pos) and row > 0:
                self.blocks[row - 1], self.blocks[row] = self.blocks[row], self.blocks[row - 1]
                self._sync_inputs()
                return True
            if self._down_rect(row).collidepoint(pos) and row < len(self.blocks) - 1:
                self.blocks[row + 1], self.blocks[row] = self.blocks[row], self.blocks[row + 1]
                self._sync_inputs()
                return True
            if self._del_rect(row).collidepoint(pos):
                del self.blocks[row]
                self._sync_inputs()
                return True
            # 下拉
            self._active_input = None
            self._active_name = None
            self._active_cond_pct = None
            for kind, dd in self._dropdowns(row):
                if dd.clicked(pos):
                    self._active_drop = (row, kind)
                    dd.open = True
                    return True
            # 条件百分比输入框（条件为"生命低于/高于X%"时显示）
            if self.blocks[row].get("condition") in BLOCK_COND_NEEDS_PCT:
                cp = self._cond_pct_rect(row)
                if cp.collidepoint(pos):
                    self._active_cond_pct = row
                    self._active_input = None
                    self._active_name = None
                    self._active_drop = None
                    self._cond_pct_rows[row].handle_event(
                        pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
                    return True
            # 名字输入框
            nrect = self._name_rect(row)
            if nrect.collidepoint(pos):
                self._active_name = row
                self._active_input = None
                self._active_cond_pct = None
                self._active_drop = None
                self._name_rows[row].handle_event(
                    pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
                return True
            # 数值输入框
            if BLOCK_EFFECT_NEEDS_VALUE.get(self.blocks[row]["effect"], True):
                vrect = self._value_rect(row)
                if vrect.collidepoint(pos):
                    self._active_input = row
                    self._active_name = None
                    self._active_cond_pct = None
                    self._active_drop = None
                    self._input_rows[row].handle_event(
                        pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
                    return True
        # 点击展开中的下拉选项
        if self._active_drop:
            row, kind = self._active_drop
            if 0 <= row < len(self.blocks):
                for k, dd in self._dropdowns(row):
                    if k == kind:
                        idx = dd.option_at(pos)
                        if idx is not None:
                            dd.index = idx
                            self._apply_drop_selection(row, kind, dd.value)
                        dd.open = False
                        self._active_drop = None
                        return True
        return False

    def _apply_drop_selection(self, row, kind, value):
        b = self.blocks[row]
        if kind == "trigger":
            # 复活效果只能由"死亡"触发，不允许改为其他时机
            if b["effect"] == "复活":
                return
            b["trigger"] = value
        elif kind == "condition":
            # value 是显示文案，反查 key
            for k, label, _ in BLOCK_COND_OPTIONS:
                if label == value:
                    b["condition"] = k
                    break
        elif kind == "effect":
            old = b["effect"]
            b["effect"] = value
            if value == "复活":
                # 复活只能由"死亡"触发，选择复活效果时强制时机为"死亡"
                b["trigger"] = "死亡"
            if value == "临时Buff" and b["attr"] not in BLOCK_TEMPBUFF_ATTRS:
                b["attr"] = BLOCK_TEMPBUFF_ATTRS[0]
            if old in BLOCK_EFFECT_NEEDS_ATTR and value not in BLOCK_EFFECT_NEEDS_ATTR:
                b["attr"] = "攻击力"
            # 属性转移：确保目标属性与源属性不同
            if value == "属性转移":
                if b.get("dst_attr", "防御力") == b["attr"]:
                    b["dst_attr"] = "防御力" if b["attr"] != "防御力" else "攻击力"
            # 效果变化后重建输入框
            self._sync_inputs()
        elif kind == "attr":
            b["attr"] = value
        elif kind == "dst_attr":
            b["dst_attr"] = value

    def handle_key(self, event):
        if self._active_input is not None and self._active_input in self._input_rows:
            box = self._input_rows[self._active_input]
            box.handle_event(event)
            self.blocks[self._active_input]["value"] = box.text
        elif self._active_name is not None and self._active_name in self._name_rows:
            box = self._name_rows[self._active_name]
            box.handle_event(event)
            self.blocks[self._active_name]["name"] = box.text
        elif self._active_cond_pct is not None and self._active_cond_pct in self._cond_pct_rows:
            box = self._cond_pct_rows[self._active_cond_pct]
            box.handle_event(event)
            self.blocks[self._active_cond_pct]["cond_pct"] = box.text

    def handle_text(self, event):
        if self._active_input is not None and self._active_input in self._input_rows:
            box = self._input_rows[self._active_input]
            box.handle_event(event)
            self.blocks[self._active_input]["value"] = box.text
        elif self._active_name is not None and self._active_name in self._name_rows:
            box = self._name_rows[self._active_name]
            box.handle_event(event)
            self.blocks[self._active_name]["name"] = box.text
        elif self._active_cond_pct is not None and self._active_cond_pct in self._cond_pct_rows:
            box = self._cond_pct_rows[self._active_cond_pct]
            box.handle_event(event)
            self.blocks[self._active_cond_pct]["cond_pct"] = box.text

    def handle_wheel(self, dy):
        if self.rect.collidepoint(pygame.mouse.get_pos()):
            max_scroll = max(0, len(self.blocks) - self.rows_visible)
            self.scroll = max(0, min(self.scroll - dy, max_scroll))

    # ---------- 绘制 ----------
    def draw(self, surface):
        pygame.draw.rect(surface, DARK, self.rect, border_radius=8)
        pygame.draw.rect(surface, GRAY, self.rect, width=1, border_radius=8)
        # 绘制积木行
        for row in range(self.scroll, min(self.scroll + self.rows_visible, len(self.blocks))):
            self._draw_row(surface, row)
        # 底部：添加按钮 + 条数/滚轮提示
        self._draw_footer(surface)

    def _draw_row(self, surface, row):
        b = self.blocks[row]
        r = self._row_rect(row)
        need_attr = BLOCK_EFFECT_NEEDS_ATTR.get(b["effect"], False)
        expanded = self._active_drop and self._active_drop[0] == row
        # 名字输入框（始终显示，可填名字便于抽取时引用）
        nrect = self._name_rect(row)
        if self._active_name == row:
            self._name_rows[row].draw(surface)
        else:
            pygame.draw.rect(surface, DARK, nrect, border_radius=5)
            pygame.draw.rect(surface, GRAY, nrect, width=1, border_radius=5)
            name_text = (b.get("name") or "").strip()
            label = name_text or "名字"
            t = self.small_font.render(label, True, WHITE if name_text else GRAY)
            surface.blit(t, (nrect.x + 6, nrect.y + 4))
        for kind, dd in self._dropdowns(row):
            dd.draw(surface, expanded=(expanded and kind == (self._active_drop[1] if self._active_drop else None)))
        # 条件百分比输入框（条件为"生命低于/高于X%"时显示）
        if b.get("condition") in BLOCK_COND_NEEDS_PCT:
            cp = self._cond_pct_rect(row)
            if self._active_cond_pct == row:
                self._cond_pct_rows[row].draw(surface)
            else:
                pygame.draw.rect(surface, DARK, cp, border_radius=5)
                pygame.draw.rect(surface, GRAY, cp, width=1, border_radius=5)
                pct_label = b.get("cond_pct", "50")
                t = self.small_font.render(str(pct_label), True, WHITE)
                surface.blit(t, (cp.x + 6, cp.y + 4))
        # 数值输入框（未占用时手动绘制；占用时由 InputBox 自绘）
        if BLOCK_EFFECT_NEEDS_VALUE.get(b["effect"], True):
            vrect = self._value_rect(row)
            if self._active_input == row:
                self._input_rows[row].draw(surface)
            else:
                pygame.draw.rect(surface, DARK, vrect, border_radius=5)
                pygame.draw.rect(surface, GRAY, vrect, width=1, border_radius=5)
                hint = BLOCK_EFFECT_VALUE_HINT.get(b["effect"], "数值")
                label = b["value"] or hint
                t = self.small_font.render(label, True, WHITE if b["value"] else GRAY)
                surface.blit(t, (vrect.x + 6, vrect.y + 4))
        # 行操作按钮
        for name, rct in (("↑", self._up_rect(row)), ("↓", self._down_rect(row)), ("×", self._del_rect(row))):
            pygame.draw.rect(surface, ACCENT if rct.collidepoint(pygame.mouse.get_pos()) else (60, 60, 75),
                             rct, border_radius=5)
            t = self.small_font.render(name, True, WHITE)
            surface.blit(t, t.get_rect(center=rct.center))
        # 永久标记（属性叠加时为小开关提示）
        if b["effect"] == "属性叠加":
            perm = self.small_font.render("永久" if b["permanent"] else "临时", True, YELLOW)
            surface.blit(perm, (r.x + 700, r.y + 6))
        # 文本预览（小字灰色）
        preview = self.small_font.render("→ " + self._build_effect(b), True, (150, 150, 160))
        surface.blit(preview, (r.x + 700, r.y + 6))

    def _draw_footer(self, surface):
        y = self.rect.bottom - self.FOOTER_H + 4
        # 添加按钮
        ab = self._add_btn_rect()
        pygame.draw.rect(surface, ACCENT, ab, border_radius=5)
        t = self.small_font.render("+ 添加被动", True, BLACK)
        surface.blit(t, (ab.x + 6, ab.y + 2))
        # 条数 / 滚轮提示
        msg = f"当前共 {len(self.blocks)} 条被动"
        if len(self.blocks) > self.rows_visible:
            msg += "（滚轮滚动查看更多）"
        mt = self.small_font.render(msg, True, YELLOW)
        surface.blit(mt, (self.rect.x + self.rect.w - 4 - mt.get_width(), y))


class App:
    # 战斗日志导出文件（每次进入程序时清空）
    BATTLE_LOG_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "battle_log.txt",
    )

    def __init__(self):
        pygame.init()
        # 每次进入程序清空战斗日志文件
        try:
            with open(self.BATTLE_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        pygame.display.set_caption("Rogue Demo - 肉鸽游戏")
        # 开启 SDL 文本输入模式，支持中文 IME（产生 TEXTINPUT 事件）
        try:
            pygame.key.start_text_input()
        except Exception:
            pass
        self.clock = pygame.time.Clock()
        self.font = self._make_font(24)
        self.small_font = self._make_font(18)
        self.title_font = self._make_font(44)
        self.running = True
        self.state = "menu"
        # state: menu / design / mode_select / choose_wave / fighting /
        #        choose_reinforce / game_over / victory

        self.character = Character()
        self.session = None
        self.mode = "offline"
        self.ai_result_text = ""
        self.result_title = ""
        self.result_sub = ""
        self.result_death = ""   # 玩家死亡报告（game_over 时显示）
        # 通关结算画面（两页：玩家属性/被动 + 战斗日志）
        self.summary_page = 0    # 0=玩家属性/被动, 1=战斗日志
        self.summary_scroll = 0  # 战斗日志滚动偏移
        self.summary_btn_next = None
        self.summary_btn_prev = None
        self.summary_btn_back = None

        # AI 后台调用（避免阻塞 UI，等待期间显示进度提示）
        self.ai_busy = False            # 是否正在后台调用 AI
        self.ai_thread = None           # 后台线程
        self.ai_busy_text = ""          # 等待弹窗文案
        self.ai_busy_action = None      # 线程完成后要执行的可调用对象
        self.ai_busy_t = 0.0            # 等待动画计时

        # 设计界面输入框（位置与 _draw_design 的布局保持一致）
        self.name_box = InputBox((160, 108, 300, 32), self.font, "自定义角色")
        self.stat_boxes = {}
        for i, key in enumerate(CHARACTER_FRAMEWORK["属性"].keys()):
            y = 196 + i * 44
            default = str(DEFAULT_STATS.get(key, "0"))
            self.stat_boxes[key] = InputBox((220, y, 160, 32), self.font, default)
        self.passive_area = TextArea((60, 120, 880, 440), self.font)
        # 敌方被动池编辑区
        self.pool_area = TextArea((60, 120, 880, 440), self.font)
        self.pool_message = ""
        self.passive_warning = ""
        # 积木式被动编辑器（与文本模式共用同一片区域；角色被动页与敌方被动池页共用组件）
        self.passive_editor = PassiveBlockEditor((60, 120, 880, 440), self.font, self.small_font)
        self.pool_editor = PassiveBlockEditor((60, 120, 880, 440), self.font, self.small_font)
        self.design_editor_active = False   # True=积木模式，False=文本模式
        self.pool_editor_active = False

        # 动态按钮
        self.action_buttons = []
        # 被动说明弹窗：None 表示关闭；否则为 (标题, 行内容列表)
        self.help_popup = None
        self.help_btn_close = None
        self.help_scroll = 0  # 弹窗内容滚动偏移

        # AI 辅助设计设置页（点击"AI 辅助设计"后进入，用于输入 API Key）
        self.ai_setup_message = ""
        self.ai_api_key_box = None
        self.ai_base_box = None
        self.ai_model_box = None
        self.ai_btn_save = None
        self.ai_btn_back = None
        self._init_ai_setup_inputs()

    def _make_font(self, size: int):
        """创建支持中文的字体。

        直接用 pygame.font.Font 加载系统中文字体文件（避免 SysFont 在部分
        Windows 环境下枚举字体目录崩溃导致回退默认字体、中文乱码）。
        """
        # 常见中文字体文件路径（按优先级）
        font_candidates = [
            r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
            r"C:\Windows\Fonts\msyh.ttf",
            r"C:\Windows\Fonts\simhei.ttf",     # 黑体
            r"C:\Windows\Fonts\simsun.ttc",     # 宋体
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\Deng.ttf",       # 等线
            r"C:\Windows\Fonts\msjh.ttc",       # 微软正黑
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
        for path in font_candidates:
            try:
                if os.path.exists(path):
                    return pygame.font.Font(path, size)
            except Exception:
                continue
        # 最后回退：尝试 SysFont，失败则用默认字体
        for name in ("microsoftyahei", "microsoftyaheiui", "simhei", "simsun"):
            try:
                return pygame.font.SysFont(name, size)
            except Exception:
                continue
        return pygame.font.Font(None, size)

    # ---------- AI 辅助设计设置 ----------
    def _init_ai_setup_inputs(self):
        """初始化 AI 设置页的输入框与按钮（进入页面时调用）。"""
        cfg = load_api_config()
        key = cfg.get("api_key", "")
        if key == "在此填入你的API_KEY":
            key = ""
        self.ai_api_key_box = InputBox((340, 220, 420, 36), self.font, key)
        self.ai_base_box = InputBox(
            (340, 320, 420, 36), self.font,
            cfg.get("base_url", "https://api.openai.com/v1/chat/completions"))
        self.ai_model_box = InputBox(
            (340, 420, 420, 36), self.font, cfg.get("model", "gpt-4o-mini"))
        self.ai_btn_save = Button((SCREEN_W // 2 - 240, 540, 160, 48), "保存并开始", self.small_font)
        self.ai_btn_record = Button((SCREEN_W // 2 - 60, 540, 120, 48), "记录", self.small_font)
        self.ai_btn_clear = Button((SCREEN_W // 2 + 80, 540, 120, 48), "清除", self.small_font)
        self.ai_btn_back = Button((SCREEN_W // 2 + 220, 540, 120, 48), "返回", self.small_font)

    def _record_ai_key(self):
        """记录（保存）当前输入的 API Key，不启动游戏。"""
        key = self.ai_api_key_box.text.strip()
        if not key or key == "在此填入你的API_KEY":
            self.ai_setup_message = "请先输入有效的 API Key 再记录。"
            return
        cfg = load_api_config()
        save_api_config(
            api_key=key,
            base_url=self.ai_base_box.text.strip(),
            model=self.ai_model_box.text.strip(),
            temperature=cfg.get("temperature", 0.8),
        )
        self.ai_setup_message = "API Key 已记录并保存到本地配置。"

    def _clear_ai_key(self):
        """清除已记录/保存的 API Key，并清空文本框。"""
        cfg = load_api_config()
        save_api_config(
            api_key="",
            base_url=self.ai_base_box.text.strip(),
            model=self.ai_model_box.text.strip(),
            temperature=cfg.get("temperature", 0.8),
        )
        self.ai_api_key_box.text = ""
        self.ai_api_key_box._initial = ""
        self.ai_setup_message = "已清除 API Key 记录。"
        self._init_ai_setup_inputs()

    def _save_ai_config_and_start(self):
        """保存 AI 配置并尝试进入 AI 模式。"""
        key = self.ai_api_key_box.text.strip()
        if not key or key == "在此填入你的API_KEY":
            self.ai_setup_message = "请先输入有效的 API Key。"
            return
        cfg = load_api_config()
        save_api_config(
            api_key=key,
            base_url=self.ai_base_box.text.strip(),
            model=self.ai_model_box.text.strip(),
            temperature=cfg.get("temperature", 0.8),
        )
        self._start_run("ai")

    # ---------- 事件处理 ----------
    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                self._handle_key(event)
            elif event.type == pygame.TEXTINPUT:
                self._handle_text_input(event)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_click(event.pos)
            elif event.type == pygame.MOUSEWHEEL:
                if self.help_popup is not None:
                    # 说明弹窗内容滚动
                    lines = self.help_popup[1]
                    self.help_scroll = max(0, min(len(lines) - 1,
                                                  self.help_scroll - event.y))
                elif self.state == "design_passive":
                    if self.design_editor_active:
                        self.passive_editor.handle_wheel(event.y)
                    else:
                        self.passive_area.handle_event(event)
                elif self.state == "pool_config":
                    if self.pool_editor_active:
                        self.pool_editor.handle_wheel(event.y)
                    else:
                        self.pool_area.handle_event(event)
                elif self.state == "summary" and self.summary_page == 1:
                    # 通关结算·战斗日志页滚动
                    lines = self.session.summary_battle_logs
                    self.summary_scroll = max(0, min(len(lines) - 1,
                                                     self.summary_scroll - event.y))
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button in (4, 5) \
                    and self.help_popup is not None:
                # 兼容旧版滚轮事件
                lines = self.help_popup[1]
                self.help_scroll = max(0, min(len(lines) - 1,
                                              self.help_scroll + (1 if event.button == 5 else -1)))

    def _handle_text_input(self, event):
        """转发文本输入事件（支持中文 IME）给当前激活的输入框。"""
        if self.state == "design":
            self.name_box.handle_event(event)
            for b in self.stat_boxes.values():
                b.handle_event(event)
        elif self.state == "design_passive":
            if self.design_editor_active:
                self.passive_editor.handle_text(event)
            else:
                self.passive_area.handle_event(event)
        elif self.state == "pool_config":
            if self.pool_editor_active:
                self.pool_editor.handle_text(event)
            else:
                self.pool_area.handle_event(event)
        elif self.state == "ai_setup":
            for box in (self.ai_api_key_box, self.ai_base_box, self.ai_model_box):
                box.handle_event(event)

    def _handle_key(self, event):
        # AI 后台调用中：忽略键盘输入
        if self.state == "ai_busy":
            return
        # 说明弹窗打开时，ESC 关闭
        if self.help_popup is not None:
            if event.key == pygame.K_ESCAPE:
                self.help_popup = None
            return
        if self.state == "design":
            self.name_box.handle_event(event)
            for b in self.stat_boxes.values():
                b.handle_event(event)
        elif self.state == "design_passive":
            if self.design_editor_active:
                self.passive_editor.handle_key(event)
            else:
                self.passive_area.handle_event(event)
        elif self.state == "pool_config":
            if self.pool_editor_active:
                self.pool_editor.handle_key(event)
            else:
                self.pool_area.handle_event(event)
        elif self.state == "ai_setup":
            for box in (self.ai_api_key_box, self.ai_base_box, self.ai_model_box):
                box.handle_event(event)
        elif self.state == "fighting":
            if event.key == pygame.K_1:
                self._attack_current()

    def _handle_click(self, pos):
        # AI 后台调用中：忽略所有点击，避免误操作
        if self.state == "ai_busy":
            return
        # 说明弹窗打开时，优先处理弹窗关闭，其余点击忽略
        if self.help_popup is not None:
            if self.help_btn_close and self.help_btn_close.clicked(pos):
                self.help_popup = None
            return
        if self.state == "menu":
            if self.menu_btn_design.clicked(pos):
                self.state = "design"
            elif self.menu_btn_play.clicked(pos):
                if self._collect_character():
                    self.state = "mode_select"
            elif self.menu_btn_pool.clicked(pos):
                self._open_pool_config()
            elif self.menu_btn_quit.clicked(pos):
                self.running = False
        elif self.state == "design":
            # 第一步：名称与属性
            if self.design_btn_next.clicked(pos):
                self._load_character_passives_into_editor()
                self.state = "design_passive"
            elif self.design_btn_back.clicked(pos):
                self.state = "menu"
            for box in [self.name_box, *self.stat_boxes.values()]:
                box.handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
        elif self.state == "design_passive":
            # 第二步：被动技能
            if self.design_btn_confirm.clicked(pos):
                self._collect_character()
                self.state = "mode_select"
            elif self.design_btn_back.clicked(pos):
                self.state = "design"
            elif self.design_btn_help.clicked(pos):
                self.help_popup = ("被动技能说明", PASSIVE_HELP_LINES)
            elif self.design_btn_toggle.clicked(pos):
                self._toggle_design_editor()
            elif self.design_editor_active:
                self.passive_editor.handle_click(pos)
            else:
                self.passive_area.handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
        elif self.state == "pool_config":
            if self.pool_btn_save.clicked(pos):
                self._save_pool_config()
            elif self.pool_btn_back.clicked(pos):
                self.state = "menu"
            elif self.pool_btn_help.clicked(pos):
                self.help_popup = ("敌方被动池说明", PASSIVE_HELP_LINES)
            elif self.pool_btn_toggle.clicked(pos):
                self._toggle_pool_editor()
            elif self.pool_editor_active:
                self.pool_editor.handle_click(pos)
            else:
                self.pool_area.handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
        elif self.state == "mode_select":
            for btn in self.mode_buttons:
                if btn.clicked(pos):
                    if btn.text == "离线模式":
                        self._start_run("offline")
                    elif btn.text == "AI 辅助设计":
                        # 先进入 API Key 设置页，配置好再启动 AI 模式
                        self._init_ai_setup_inputs()
                        self.ai_setup_message = ""
                        self.state = "ai_setup"
                    elif btn.text == "返回主菜单":
                        self.state = "menu"
        elif self.state == "ai_setup":
            if self.ai_btn_save.clicked(pos):
                self._save_ai_config_and_start()
            elif self.ai_btn_record.clicked(pos):
                self._record_ai_key()
            elif self.ai_btn_clear.clicked(pos):
                self._clear_ai_key()
            elif self.ai_btn_back.clicked(pos):
                self.state = "mode_select"
            for box in (self.ai_api_key_box, self.ai_base_box, self.ai_model_box):
                box.handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
        elif self.state == "fighting":
            if getattr(self, "fight_btn_view_enemy", None) and self.fight_btn_view_enemy.clicked(pos):
                self._open_status_popup(enemy=True)
            elif getattr(self, "fight_btn_view_player", None) and self.fight_btn_view_player.clicked(pos):
                self._open_status_popup(enemy=False)
            elif getattr(self, "fight_btn_save_log", None) and self.fight_btn_save_log.clicked(pos):
                self._save_battle_log()
        elif self.state == "choose_wave":
            for btn in self.action_buttons:
                if btn.clicked(pos):
                    multiwave = btn.text == "多波"
                    if self.mode == "ai":
                        # AI 模式生成怪物会调用大模型（耗时），放后台线程避免卡 UI
                        self._start_ai_thread(
                            lambda m=multiwave: self._ai_wave_worker(m),
                            self._ai_wave_done,
                            f"AI 正在设计{'多波' if multiwave else '单波'}敌人，请稍候...",
                        )
                    else:
                        self.session.select_wave_mode(multiwave)
                        self._sync_result_state()
        elif self.state == "choose_reinforce":
            for btn in self.action_buttons:
                if btn.clicked(pos):
                    if btn.text.startswith("放弃强化换治疗"):
                        self.session.choose_heal_exchange(100.0)
                        self._sync_result_state()
                    elif getattr(btn, "attr", None) is not None:
                        # 强化选项，btn 保存了属性名
                        self.session.choose_reinforcement(btn.attr)
                        self._sync_result_state()
        elif self.state == "summary":
            # 通关结算画面：翻页 / 返回主菜单
            if self.summary_btn_next and self.summary_btn_next.clicked(pos):
                self.summary_page = 1 if self.summary_page == 0 else 0
                self.summary_scroll = 0
            elif self.summary_btn_prev and self.summary_btn_prev.clicked(pos):
                self.summary_page = 0 if self.summary_page == 1 else 1
                self.summary_scroll = 0
            elif self.summary_btn_back and self.summary_btn_back.clicked(pos):
                self.state = "menu"
        elif self.state in ("game_over", "victory"):
            if self.state == "game_over" or self.state == "victory":
                # 点击任意区域返回主菜单
                self.state = "menu"

    def _save_battle_log(self):
        """把当前战斗日志写入 battle_log.txt（覆盖式）。"""
        try:
            s = self.session
            lines = []
            if s and s.current_battle:
                b = s.current_battle
                header = f"第 {s.current_level}/{s.level_count} 关 战斗 {s.battle_index+1}/{len(s.monsters)}"
                lines.append(header)
                lines.append("=" * 30)
                lines.extend(b.log)
            with open(self.BATTLE_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.log_save_status = f"已保存 {len(lines)} 行到 battle_log.txt"
        except OSError as e:
            self.log_save_status = f"保存失败: {e}"

    # ---------- 战斗状态弹窗 ----------
    def _status_lines(self, unit) -> List[str]:
        """把战斗单位（Combatant）的全部状态/属性转换为弹窗行内容。"""
        pct = lambda v: f"{v*100:.0f}%"
        lines = [f"【{unit.name}】", f"生命值: {unit.hp:.0f} / {unit.max_hp:.0f}",
                 f"攻击力: {unit.eff_atk:.1f}（基础 {unit.atk:.1f}）",
                 f"防御力: {unit.eff_def:.1f}（基础 {unit.defense:.1f}）",
                 f"暴击率: {pct(unit.eff_crit)}",
                 f"连击率: {unit.eff_combo:.1f}"]
        if unit.temp_shield > 0:
            lines.append(f"护盾: {unit.temp_shield:.0f}")
        if unit.temp_atk or unit.temp_def or unit.temp_crit or unit.temp_combo:
            lines.append("临时Buff: "
                         + " ".join(f"{k}{v:+.1f}"
                                    for k, v in (("攻击", unit.temp_atk), ("防御", unit.temp_def),
                                                 ("暴击", unit.temp_crit), ("连击", unit.temp_combo))
                                    if v))
        if unit.invuln_turns > 0:
            lines.append(f"无敌: 剩余 {unit.invuln_turns} 次")
        if unit._lifesteal_ratio > 0 or unit._lifesteal_amount > 0:
            ls_parts = []
            if unit._lifesteal_amount > 0:
                ls_parts.append(f"每次{unit._lifesteal_amount:g}点")
            if unit._lifesteal_ratio > 0:
                ls_parts.append(pct(unit._lifesteal_ratio))
            lines.append("吸血: " + " + ".join(ls_parts))
        if unit.evade_chance > 0:
            lines.append(f"闪避: {pct(unit.evade_chance)}" + ("（必定闪避）" if unit.evade else ""))
        if unit._revive_chance > 0:
            lines.append(f"复活: {pct(unit._revive_chance)}（死亡时概率复活）")
        if unit._berserk_ratio > 0 or unit._berserk_ratio_pct > 0:
            below = unit._berserk_hp_below
            above = unit._berserk_hp_above
            thr = ""
            if below > 0:
                thr += f"生命低于 {below*100:.0f}%"
            if above > 0:
                thr += ((" 或 " if thr else "") + f"生命高于 {above*100:.0f}%")
            bk_parts = []
            if unit._berserk_ratio > 0:
                bk_parts.append(f"{unit._berserk_ratio:g}点")
            if unit._berserk_ratio_pct > 0:
                bk_parts.append(pct(unit._berserk_ratio_pct))
            lines.append("狂暴: 攻击力提升 " + " + ".join(bk_parts)
                         + (f"（{thr}）" if thr else ""))
        lines.append("被动: " + ("、".join(str(p) for p in unit.passives) if unit.passives else "无"))
        return lines

    def _open_status_popup(self, enemy: bool):
        """打开战斗状态弹窗：enemy=True 展示敌方，否则展示我方。"""
        b = self.session.current_battle if self.session else None
        if not b:
            return
        if enemy:
            lines = ["·".join(f"{m.name}" for m in b.monsters if m.alive) or "无存活敌人"]
            for m in b.monsters:
                if m.alive:
                    lines.append("")
                    lines.extend(self._status_lines(m))
            self.help_popup = ("敌方状态", lines)
        else:
            self.help_popup = ("我方状态", self._status_lines(b.player))

    def _attack_current(self):
        if self.session and self.session.state == "fighting":
            self.session.player_attack_current(0)
            self._sync_result_state()

    def _sync_result_state(self):
        """同步会话状态到 UI state，用于切换画面。"""
        s = self.session
        if s.state == "game_over":
            self.state = "game_over"
            self.result_title = "失败..."
            # 死亡报告：死于什么伤害、多少数值
            death_reason = ""
            if s.current_battle is not None:
                death_reason = s.current_battle.player_death_reason
            self.result_sub = f"你倒在了第 {s.current_level} 关。击杀 {s.stats['kills']} 只怪物。"
            self.result_death = death_reason
        elif s.state == "victory":
            self.result_title = "通关！"
            hint = getattr(s, "victory_hint", "")
            sub = f"你通关了全部 {s.level_count} 关！击杀 {s.stats['kills']} 只怪物。"
            if hint:
                sub += f"\n{hint}"
            self.result_sub = sub
            self.result_death = ""
            # 若有完整战斗结算数据，进入结算画面（两页可翻页）；否则直接胜利界面
            if getattr(s, "summary_battle_logs", None):
                self._init_summary()
                self.state = "summary"
            else:
                self.state = "victory"
        else:
            self.state = s.state

    # ---------- 通关结算画面 ----------
    def _init_summary(self):
        """初始化通关结算画面（默认显示第一页：玩家属性/被动）。"""
        self.summary_page = 0
        self.summary_scroll = 0
        self.summary_btn_prev = Button((40, 640, 120, 45), "上一页", self.small_font)
        self.summary_btn_next = Button((170, 640, 120, 45), "下一页", self.small_font)
        self.summary_btn_back = Button((SCREEN_W - 160, 640, 120, 45), "返回主菜单", self.small_font)

    def _draw_summary(self):
        """通关结算画面：第一页玩家属性/被动，第二页完整战斗日志。"""
        s = self.session
        # 标题
        title = self.title_font.render("通关结算", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 50)))
        # 页码
        page_label = self.small_font.render(
            f"第 {self.summary_page + 1} / 2 页   ·   {s.victory_hint or ''}", True, GRAY)
        self.screen.blit(page_label, page_label.get_rect(center=(SCREEN_W // 2, 100)))

        if self.summary_page == 0:
            # 第一页：玩家属性 + 被动
            self._draw_summary_page1(s)
        else:
            # 第二页：战斗日志
            self._draw_summary_page2(s)

        self.summary_btn_prev.draw(self.screen)
        self.summary_btn_next.draw(self.screen)
        self.summary_btn_back.draw(self.screen)
        # 战斗日志页支持滚轮
        if self.summary_page == 1:
            scroll_hint = self.small_font.render("滚轮滚动查看更多战斗日志", True, YELLOW)
            self.screen.blit(scroll_hint, (SCREEN_W - 260, 120))

    def _draw_summary_page1(self, s):
        """结算第一页：玩家属性和被动。"""
        y = 140
        # 玩家属性
        l1 = self.font.render("【玩家属性】", True, GREEN)
        self.screen.blit(l1, (60, y))
        y += 34
        stats = s.summary_player_stats
        for k, v in stats.items():
            line = f"  {k}: {v:g}"
            ln = self.small_font.render(line, True, WHITE)
            self.screen.blit(ln, (80, y))
            y += 26
        y += 20
        # 玩家被动
        l2 = self.font.render("【玩家被动技能】", True, GREEN)
        self.screen.blit(l2, (60, y))
        y += 34
        passives = s.summary_player_passives
        if passives:
            for p in passives:
                pl = self.small_font.render(f"  {p}", True, WHITE)
                self.screen.blit(pl, (80, y))
                y += 26
        else:
            pl = self.small_font.render("  无", True, GRAY)
            self.screen.blit(pl, (80, y))

    def _draw_summary_page2(self, s):
        """结算第二页：完整战斗日志（支持滚动）。"""
        lines = s.summary_battle_logs
        # 绘制可视区域内的日志行（从 summary_scroll 开始）
        max_visible = 21
        start = max(0, min(self.summary_scroll, max(0, len(lines) - max_visible)))
        y = 130
        for i in range(start, min(start + max_visible, len(lines))):
            line = lines[i]
            # 分隔线/标题用绿色，其余用白/灰
            if line.startswith("第") and "关 ·" in line:
                color = GREEN
            elif set(line) == {"="}:
                color = GRAY
            else:
                color = WHITE if not line.startswith("  ") else GRAY
            ln = self.small_font.render(line, True, color)
            self.screen.blit(ln, (40, y))
            y += 24
        if not lines:
            empty = self.small_font.render("（暂无战斗日志）", True, GRAY)
            self.screen.blit(empty, (60, 160))

    # ---------- 角色收集 ----------
    def _collect_character(self) -> bool:
        self.character.name = self.name_box.text or "自定义角色"
        self.character.stats = dict(DEFAULT_STATS)
        for key, box in self.stat_boxes.items():
            text = box.text.strip()
            self.character.stats[key] = float(text) if text.lstrip("-").replace(".", "").isdigit() else 0.0
        text = self.passive_editor.get_text() if self.design_editor_active \
            else self.passive_area.get_text()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        self.character.passives = [Passive.parse(l) for l in lines]
        self.character.current_hp = self.character.get("生命值", 100)
        # 校验每条被动是否可被引擎解析，无法解析的给出警告（不阻止保存）
        from . import passive_engine
        bad = [l for l in lines if not passive_engine.validate(l)[0]]
        if bad:
            self.passive_warning = f"有 {len(bad)} 条被动无法解析（战斗中将提示未生效）：{bad[0]}"
        else:
            self.passive_warning = ""
        return True

    def _start_run(self, mode: str):
        self.mode = mode
        self.character.reset_to_full()
        # AI 模式：预校验 API 可用性，并使用 AI 作为关卡怪物生成器。
        # 校验放到后台线程，等待期间显示进度提示，避免 UI 卡死。
        if mode == "ai":
            self._start_ai_thread(
                self._ai_verify_worker,
                lambda ok, err: self._ai_verify_done(ok, err),
                "正在校验 AI 连接，请稍候...",
            )
            return
        self.session = GameSession(self.character, mode=mode, monster_provider=None)
        self.state = "choose_wave"

    # ---------- AI 后台调用 ----------
    def _start_ai_thread(self, worker, done_callback, busy_text: str):
        """启动一个后台线程执行 AI 调用，UI 进入等待状态。

        worker(no_args) -> (ok, err) 返回 (是否成功, 错误信息或 None)。
        done_callback(ok, err) 在主线程执行收尾。
        """
        self.ai_busy = True
        self.ai_busy_text = busy_text
        self.ai_busy_t = 0.0
        self.state = "ai_busy"
        self._ai_done_callback = done_callback

        def _run():
            try:
                ok, err = worker()
            except Exception as e:  # 兜底
                ok, err = False, str(e)
            self.ai_result = (ok, err)

        self.ai_result = (False, "")
        self.ai_thread = threading.Thread(target=_run, daemon=True)
        self.ai_thread.start()

    def _ai_verify_worker(self):
        """后台：轻量校验 AI 连接（只发极小请求，快速确认 key/接口/模型可用）。"""
        from .ai_design import verify_api_connection
        try:
            verify_api_connection()
            return True, None
        except Exception as e:
            return False, str(e)

    def _ai_verify_done(self, ok, err):
        """校验完成后（主线程）：成功则进入 AI 模式，失败回设置页并提示。"""
        if ok:
            self.ai_result_text = "AI 已连接，将逐关设计敌人"
            self.session = GameSession(self.character, mode="ai",
                                       monster_provider=ai_monster_provider)
            self.state = "choose_wave"
        else:
            self.ai_result_text = f"AI 调用失败：{err}"
            self.state = "mode_select"

    def _ai_wave_worker(self, multiwave):
        """后台：选择波次并生成 AI 怪物。"""
        try:
            self.session.select_wave_mode(multiwave)
            return True, None
        except Exception as e:
            return False, str(e)

    def _ai_wave_done(self, ok, err):
        """波次生成完成后（主线程）：同步 UI 状态。"""
        if ok:
            self._sync_result_state()
        else:
            self.ai_result_text = f"AI 生成敌人失败：{err}"
            self.state = "mode_select"

    def _poll_ai_thread(self):
        """主循环轮询：后台 AI 线程完成后执行收尾回调。"""
        if not self.ai_busy or self.ai_thread is None:
            return
        if self.ai_thread.is_alive():
            return  # 仍在调用
        self.ai_busy = False
        self.ai_thread = None
        cb = getattr(self, "_ai_done_callback", None)
        ok, err = self.ai_result
        if cb is not None:
            cb(ok, err)

    # ---------- 敌方被动池配置 ----------
    def _open_pool_config(self):
        """打开敌方被动池编辑界面，载入当前配置。"""
        from .passive_pool import load_pool
        pool = load_pool()
        self.pool_area.set_text("\n".join(pool))
        self.pool_editor.set_text("\n".join(pool))
        self.pool_message = "编辑敌方被动池（每行一条），点击【保存】写入配置"
        self.state = "pool_config"

    def _save_pool_config(self):
        """保存被动池到配置文件。"""
        from .passive_pool import save_pool, default_pool
        text = self.pool_editor.get_text() if self.pool_editor_active \
            else self.pool_area.get_text()
        pool = [l.strip() for l in text.splitlines() if l.strip()]
        if not pool:
            pool = default_pool()
            self.pool_message = "被动池为空，已恢复默认被动池"
        else:
            # 校验每条被动是否可被引擎解析，无法解析的提示
            from . import passive_engine
            bad = [l for l in pool if not passive_engine.validate(l)[0]]
            if bad:
                self.pool_message = f"已保存 {len(pool)} 条，但 {len(bad)} 条无法解析（战斗中不会生效）"
            else:
                self.pool_message = f"已保存 {len(pool)} 条被动到配置"
        save_pool(pool)

    def _load_character_passives_into_editor(self):
        """进入被动设计页时，若编辑器为空则从角色已保存的被动载入。"""
        text = self.passive_area.get_text()
        if text.strip():
            return
        lines = [str(p) for p in self.character.passives]
        self.passive_area.set_text("\n".join(lines))
        if self.design_editor_active:
            self.passive_editor.set_text(self.passive_area.get_text())

    # ---------- 文本/积木模式切换 ----------
    def _toggle_design_editor(self):
        """角色设计页被动编辑区：文本模式 <-> 积木模式。"""
        if self.design_editor_active:
            # 积木 -> 文本：把积木转文本填入文本区
            self.passive_area.set_text(self.passive_editor.get_text())
            self.design_editor_active = False
        else:
            # 文本 -> 积木：从现有文本载入积木
            self.passive_editor.set_text(self.passive_area.get_text())
            self.design_editor_active = True

    def _toggle_pool_editor(self):
        """敌方被动池编辑区：文本模式 <-> 积木模式。"""
        if self.pool_editor_active:
            self.pool_area.set_text(self.pool_editor.get_text())
            self.pool_editor_active = False
        else:
            self.pool_editor.set_text(self.pool_area.get_text())
            self.pool_editor_active = True

    # ---------- 绘制 ----------
    def draw(self):
        self.screen.fill(BLACK)
        if self.state == "menu":
            self._draw_menu()
        elif self.state == "design":
            self._draw_design()
        elif self.state == "design_passive":
            self._draw_design_passive()
        elif self.state == "mode_select":
            self._draw_mode_select()
        elif self.state == "ai_setup":
            self._draw_ai_setup()
        elif self.state == "ai_busy":
            self._draw_ai_busy()
        elif self.state == "pool_config":
            self._draw_pool_config()
        elif self.state == "choose_wave":
            self._draw_choose_wave()
        elif self.state == "fighting":
            self._draw_fight()
        elif self.state == "choose_reinforce":
            self._draw_reinforce()
        elif self.state == "summary":
            self._draw_summary()
        elif self.state in ("game_over", "victory"):
            self._draw_end()
        # 说明弹窗覆盖层（最后绘制，显示在最上层）
        if self.help_popup is not None:
            self._draw_help_popup()
        pygame.display.flip()

    def _draw_help_popup(self):
        """绘制被动说明弹窗（半透明遮罩 + 面板 + 关闭按钮）。"""
        # 半透明遮罩
        overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180))
        self.screen.blit(overlay, (0, 0))
        # 面板
        panel_w, panel_h = 720, 520
        panel = pygame.Rect((SCREEN_W - panel_w) // 2, (SCREEN_H - panel_h) // 2,
                            panel_w, panel_h)
        pygame.draw.rect(self.screen, DARK, panel, border_radius=12)
        pygame.draw.rect(self.screen, ACCENT, panel, width=2, border_radius=12)
        # 标题
        title, lines = self.help_popup
        tl = self.title_font.render(title, True, ACCENT)
        self.screen.blit(tl, (panel.x + 20, panel.y + 12))
        # 内容（支持鼠标滚轮滚动）
        visible = 20
        start = min(self.help_scroll, max(0, len(lines) - visible))
        y = panel.y + 70
        for line in lines[start:start + visible]:
            l = self.small_font.render(line, True, WHITE if not line or not line.startswith("  ") else GRAY)
            self.screen.blit(l, (panel.x + 24, y))
            y += 21
        # 滚动提示
        if len(lines) > visible:
            hint = self.small_font.render(
                f"{start + 1}-{min(start + visible, len(lines))}/{len(lines)} 行（滚轮滚动）",
                True, YELLOW)
            self.screen.blit(hint, (panel.x + 24, panel.bottom - 78))
        # 关闭按钮
        self.help_btn_close = Button(
            (panel.right - 130, panel.bottom - 50, 100, 36), "关闭", self.small_font)
        self.help_btn_close.draw(self.screen)

    def _draw_menu(self):
        title = self.title_font.render("Rogue Demo", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 120)))
        sub = self.font.render("肉鸽游戏原型 - 自定义角色 + 7 关挑战", True, GRAY)
        self.screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 170)))
        self.menu_btn_design = Button((SCREEN_W // 2 - 150, 250, 300, 50), "角色设计", self.font)
        self.menu_btn_play = Button((SCREEN_W // 2 - 150, 320, 300, 50), "开始游戏", self.font)
        self.menu_btn_pool = Button((SCREEN_W // 2 - 150, 390, 300, 50), "敌方被动池", self.font)
        self.menu_btn_quit = Button((SCREEN_W // 2 - 150, 460, 300, 50), "退出", self.font)
        for b in (self.menu_btn_design, self.menu_btn_play, self.menu_btn_pool, self.menu_btn_quit):
            b.draw(self.screen)

    def _draw_pool_config(self):
        """敌方被动池编辑界面。"""
        title = self.font.render("敌方基本被动池配置", True, ACCENT)
        self.screen.blit(title, (40, 25))
        # 精简顶部提示为一行
        mode_label = "积木模式" if self.pool_editor_active else "文本模式"
        tip = self.small_font.render(
            f"编辑敌方被动池（当前 {mode_label}），格式 [时机|条件] 效果，点【被动说明】查看示例",
            True, GRAY)
        self.screen.blit(tip, (40, 70))
        # 编辑区（按模式绘制）
        if self.pool_editor_active:
            self.pool_editor.draw(self.screen)
        else:
            self.pool_area.draw(self.screen)
            # 文本模式：编辑区底部空闲处显示条数/滚轮提示
            n = len([l for l in self.pool_area.lines if l.strip()])
            hint = self.small_font.render(
                f"当前共 {n} 条被动" +
                ("（滚轮滚动查看更多）" if n > self.pool_area.MAX_VISIBLE else ""),
                True, YELLOW)
            self.screen.blit(hint, (900 - hint.get_width(), 552))
        # 状态提示
        msg = self.small_font.render(self.pool_message, True, GREEN if "保存" in self.pool_message or "默认" in self.pool_message else WHITE)
        self.screen.blit(msg, (40, 585))
        # 按钮
        self.pool_btn_toggle = Button((60, 620, 140, 45),
                                      "切到积木" if not self.pool_editor_active else "切到文本", self.font)
        self.pool_btn_help = Button((440, 620, 140, 45), "被动说明", self.font)
        self.pool_btn_save = Button((600, 620, 140, 45), "保存", self.font)
        self.pool_btn_back = Button((760, 620, 140, 45), "返回", self.font)
        self.pool_btn_toggle.draw(self.screen)
        self.pool_btn_help.draw(self.screen)
        self.pool_btn_save.draw(self.screen)
        self.pool_btn_back.draw(self.screen)

    def _draw_design(self):
        """角色设计（第一步）：角色名称 + 属性。"""
        title = self.font.render("角色设计（1/2：名称与属性）", True, ACCENT)
        self.screen.blit(title, (40, 30))

        # 顶部提示
        tip = self.small_font.render(
            "操作提示：先点击文本框，再直接输入数字即可替换原值（激活后首次输入会清空原内容）",
            True, YELLOW)
        self.screen.blit(tip, (40, 75))

        # 名称
        nlabel = self.small_font.render("角色名称:", True, WHITE)
        self.screen.blit(nlabel, (60, 115))
        self.name_box.draw(self.screen)

        # 属性
        attr_title = self.small_font.render("普通属性 / 稀有属性（点击输入框修改）:", True, YELLOW)
        self.screen.blit(attr_title, (60, 165))
        for i, (key, box) in enumerate(self.stat_boxes.items()):
            y = 196 + i * 44
            desc = CHARACTER_FRAMEWORK["属性"][key]
            klabel = self.small_font.render(f"{key}:", True, WHITE)
            self.screen.blit(klabel, (80, y + 5))
            box.draw(self.screen)
            dlabel = self.small_font.render(desc, True, GRAY)
            self.screen.blit(dlabel, (400, y + 5))

        self.design_btn_next = Button((700, 600, 160, 50), "下一步：设计被动", self.font)
        self.design_btn_back = Button((840, 600, 130, 50), "返回", self.font)
        self.design_btn_next.draw(self.screen)
        self.design_btn_back.draw(self.screen)

    def _draw_design_passive(self):
        """角色设计（第二步）：被动技能编辑。"""
        title = self.font.render("角色设计（2/2：被动技能）", True, ACCENT)
        self.screen.blit(title, (40, 30))
        mode_label = "积木模式" if self.design_editor_active else "文本模式"
        ptitle = self.small_font.render(
            f"被动技能（{mode_label}，格式: [触发时机|条件] 名字：效果，名字可留空）:", True, YELLOW)
        self.screen.blit(ptitle, (60, 75))
        if self.design_editor_active:
            self.passive_editor.draw(self.screen)
        else:
            self.passive_area.draw(self.screen)
            n = len([l for l in self.passive_area.lines if l.strip()])
            phint = self.small_font.render(
                f"共 {n} 条" + ("，滚轮滚动查看更多" if n > self.passive_area.MAX_VISIBLE else ""),
                True, YELLOW)
            self.screen.blit(phint, (700, 78))
        if self.passive_warning:
            wl = self.small_font.render(self.passive_warning, True, RED)
            self.screen.blit(wl, (60, 600))

        self.design_btn_back = Button((60, 620, 130, 45), "上一步", self.font)
        self.design_btn_toggle = Button((350, 620, 130, 45),
                                        "切到积木" if not self.design_editor_active else "切到文本", self.font)
        self.design_btn_help = Button((500, 620, 130, 45), "被动说明", self.font)
        self.design_btn_confirm = Button((700, 620, 130, 45), "确认", self.font)
        self.design_btn_back.draw(self.screen)
        self.design_btn_toggle.draw(self.screen)
        self.design_btn_help.draw(self.screen)
        self.design_btn_confirm.draw(self.screen)

    def _draw_mode_select(self):
        title = self.title_font.render("选择模式", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 120)))
        info = self.small_font.render(
            f"角色: {self.character.name} | 被动: {len(self.character.passives)} 条", True, GRAY)
        self.screen.blit(info, info.get_rect(center=(SCREEN_W // 2, 170)))
        desc = [
            "离线模式: 按属性预算生成 7 关敌人，被动从玩家构建与系统池抽取",
            "AI 辅助设计: 调用大模型设计每一关的针对性敌人",
        ]
        for i, d in enumerate(desc):
            l = self.small_font.render(d, True, WHITE)
            self.screen.blit(l, (SCREEN_W // 2 - l.get_width() // 2, 200 + i * 26))
        self.mode_buttons = [
            Button((SCREEN_W // 2 - 150, 300, 300, 55), "离线模式", self.font),
            Button((SCREEN_W // 2 - 150, 380, 300, 55), "AI 辅助设计", self.font),
            Button((SCREEN_W // 2 - 150, 480, 300, 45), "返回主菜单", self.small_font),
        ]
        for b in self.mode_buttons:
            b.draw(self.screen)
        if self.ai_result_text:
            e = self.small_font.render(self.ai_result_text, True, RED)
            self.screen.blit(e, (SCREEN_W // 2 - 200, 560))

    def _draw_ai_setup(self):
        """AI 辅助设计设置页：让玩家输入 API Key 并启动。"""
        title = self.title_font.render("AI 辅助设计设置", True, ACCENT)
        self.screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 100)))
        hint = self.small_font.render(
            "AI 模式会调用大模型逐关设计敌人，需填写 API Key（仅保存在本地配置文件）", True, GRAY)
        self.screen.blit(hint, hint.get_rect(center=(SCREEN_W // 2, 160)))
        sub = self.small_font.render(
            "【记录】保存输入的 Key 到本地；【清除】删除已保存记录并清空输入框", True, YELLOW)
        self.screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 190)))

        labels = [
            ("API Key", self.ai_api_key_box, 220, "必填，大模型鉴权密钥"),
            ("Base URL", self.ai_base_box, 320, "OpenAI 兼容接口地址（含 /chat/completions 端点）"),
            ("模型名称", self.ai_model_box, 420, "如 gpt-4o-mini"),
        ]
        for label, box, y, sub in labels:
            lb = self.font.render(label, True, WHITE)
            self.screen.blit(lb, (300, y + 5))
            box.draw(self.screen)
            s = self.small_font.render(sub, True, GRAY)
            self.screen.blit(s, (300, y + 42))

        self.ai_btn_save.draw(self.screen)
        self.ai_btn_record.draw(self.screen)
        self.ai_btn_clear.draw(self.screen)
        self.ai_btn_back.draw(self.screen)
        if self.ai_setup_message:
            msg = self.small_font.render(self.ai_setup_message, True,
                                         RED if "请先" in self.ai_setup_message or "失败" in self.ai_setup_message else GREEN)
            self.screen.blit(msg, msg.get_rect(center=(SCREEN_W // 2, 620)))

    def _draw_ai_busy(self):
        """AI 后台调用等待弹窗：显示进度动画与已等待时长。"""
        self.ai_busy_t += 0.033  # 粗略按帧增量（主循环 30fps）
        # 半透明遮罩
        overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        self.screen.blit(overlay, (0, 0))
        # 面板
        panel_w, panel_h = 520, 240
        panel = pygame.Rect((SCREEN_W - panel_w) // 2, (SCREEN_H - panel_h) // 2,
                            panel_w, panel_h)
        pygame.draw.rect(self.screen, DARK, panel, border_radius=12)
        pygame.draw.rect(self.screen, ACCENT, panel, width=2, border_radius=12)
        # 标题
        tl = self.title_font.render("AI 调用中", True, ACCENT)
        self.screen.blit(tl, tl.get_rect(center=(SCREEN_W // 2, panel.y + 48)))
        # 旋转进度动画（一个转圈的小球）
        cx, cy = SCREEN_W // 2, panel.y + 130
        import math
        for i in range(8):
            ang = self.ai_busy_t * 2.0 + i * math.pi / 4
            x = cx + 22 * math.cos(ang)
            y = cy + 22 * math.sin(ang)
            alpha = 80 + i * 20
            pygame.draw.circle(self.screen, (min(255, alpha + 60), 200, 255), (int(x), int(y)), 6)
        # 等待文案
        text = self.font.render(self.ai_busy_text, True, WHITE)
        self.screen.blit(text, text.get_rect(center=(SCREEN_W // 2, panel.y + 175)))
        # 已等待时长
        secs = int(self.ai_busy_t)
        el = self.small_font.render(f"已等待 {secs} 秒，请耐心等候，AI 生成通常需要 10~60 秒",
                                    True, GRAY)
        self.screen.blit(el, el.get_rect(center=(SCREEN_W // 2, panel.y + 208)))

    def _draw_choose_wave(self):
        s = self.session
        title = self.font.render(f"第 {s.current_level}/{s.level_count} 关", True, ACCENT)
        self.screen.blit(title, (40, 40))
        info = self.small_font.render(
            f"生命 {self.character.current_hp:.1f}/{self.character.max_hp:.1f} | "
            f"进入本关自动回复 10% 生命", True, GRAY)
        self.screen.blit(info, (40, 90))
        if s.message:
            msg = self.small_font.render(s.message, True, GREEN)
            self.screen.blit(msg, (40, 130))
        hint = self.font.render("选择本关敌人波次：", True, WHITE)
        self.screen.blit(hint, (SCREEN_W // 2 - 160, 240))
        wave_info = self.small_font.render(
            f"单波: 敌人普通属性之和 = 你的普通属性之和(1:1) | "
            f"多波: = 你的 1.5 倍，波数 {s.wave_count} 随深度增加", True, GRAY)
        self.screen.blit(wave_info, (SCREEN_W // 2 - 330, 280))
        self.action_buttons = [
            Button((SCREEN_W // 2 - 250, 360, 200, 60), "单波", self.font),
            Button((SCREEN_W // 2 + 50, 360, 200, 60), "多波", self.font),
        ]
        for b in self.action_buttons:
            b.draw(self.screen)

    def _draw_fight(self):
        s = self.session
        if not s.current_battle:
            return
        b = s.current_battle
        title = self.font.render(
            f"第 {s.current_level}/{s.level_count} 关 - 战斗 {s.battle_index+1}/{len(s.monsters)}",
            True, ACCENT)
        self.screen.blit(title, (40, 25))

        # 玩家状态（左侧卡片）
        p = b.player
        pygame.draw.rect(self.screen, DARK, (40, 60, 440, 90), border_radius=8)
        plabel = self.font.render(
            f"{p.name}   HP {p.hp:.0f}/{p.max_hp:.0f}",
            True, GREEN if p.hp > p.max_hp * 0.3 else RED)
        self.screen.blit(plabel, (55, 70))
        psub = self.small_font.render(
            f"攻击 {p.eff_atk:.1f} | 防御 {p.eff_def:.1f} | 暴击 {p.eff_crit*100:.0f}% | 连击 {p.eff_combo:.1f}"
            + (f"  |  护盾 {p.temp_shield:.0f}" if p.temp_shield > 0 else ""),
            True, WHITE)
        self.screen.blit(psub, (55, 110))

        # 当前怪物（右侧卡片）
        for i, m in enumerate(b.monsters):
            if not m.alive:
                continue
            pygame.draw.rect(self.screen, DARK, (520, 60, 440, 150), border_radius=8)
            ml = self.title_font.render(f"{m.name}", True, RED)
            self.screen.blit(ml, (535, 70))
            mhp = self.font.render(f"HP {m.hp:.0f}/{m.max_hp:.0f}", True,
                                   GREEN if m.hp > m.max_hp * 0.3 else RED)
            self.screen.blit(mhp, (535, 120))
            # 血量条
            ratio = max(0.0, min(1.0, m.hp / m.max_hp))
            pygame.draw.rect(self.screen, (80, 30, 30), (535, 160, 400, 14), border_radius=4)
            pygame.draw.rect(self.screen, (200, 60, 60), (535, 160, int(400 * ratio), 14), border_radius=4)
            msub = self.small_font.render(
                f"攻 {m.atk:.1f} 防 {m.defense:.1f} 暴 {m.crit_rate*100:.0f}% 连 {m.combo_rate:.1f}",
                True, WHITE)
            self.screen.blit(msub, (535, 185))
            passive_str = "、".join(str(p) for p in m.passives) if m.passives else "无"
            ptag = self.small_font.render(f"被动: {passive_str}", True, YELLOW)
            self.screen.blit(ptag, (535, 205))

        # 攻击提示（大按钮式）
        pygame.draw.rect(self.screen, ACCENT, (40, 220, 920, 62), border_radius=8)
        hint = self.font.render(
            f"按 [1] 键攻击！  |  本关剩余敌人 {s.enemies_remaining} 只  |  {'多波' if s.multiwave else '单波'}",
            True, BLACK)
        self.screen.blit(hint, hint.get_rect(center=(SCREEN_W // 2, 234)))
        # 剩余回合数：显示当前回合 / 有效上限（动态算法与硬性上限99取更小）
        eff_limit = b.effective_turn_limit
        remain = max(0, eff_limit - b.player.turn)
        turn_info = f"回合 {b.player.turn}/{eff_limit}"
        if b.turn_limit is not None:
            turn_info += f"（剩余 {remain} 回合）"
        else:
            turn_info += "（硬性上限 99）"
        turn_label = self.small_font.render(turn_info, True, (40, 20, 20))
        self.screen.blit(turn_label, turn_label.get_rect(center=(SCREEN_W // 2, 262)))

        # 战斗日志
        log_title = self.small_font.render("战斗日志（按 1 后查看行动结果）:", True, GRAY)
        self.screen.blit(log_title, (40, 290))
        for i, line in enumerate(b.log[-9:]):
            l = self.small_font.render(line, True, WHITE)
            self.screen.blit(l, (60, 320 + i * 24))
        # 底部按钮区：查看敌方状态 / 查看我方状态 / 保存战斗日志
        self.fight_btn_view_enemy = Button((40, 560, 200, 40), "查看敌方状态", self.small_font)
        self.fight_btn_view_enemy.draw(self.screen)
        self.fight_btn_view_player = Button((260, 560, 200, 40), "查看我方状态", self.small_font)
        self.fight_btn_view_player.draw(self.screen)
        self.fight_btn_save_log = Button((480, 560, 200, 40), "保存战斗日志", self.small_font)
        self.fight_btn_save_log.draw(self.screen)
        log_status = getattr(self, "log_save_status", "")
        if log_status:
            st = self.small_font.render(log_status, True, YELLOW)
            self.screen.blit(st, (700, 570))

    def _draw_reinforce(self):
        s = self.session
        title = self.font.render(
            f"第 {s.current_level} 关击败所有敌人！", True, GREEN)
        self.screen.blit(title, (40, 40))
        info = self.small_font.render(
            f"生命 {self.character.current_hp:.1f}/{self.character.max_hp:.1f} "
            f"| 已损失 {self.character.lost_hp:.1f} | 三选一强化", True, GRAY)
        self.screen.blit(info, (40, 90))
        # 单波/多波不同的通关提示
        if getattr(s, "victory_hint", ""):
            vh = self.small_font.render(s.victory_hint, True, YELLOW)
            self.screen.blit(vh, (40, 120))
        # 强化选项
        self.action_buttons = []
        for i, attr in enumerate(s.reinforce_options):
            desc = s.reinforcement_desc(attr)
            btn = Button((80, 160 + i * 90, 520, 60), desc, self.small_font)
            btn.attr = attr
            self.action_buttons.append(btn)
        # 治疗换算选项
        lost = self.character.lost_hp
        heal = heal_exchange_amount(self.character, lost, 100.0)
        heal_btn = Button((80, 160 + 3 * 90, 520, 60),
                          f"放弃强化换治疗（回复 {heal:.1f} 生命）", self.small_font)
        self.action_buttons.append(heal_btn)
        for btn in self.action_buttons:
            btn.draw(self.screen)
        note = self.small_font.render(
            "多波模式强化数值 x1.5 | 点击选项确认", True, YELLOW)
        self.screen.blit(note, (80, 160 + 3 * 90 + 70))

    def _draw_end(self):
        color = GREEN if self.state == "victory" else RED
        title = self.title_font.render(self.result_title, True, color)
        self.screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 200)))
        # 主文案：支持多行（按 \n 拆行）
        sub_lines = self.result_sub.splitlines() if self.result_sub else [""]
        y = 280
        for line in sub_lines:
            sl = self.font.render(line, True, WHITE)
            self.screen.blit(sl, sl.get_rect(center=(SCREEN_W // 2, y)))
            y += 32
        stats = self.small_font.render(
            f"击杀 {self.session.stats['kills']} | 治疗交换 {self.session.stats['heal_exchanged']:.1f}",
            True, GRAY)
        self.screen.blit(stats, stats.get_rect(center=(SCREEN_W // 2, y)))
        y += 40
        # 死亡报告：我方死于什么伤害、多少数值
        if self.result_death:
            death = self.font.render(self.result_death, True, RED)
            self.screen.blit(death, death.get_rect(center=(SCREEN_W // 2, y)))
            y += 40
            hint = self.small_font.render("点击任意位置返回主菜单", True, GRAY)
            self.screen.blit(hint, hint.get_rect(center=(SCREEN_W // 2, y)))
        else:
            hint = self.small_font.render("点击任意位置返回主菜单", True, GRAY)
            self.screen.blit(hint, hint.get_rect(center=(SCREEN_W // 2, y)))

    def run(self):
        while self.running:
            self.handle_events()
            self._poll_ai_thread()  # 检查后台 AI 调用是否完成
            self.draw()
            self.clock.tick(30)
        pygame.quit()

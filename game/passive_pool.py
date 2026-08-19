"""敌方基本被动池配置。

提供从 config/settings.json 读取/保存"敌方被动池"的能力。
默认值在 LEVEL_CONFIG["monster_passive_pool"] 中，若配置文件中未定义则用默认。
"""

import json
import os
from typing import List

from .character import LEVEL_CONFIG, PASSIVE_TRIGGERS, Passive

# 配置文件路径
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "settings.json",
)

# 配置文件中敌方被动池的字段名
ENEMY_POOL_KEY = "enemy_passive_pool"


def default_pool() -> List[str]:
    """返回默认的敌方被动池（来自 LEVEL_CONFIG）。"""
    return list(LEVEL_CONFIG["monster_passive_pool"])


def load_pool() -> List[str]:
    """从配置文件加载敌方被动池；无配置时返回默认池。"""
    if not os.path.exists(CONFIG_PATH):
        return default_pool()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default_pool()
    pool = cfg.get(ENEMY_POOL_KEY)
    if isinstance(pool, list):
        return [str(p).strip() for p in pool if str(p).strip()]
    return default_pool()


def save_pool(pool: List[str]) -> None:
    """保存敌方被动池到配置文件（合并到现有配置，保留 api 等其他字段）。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg[ENEMY_POOL_KEY] = [str(p).strip() for p in pool if str(p).strip()]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def parse_pool_passives(pool: List[str]) -> List[Passive]:
    """把被动池文本列表解析为 Passive 对象列表。"""
    return [Passive.parse(p) for p in pool if p.strip()]


def validate_passive(text: str) -> bool:
    """校验单条被动是否合法（可解析且触发时机有效）。

    使用 passive_engine 做真实解析校验：触发时机有效 + 效果可解析。
    返回 (ok, reason) 中的 ok。空文本视为非法。
    """
    from . import passive_engine
    t = text.strip()
    if not t:
        return False
    p = passive_engine.parse(t)
    return p.ok


def validate_passive_detail(text: str) -> tuple:
    """校验单条被动，返回 (ok, reason)。供 UI 提示不可解析的具体原因。"""
    from . import passive_engine
    t = text.strip()
    if not t:
        return False, "空被动"
    return passive_engine.validate(t)

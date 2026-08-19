"""AI 辅助设计模式。

读取本地配置中的 API Key，通过预置 Prompt 调用大模型（兼容 OpenAI
Chat Completions 协议），根据玩家角色情况设计对应的敌人。
"""

import datetime
import json
import os
import time
from typing import List, Dict, Optional

import requests

from .character import Character, Passive
from .enemy import Monster

# 本地配置文件路径
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "settings.json",
)

# API 调用日志路径（每次启动程序会清空）
LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "api_log.txt",
)

# 预置的系统 Prompt：指导 AI 如何设计敌人（适配新属性与被动体系）
SYSTEM_PROMPT = """你是肉鸽游戏的怪物设计专家。根据玩家角色的属性和被动，设计一队敌人。

【必须遵守的规则，违反即视为不合格】
1. 敌人的属性字段只能使用：name, atk(攻击力), def(防御力), hp(生命值), crit(暴击率), combo(连击率)。
   禁止出现任何其他属性（如嘲讽、速度、法力、怒气等——这些系统不支持，会导致错误）。
2. 数值约束：crit 必须是 0~1 的小数；hp/atk/def 为正数；combo 为 0 或正整数。
3. 被动技能只能用方括号时机开头，后接效果描述。合法时机仅限：
   [击杀] [回合] [攻击] [受击] [暴击] [连击] [数值] [死亡] [复活] [战斗开始] [战斗结束]
4. 被动效果只能用系统支持的类型：属性叠加(攻击力/防御力/生命值/暴击率/连击率)、闪避、
   吸血、回血、护盾、反弹、斩杀、无敌、属性转移、偷取、额外伤害、狂暴。
   禁止编造不存在的被动效果。
5. 每个敌人必须有攻击能力（atk 为正），被动要有进攻性或针对性；
   禁止全是纯防御/回血/护盾技能而不攻击的"挨打怪"。
6. 数值要与玩家强度大致匹配：单个敌人普通属性(攻击+生命+防御)和≈玩家的一半到八成，
   多波时全体敌人之和≈玩家的1.5倍。避免过强或过弱，全部用加法，禁止指数级数值。
7. 暴击率/连击率按玩家水平给：若玩家暴击/连击较高，怪物也应有可观的暴击/连击，
   不要吝啬；但 crit 仍限 0~1，combo 不超过 3。用攻击+生命+防御+暴击+连击合理搭配
   构成威胁，而不是全靠夸张的额外伤害。
8. 【重要】每个怪物完全独立，禁止任何"怪物之间联动"的被动写法：
   被动只能影响该怪物自己（自身加属性/回血/护盾/狂暴等）。
   严禁出现"传给下一个/下一位敌人""队友/同伴继承/获得""死亡后把属性转移给下一只"
   等跨单位联动描述——不同怪物是依次独立战斗的，这类写法无法生效，会被判定无效。
   敌人间的"配合"仅指它们各自的属性/被动数值搭配合理，不是互相传递增益。

【战斗公式】单次伤害 = 攻击力 × 暴击倍率(未暴击1倍,暴击2倍起) - 防御力，最低1。
【配合要求】敌人间形成坦/输出/辅助的数值搭配，且要针对玩家角色的强项或弱项；
   但各自被动只作用于自己，绝不跨单位传递。

输出必须是合法的 JSON 数组，元素格式：
[{"name":"敌人名称","atk":数值,"def":数值,"hp":数值,"crit":0到1的小数,"combo":数值,"passives":["[攻击] 效果描述"]}]
只输出 JSON，不要任何多余文字。
"""


def _load_config() -> dict:
    """读取本地配置文件。"""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API 调用日志（api_log.txt）：每次启动程序时清空，出错时追加详细记录
# ---------------------------------------------------------------------------
def clear_api_log() -> None:
    """清空 api_log.txt（每次启动程序时调用）。"""
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass


def _append_log(text: str) -> None:
    """追加一行到 api_log.txt。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _log_api_error(context: str, exc: Exception, resp=None) -> None:
    """把一次 API 调用错误写入日志，包含时间、上下文、状态码、响应与排查建议。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"========== {now} ==========",
        f"上下文: {context}",
        f"错误类型: {type(exc).__name__}: {exc}",
    ]
    if resp is not None:
        lines.append(f"HTTP 状态码: {getattr(resp, 'status_code', '?')}")
        try:
            lines.append(f"响应内容: {resp.text[:800]}")
        except Exception:
            lines.append("响应内容: <无法读取>")
    # 对 429 给出更具体的排查建议
    if isinstance(exc, requests.exceptions.HTTPError) and resp is not None \
            and getattr(resp, "status_code", None) == 429:
        lines.append(
            "建议: 429 = 请求过于频繁或达到速率/配额限制。请检查:\n"
            "  - 是否在短时间内反复调用（如频繁进入关卡/换关/多次点击按钮）\n"
            "  - 该模型/账号的每分钟请求数(RPM)与每分钟令牌数(TPM)限制\n"
            "  - 是否使用免费或低配额模型，可尝试换用更高配额的模型\n"
            "  - 稍等片刻后重试，或减少 AI 调用频率"
        )
    elif isinstance(exc, requests.exceptions.HTTPError):
        lines.append("建议: 非 2xx 响应，请检查 API Key、接口地址(base_url)与模型名是否正确。")
    elif isinstance(exc, requests.exceptions.ConnectionError):
        lines.append("建议: 网络连接失败，请检查网络/代理/接口地址是否可访问。")
    elif isinstance(exc, requests.exceptions.Timeout):
        lines.append("建议: 请求超时，可适当调大超时时间或稍后重试。")
    lines.append("========================================")
    _append_log("\n".join(lines))


def _resolve_endpoint(base_url: str) -> str:
    """把用户填写的 base_url 补全为完整的 chat/completions 端点。

    用户可能在设置页只填域名（如 https://api.deepseek.com），若直接 POST 到
    根路径会得到 404。这里统一补全为 OpenAI 兼容的对话端点。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/chat/completions") or url.endswith("/v1/chat/completions"):
        return url
    # 兼容 DeepSeek 官方：https://api.deepseek.com 也接受 /chat/completions
    return url + "/chat/completions"


def _post_and_log(base_url: str, payload: dict, headers: dict, context: str) -> requests.Response:
    """发起 API 请求；网络/超时错误自动重试 1 次，出错时记录日志并抛出异常。"""
    url = _resolve_endpoint(base_url)
    # 元组 timeout: (连接超时 20秒, 读取超时 180秒)。DeepSeek 等模型 prompt 较长，
    # 单值 timeout=120 容易在建立连接后服务器还没返回数据时触发 Read timed out。
    TIMEOUT = (20, 180)
    last_exc = None
    for attempt in (1, 2):  # 最多 2 次（初次 + 1 次重试）
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            # HTTP 错误（4xx/5xx）不重试，避免对 429 加重限流
            _log_api_error(context, e, getattr(e, "response", None))
            status = getattr(e.response, "status_code", None)
            if status == 429:
                raise RuntimeError("API 请求过于频繁（429 Too Many Requests），请稍后重试或降低调用频率。详见 api_log.txt") from e
            raise RuntimeError(f"接口返回错误 {status}: {e}（详见 api_log.txt）") from e
        except requests.exceptions.RequestException as e:
            # 网络/超时/连接错误：自动重试 1 次（attempt=2），间隔 2 秒
            last_exc = e
            if attempt == 1:
                time.sleep(2)
                continue
            _log_api_error(context, e)
            raise RuntimeError(f"网络错误：{e}（已重试 1 次，详见 api_log.txt）") from e
    # 理论上不会到这里，但保留兜底
    raise RuntimeError(f"网络错误：{last_exc}（详见 api_log.txt）")


def load_api_config() -> dict:
    """读取当前 api 配置（base_url / api_key / model / temperature）。"""
    try:
        cfg = _load_config()
    except FileNotFoundError:
        return {}
    return cfg.get("api", {})


def save_api_config(api_key: Optional[str] = None, base_url: str = "",
                    model: str = "", temperature: float = 0.8) -> None:
    """把 api 配置写回 settings.json（合并保留其他字段）。

    api_key：None=不修改；空字符串=清除；其他=写入。base_url/model 为空时保留原值。
    """
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError):
            config = {}
    api_cfg = dict(config.get("api", {}))
    if api_key is not None:
        # 显式传空字符串用于清除，传 None 保留原值
        api_cfg["api_key"] = api_key.strip()
    if base_url and base_url.strip():
        api_cfg["base_url"] = base_url.strip()
    if model and model.strip():
        api_cfg["model"] = model.strip()
    if temperature is not None:
        api_cfg["temperature"] = temperature
    config["api"] = api_cfg
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def verify_api_connection() -> None:
    """轻量校验 AI 连接是否可用（发送极小的 prompt，不做完整敌人生成）。

    用于启动时快速确认 API Key / 接口 / 模型是否有效，避免每次启动都触发
    一次完整的大模型生成（那可能耗时数十秒甚至几分钟）。
    """
    config = _load_config()
    api_cfg = config.get("api", {})
    base_url = api_cfg.get("base_url")
    api_key = api_cfg.get("api_key", "")
    model = api_cfg.get("model", "gpt-4o-mini")
    temperature = api_cfg.get("temperature", 0.8)

    if not api_key or api_key == "在此填入你的API_KEY":
        raise RuntimeError("未配置有效的 API Key。请先输入 API Key。")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 8,  # 最小输出，加快响应
        "messages": [
            {"role": "system", "content": "你是一个辅助工具，只回复 OK。"},
            {"role": "user", "content": "回复 OK"},
        ],
    }
    _post_and_log(base_url, payload, headers, "AI 连接校验")


def compute_player_power(character: Character) -> dict:
    """计算玩家的战斗力强度指标，供 AI 参照平衡怪物数值。

    返回：
      atk: 攻击力
      dps: 期望单回合输出 = 攻击力 × 期望暴击倍率 × 期望连击次数
      hp / def: 生命 / 防御
      crit / combo: 暴击率 / 连击率
      ehp: 有效生命 = 生命 + 防御（防御每点抵消 1 伤害/次）
    """
    atk = character.get("攻击力", 0.0)
    hp = character.get("生命值", 0.0)
    defense = character.get("防御力", 0.0)
    crit = character.get("暴击率", 0.0)
    combo = character.get("连击率", 0.0)
    # 期望暴击倍率：未暴击 1 倍、暴击 2 倍 → 1 + crit；crit>1 时超额近似线性加成
    crit_mult = 1.0 + max(0.0, crit)
    # 期望连击次数：combo>=1 时约为 combo；combo<1 时至少 1 次 + 触发额外攻击概率
    if combo >= 1.0:
        hit_mult = combo
    else:
        hit_mult = 1.0 + combo
    dps = atk * crit_mult * hit_mult
    return {
        "atk": atk, "hp": hp, "def": defense,
        "crit": crit, "combo": combo, "dps": dps,
        "ehp": hp + defense,
    }


def build_user_prompt(character: Character) -> str:
    """根据角色信息构造用户 Prompt（紧凑格式，减少 token 以加快生成）。

    附带玩家战斗力指标，并给出怪物数值平衡建议，让 AI 在不了解机制细节时
    也能把数值设计得相对均衡（不偏科于攻击力而吝啬暴击/连击）。
    """
    stats = character.stats
    parts = [f"{k}:{v:g}" for k, v in stats.items()]
    passives = "；".join(str(p) for p in character.passives) or "无"
    p = compute_player_power(character)
    balance_hint = (
        f"玩家强度参考：单回合期望输出≈{p['dps']:.0f}，有效生命≈{p['ehp']:.0f}。"
        f"怪物设计建议：单只怪 HP≈玩家DPS的2~4倍，攻击力≈玩家EHP的1/3~1/5；"
        f"暴击率/连击率按玩家对等水平给出（别吝啬，也不要爆炸超限），"
        f"优先用合理的攻击力+防御力+生命值来平衡，而不是全靠夸张的额外伤害。"
    )
    return (
        f"玩家[{character.name}] 属性[{','.join(parts)}] 被动[{passives}]\n"
        f"{balance_hint}"
    )


def ai_design_enemies(character: Character) -> List[Dict]:
    """调用大模型 API 生成敌人列表。出错时抛出带提示信息的异常。"""
    config = _load_config()
    api_cfg = config.get("api", {})
    base_url = api_cfg.get("base_url")
    api_key = api_cfg.get("api_key", "")
    model = api_cfg.get("model", "gpt-4o-mini")
    temperature = api_cfg.get("temperature", 0.8)

    if not api_key or api_key == "在此填入你的API_KEY":
        raise RuntimeError(
            "未配置有效的 API Key。请编辑 config/settings.json 填入你的 API Key。"
        )

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(character)},
        ],
    }

    resp = _post_and_log(base_url, payload, headers, "AI 辅助设计敌人")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_json_content(content)


def _parse_json_content(content: str) -> List[Dict]:
    """从模型输出中解析 JSON 数组，容忍代码块包裹。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # 解析失败：把原始输出写入 api_log.txt，方便排查
        _log_api_error(
            "AI 输出 JSON 解析失败",
            RuntimeError(f"json.JSONDecodeError: {e}"),
            None,
        )
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write(f"原始输出内容: {content[:800]}\n")
                _f.write("========================================\n")
        except OSError:
            pass
        raise RuntimeError(
            f"模型输出无法解析为 JSON: {e}\n输出内容(已写入 api_log.txt): {content[:500]}"
        )
    if not isinstance(parsed, list):
        raise RuntimeError("模型输出不是 JSON 数组。")
    return parsed


def ai_design_to_monsters(character: Character) -> List[Monster]:
    """将 AI 生成的数据转换为游戏内怪物对象。"""
    enemy_data = ai_design_enemies(character)
    monsters = []
    for item in enemy_data:
        passives = [Passive.parse(str(p)) for p in item.get("passives", [])]
        m = Monster(
            mtype=item.get("name", "AI敌人"),
            hp=float(item.get("hp", 30)),
            atk=float(item.get("atk", 8)),
            defense=float(item.get("def", 2)),
            crit_rate=float(item.get("crit", 0.05)),
            combo_rate=float(item.get("combo", 0)),
            color=(150, 80, 200),
            passives=passives,
        )
        monsters.append(m)
    return monsters


def _level_difficulty_hint(level: int) -> str:
    """返回按关卡设计的难度提示（前四关简单，后三关递增）。"""
    if level <= 4:
        return (
            f"本关为第 {level} 关（前期简单关卡）：怪物属性约为玩家的一半到七成，"
            f"被动数量少且简单，难度应明显低于玩家水平，保证轻松通过。"
        )
    if level == 5:
        return f"本关为第 5 关：怪物属性约为玩家的八成到九成，可带 1 条较强的被动。"
    if level == 6:
        return f"本关为第 6 关：怪物属性约为玩家的 1.0~1.1 倍，被动更具威胁，难度明显提升。"
    return f"本关为最终关（第 7 关）：怪物属性约为玩家的 1.2~1.4 倍，是整局最强挑战，可全力针对玩家。"


# 禁止怪物间联动的被动关键词（命中即丢弃该被动，避免无法生效的跨单位写法）
_LINKAGE_KEYWORDS = (
    "下一个", "下一位", "下一只", "下个", "下名",
    "队友", "同伴", "友军", "同盟",
    "传给", "传递", "继承", "转给", "给予", "交给", "移交给",
    "后续", "下一关", "后续敌人", "其他敌人", "别的敌人",
)


def _is_linkage_passive(passive_text: str) -> bool:
    """判断某条被动文本是否含跨单位联动描述。"""
    for kw in _LINKAGE_KEYWORDS:
        if kw in passive_text:
            return True
    return False


def _filter_passives(passives: List[Passive]) -> List[Passive]:
    """剔除含跨单位联动的被动（怪物独立战斗，此类写法无效）。"""
    return [p for p in passives if not _is_linkage_passive(str(p))]


def ai_monster_provider(character: Character, level: int, multiwave: bool,
                        rng=None, battle_history: Optional[List[dict]] = None) -> List[Monster]:
    """AI 模式的关卡怪物生成器（兼容 level 系统的 provider 签名）。

    根据当前关卡与单波/多波选择，调用大模型生成对应数量与强度的敌人。
    前四关难度简单，后三关递增；第 7 关可参考 battle_history 针对性布置。
    """
    from .level import multiwave_count

    if multiwave:
        n = multiwave_count(level)
        # 第 7 关多波怪物数量减半：避免最终关车轮战过多 BOSS，减轻玩家压力
        if level == 7:
            n = max(1, n // 2)
        prompt_extra = (
            f"本关为多波，请设计 {n} 只不同但相互配合的敌人（依次与玩家作战）。"
            f"为加快生成，请保持输出精简：每只敌人被动不超过 1 条，描述尽量简短。"
        )
    else:
        n = 1
        prompt_extra = "本关为单波，请设计 1 只精英敌人。"

    # 按关卡难度调整
    prompt_extra += _level_difficulty_hint(level)

    # 第 7 关参考前 6 场战斗历史，针对性布置
    if battle_history:
        hist_str = _format_battle_history(battle_history)
        prompt_extra += f"\n\n【前 6 场战斗历史（供你针对性布置）】\n{hist_str}"

    enemy_data = ai_design_enemies_with_extra(character, level, prompt_extra)
    monsters = []
    for item in enemy_data[:n]:
        passives = _filter_passives(
            [Passive.parse(str(p)) for p in item.get("passives", [])])
        monsters.append(Monster(
            mtype=item.get("name", "AI敌人"),
            hp=float(item.get("hp", 30)),
            atk=float(item.get("atk", 8)),
            defense=float(item.get("def", 2)),
            crit_rate=float(item.get("crit", 0.05)),
            combo_rate=float(item.get("combo", 0)),
            color=(150, 80, 200),
            passives=passives,
        ))
    return monsters


def _format_battle_history(history: List[dict]) -> str:
    """把战斗历史摘要格式化为 prompt 文本。"""
    if not history:
        return "（暂无战斗记录）"
    lines = []
    for i, h in enumerate(history, 1):
        lines.append(
            f"{i}. 第{h.get('level')}关 {('多波' if h.get('multiwave') else '单波')} "
            f"第{h.get('battle_index')}场：{h.get('result')}。"
            f"玩家剩余生命 {h.get('player_hp_remain')}/{h.get('player_max_hp')}，"
            f"进行 {h.get('player_turn')} 回合。敌人：{h.get('enemies')}"
        )
    return "\n".join(lines)


def ai_design_enemies_with_extra(character: Character, level: int, extra: str) -> List[Dict]:
    """调用 AI 生成敌人，附带关卡深度与波次说明。"""
    config = _load_config()
    api_cfg = config.get("api", {})
    base_url = api_cfg.get("base_url")
    api_key = api_cfg.get("api_key", "")
    model = api_cfg.get("model", "gpt-4o-mini")
    temperature = api_cfg.get("temperature", 0.8)

    if not api_key or api_key == "在此填入你的API_KEY":
        raise RuntimeError("未配置有效的 API Key。请编辑 config/settings.json 填入你的 API Key。")

    user_prompt = (
        build_user_prompt(character) + f"\n\n关卡信息：第 {level} 关。{extra}"
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "temperature": temperature,
        # 不设 max_tokens：避免设太小导致模型输出被截断/返回空 content，
        # 进而导致 JSON 解析失败（line 1 column 1 char 0 错误）。
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = _post_and_log(base_url, payload, headers, f"AI 生成第 {level} 关敌人")
    data = resp.json()
    # 取出 content：若响应格式异常（无 choices / 无 message / content 为空），
    # 记录原始响应关键信息到 api_log.txt 并抛出友好异常，避免出现难以排查的
    # "Expecting value: line 1 column 1 (char 0)" 错误。
    try:
        choices = data["choices"]
        message = choices[0]["message"]
        content = (message.get("content") or "").strip()
        finish_reason = choices[0].get("finish_reason", "?")
    except (KeyError, IndexError, TypeError) as e:
        _log_api_error(
            f"AI 生成第 {level} 关敌人",
            RuntimeError(f"响应格式异常: {e}"),
            resp,
        )
        raise RuntimeError(
            f"AI 响应格式异常（缺少 choices/message）。详见 api_log.txt"
        ) from e
    if not content:
        # content 为空：常见原因是 max_tokens 截断、模型拒绝输出、网络代理返回空等
        _log_api_error(
            f"AI 生成第 {level} 关敌人",
            RuntimeError("模型返回的 content 为空"),
            resp,
        )
        # 附加一条排查建议
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write(
                    "建议: 模型返回的 content 为空，常见原因：\n"
                    "  - 请求中设了过小的 max_tokens 导致输出被截断（已移除 max_tokens）\n"
                    "  - 模型安全策略拒绝输出（尝试调整 temperature 或简化 prompt）\n"
                    "  - 代理/网关返回了非标准响应\n"
                    "  - 网络超时/中断导致响应不完整\n"
                    f"finish_reason: {finish_reason}\n"
                    "========================================\n"
                )
        except OSError:
            pass
        raise RuntimeError(
            f"AI 返回内容为空（finish_reason={finish_reason}），请重试或检查模型。详见 api_log.txt"
        )
    return _parse_json_content(content)

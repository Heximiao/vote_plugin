from typing import List, Tuple, Type, Any, Dict, Optional
import asyncio
import aiohttp
import json
import re
import logging

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    BaseEventHandler,
    ComponentInfo,
    ConfigField,
    EventType,
    MaiMessages,
)
from src.common.logger import get_logger

logger = get_logger("vote_plugin")

# ================= 全局状态 =================
vote_summaries: Dict[str, Dict] = {}  # message_id -> vote info

# 表情对应投票选项
VOTE_EMOJI = {
    "✅": "yes",
    "🙅": "no",
}

# ================= 辅助函数（用于健壮地解析消息上下文） =================
def _dig(obj: Any, path: str, default=None):
    """
    安全地从嵌套的字典或对象中获取值。
    """
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return default
        if hasattr(cur, seg):
            cur = getattr(cur, seg)
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return default
    return cur

def _first_non_none(*vals):
    """
    返回第一个不为 None 的值。
    """
    for v in vals:
        if v is not None:
            return v
    return None

def _safe_str(x: Any) -> str:
    return str(x) if x is not None else ""

def _first_text(*vals: Any) -> str:
    """返回第一个非空白字符串。"""
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""

def _get_display_name_from_any(user_like: Any, extra_like: Any = None) -> str:
    """
    名称提取（非空优先）。
    """
    name = _first_text(
        _dig(user_like, "user_cardname"),
        _dig(user_like, "card"),
        _dig(user_like, "user_nickname"),
        _dig(user_like, "nickname"),
        _dig(user_like, "nick"),
    )
    if not name and extra_like is not None:
        name = _first_text(
            _dig(extra_like, "user_cardname"),
            _dig(extra_like, "user_nickname"),
        )
    return name

def _resolve_ctx_from_message_any(msg: Any) -> tuple[Optional[str], Optional[str], str, str]:
    """
    健壮的上下文解析函数，能兼容多种消息数据结构。
    返回: (group_id, sender_qq, group_name, sender_name)
    """
    group_id = _first_non_none(
        _dig(msg, "message_info.group_info.group_id"),
        _dig(msg, "group_info.group_id"),
        _dig(msg, "group_id"),
        _dig(msg, "ctx.group_id"),
        _dig(msg, "context.group_id"),
        _dig(msg, "message_base_info.group_id"),
        _dig(msg, "message_base_info.group_info.group_id"),
        _dig(msg, "additional_data.group_id"),
        _dig(msg, "receiver_id"),
    )
    sender_qq = _first_non_none(
        _dig(msg, "message_info.user_info.user_id"),
        _dig(msg, "user_info.user_id"),
        _dig(msg, "user_id"),
        _dig(msg, "sender.user_id"),
        _dig(msg, "sender.id"),
        _dig(msg, "ctx.user_id"),
        _dig(msg, "context.user_id"),
        _dig(msg, "message_base_info.user_id"),
        _dig(msg, "message_base_info.user_info.user_id"),
        _dig(msg, "additional_data.user_id"),
    )
    group_name = _first_text(
        _dig(msg, "message_info.group_info.group_name"),
        _dig(msg, "group_info.group_name"),
        _dig(msg, "group_name"),
        _dig(msg, "message_base_info.group_name"),
        _dig(msg, "message_base_info.group_info.group_name"),
        "",
    )
    
    message_base_info = _dig(msg, "message_base_info")
    user_info_like = _first_non_none(
        _dig(msg, "message_info.user_info"),
        _dig(msg, "user_info"),
        _dig(msg, "sender"),
        _dig(msg, "message_base_info.user_info"),
        {},
    )
    sender_name = _get_display_name_from_any(user_info_like, extra_like=message_base_info)
    
    logger.debug(f"Resolved context: group_id={group_id}, sender_qq={sender_qq}, group_name={group_name}, sender_name={sender_name}")

    if group_id is not None:
        group_id = _safe_str(group_id)
    if sender_qq is not None:
        sender_qq = _safe_str(sender_qq)
    return group_id, sender_qq, _safe_str(group_name), _safe_str(sender_name)

# ================= 核心工具函数（调用 Napcat HTTP API） =================
async def send_group_text_via_napcat(group_id: str, text: str) -> Optional[int]:
    """
    发送群消息，返回 message_id
    """
    url = "127.0.0.1:9998"
    payload = {
        "group_id": int(group_id),
        "message": [{"type": "text", "data": {"text": text}}]
    }
    headers = {"Content-Type": "application/json"}
    logger.info(f"发送群消息请求: {json.dumps(payload, ensure_ascii=False)}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://{url}/send_group_msg", json=payload, headers=headers) as resp:
                data = await resp.json()
                logger.info(f"发送群消息响应: {json.dumps(data, ensure_ascii=False)}")
                return data.get("data", {}).get("message_id")
    except Exception as e:
        logger.error(f"发送群消息失败: {e}")
        return None

async def fetch_emoji_votes(message_id: int, emoji: str, emoji_type: str = "1") -> int:
    """
    查询指定消息贴表情数量
    """
    url = "127.0.0.1:9998"
    payload = {
        "message_id": int(message_id),
        "emojiId": emoji,
        "emojiType": emoji_type
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://{url}/fetch_emoji_like", json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get('retcode') == 0:
                    return len(data.get("data", {}).get("emojiLikesList", []))
                else:
                    logger.warning(f"查询表情 {emoji} 失败，API返回: {data}")
                    return 0
    except Exception as e:
        logger.error(f"查询表情 {emoji} 失败: {e}")
        return 0

async def set_group_ban_via_napcat(group_id: str, user_id: str, minutes: int):
    """
    调用 Napcat HTTP 接口对指定用户进行禁言
    """
    url = "127.0.0.1:9998"
    payload = {
        "group_id": int(group_id),
        "user_id": int(user_id),
        "duration": minutes * 60
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://{url}/set_group_ban", json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("retcode") == 0:
                    logger.info(f"成功对用户 {user_id} 在群 {group_id} 禁言 {minutes} 分钟。")
                else:
                    logger.warning(f"禁言失败，接口返回: {data}")
    except Exception as e:
        logger.error(f"调用禁言接口失败: {e}")

# ================= 命令处理器 =================
class VoteHelpCommand(BaseCommand):
    command_name = "vote_help"
    command_description = "获取投票禁言命令的使用说明"
    command_pattern = r"^/vote\s+help$"

    async def execute(self) -> Tuple[bool, str, bool]:
        help_text = (
            "📖 投票禁言命令使用说明:\n"
            "发起投票: `/投票禁言 @目标QQ号 [时长]`\n"
            "  - @目标QQ: 必须，指定要禁言的用户QQ号。\n"
            "  - 时长: 可选，禁言的分钟数，不填则默认为1分钟。\n"
            "示例:\n"
            "  - `/投票禁言 @12345678` (默认禁言1分钟)\n"
            "  - `/投票禁言 @12345678 5` (禁言5分钟)\n"
            "发起投票后，群成员可以通过在消息上点按钮或点问号来投票。\n"
            "按钮：同意禁言\n"
            "❓：反对禁言"
        )
        await self.send_text(help_text)
        return True, "help_sent", True

class VoteBanCommand(BaseCommand):
    command_name = "vote_ban"
    command_description = "发起投票禁言"
    command_pattern = r"^/投票禁言(?:\s+@?(?P<target>\S+))?(?:\s+(?P<minutes>\d+))?$"

    DEFAULT_CONFIG = {
        "vote_ban": {
            "default_minutes": 1,
            "vote_duration": 60,
        }
    }

    async def execute(self) -> Tuple[bool, str, bool]:
        plugin_config = getattr(self, 'plugin', None)
        if plugin_config and hasattr(plugin_config, 'config'):
            config_data = plugin_config.config
        else:
            config_data = self.DEFAULT_CONFIG
            
        message_parts = _first_non_none(
            _dig(self.message, "message"),
            _dig(self.message, "message_info.message"),
            _dig(self.message, "raw_message"),
            _dig(self.message, "data.message"),
            []
        )

        # 处理只输入 /投票禁言 的情况
        full_command = _first_text(
            _dig(self.message, "message_info.plain_text"),
            _dig(self.message, "raw_message")
        )
        if full_command.strip() == "/投票禁言":
            await self.send_text("请指定要禁言的用户，例如：/投票禁言 @12345 5")
            return False, "no_target", True

        if isinstance(message_parts, str):
            match = re.search(r"@(\d+)", message_parts)
            if match:
                qq_number = match.group(1)
                message_parts = [{'type': 'at', 'data': {'qq': qq_number}}]
            else:
                message_parts = []
        
        group_id, sender_qq, _, _ = _resolve_ctx_from_message_any(self.message)

        if not group_id or not sender_qq:
            await self.send_text("此命令仅能在群聊中使用。")
            return False, "not_in_group", True
            
        if any(v.get('group_id') == group_id for v in vote_summaries.values()):
            await self.send_text("当前群聊已有正在进行的投票，请等待结束。")
            return False, "vote_in_progress", True

        target_str = self.matched_groups.get("target")
        if not target_str:
            await self.send_text("未能识别到被艾特的用户ID，请确保使用 @ 方式艾特。")
            return False, "no_target", True

        try:
            minutes = int(self.matched_groups.get("minutes") or config_data["vote_ban"]["default_minutes"])
        except (ValueError, TypeError):
            minutes = config_data["vote_ban"]["default_minutes"]

        target_user_id = None
        for seg in message_parts:
            if isinstance(seg, dict) and seg.get('type') == 'at':
                if str(seg.get('data', {}).get('qq')) == target_str or (target_str.startswith("@") and str(seg.get('data', {}).get('qq')) == target_str[1:]):
                    target_user_id = str(seg.get('data', {}).get('qq'))
                    break
        
        if not target_user_id:
            await self.send_text("未能识别到被艾特的用户ID，请确保使用 @ 方式艾特。")
            return False, "target_id_not_found", True

        vote_duration = config_data["vote_ban"]["vote_duration"]
        # 更新投票信息中的表情
        text = f"📢 群投票禁言发起\n目标用户: @{target_str}\n禁言时长: {minutes} 分钟\n投票方式: [按钮] 同意 / [❓] 反对\n投票时间: {vote_duration} 秒"

        message_id = await send_group_text_via_napcat(group_id, text)
        if message_id:
            # 存储投票信息时，使用返回的 message_id 作为键
            vote_summaries[str(message_id)] = {
                "group_id": group_id,
                "target_user_id": target_user_id,
                "target_name": target_str,
                "minutes": minutes,
                "yes": 0,
                "no": 0,
            }
            # 启动计时任务，并传入正确的 message_id
            asyncio.create_task(self._end_vote_after_delay(str(message_id)))
            return True, "投票已发起", False
        else:
            return False, "发送投票消息失败", True

    async def _end_vote_after_delay(self, message_id: str):
        plugin_config = getattr(self, 'plugin', None)
        if plugin_config and hasattr(plugin_config, 'config'):
            config_data = plugin_config.config
        else:
            config_data = self.DEFAULT_CONFIG

        # 从配置中获取 debug_mode
        debug_mode = config_data.get("plugin", {}).get("debug_mode", False)
            
        vote_duration = config_data["vote_ban"]["vote_duration"]
        logger.info(f"等待 {vote_duration} 秒后结束投票。")
        await asyncio.sleep(vote_duration)

        # 增加日志：检查 vote_summaries 字典中的所有键
        logger.info(f"检查 vote_summaries 字典... 键列表: {list(vote_summaries.keys())}")
        vote_info = vote_summaries.get(message_id)
        if not vote_info:
            logger.warning(f"消息ID {message_id} 的投票信息已不存在，可能已过期或被移除。")
            return
        
        yes_count = 0
        no_count = 0
        
        # 已经确定表情ID
        EMOJI_YES_ID = "424" # ✅
        EMOJI_NO_ID = "10068" # ❓
        EMOJI_NO_TYPE = "2" # 问号表情的正确emojiType

        # 第一次请求：查询“✅”表情的票数
        try:
            url = "127.0.0.1:9998"
            payload_yes = {
                "message_id": int(message_id),
                "emojiId": EMOJI_YES_ID,
                "emojiType": "1"
            }
            headers = {"Content-Type": "application/json"}
            
            if debug_mode:
                logger.info(f"正在向Napcat请求'同意'票数。发送请求: {json.dumps(payload_yes)}")
            async with aiohttp.ClientSession() as session:
                async with session.post(f"http://{url}/fetch_emoji_like", json=payload_yes, headers=headers) as resp:
                    data = await resp.json()
                    logger.info(f"Napcat返回的'同意'票数据: {json.dumps(data, ensure_ascii=False, indent=2)}")
                    if data.get('retcode') == 0:
                        yes_count = len(data.get("data", {}).get("emojiLikesList", []))
                    else:
                        logger.warning(f"获取同意票失败，Napcat返回错误码: {data.get('retcode')}")
        except Exception as e:
            logger.error(f"查询同意票时发生异常: {e}")
            
        # 第二次请求：查询“❓”表情的票数
        try:
            url = "127.0.0.1:9998"
            payload_no = {
                "message_id": int(message_id),
                "emojiId": EMOJI_NO_ID,
                "emojiType": EMOJI_NO_TYPE
            }
            headers = {"Content-Type": "application/json"}
            
            if debug_mode:
                logger.info(f"正在向Napcat请求'反对'票数。发送请求: {json.dumps(payload_no)}")
            async with aiohttp.ClientSession() as session:
                async with session.post(f"http://{url}/fetch_emoji_like", json=payload_no, headers=headers) as resp:
                    data = await resp.json()
                    logger.info(f"Napcat返回的'反对'票数据: {json.dumps(data, ensure_ascii=False, indent=2)}")
                    if data.get('retcode') == 0:
                        no_count = len(data.get("data", {}).get("emojiLikesList", []))
                    else:
                        logger.warning(f"获取反对票失败，Napcat返回错误码: {data.get('retcode')}")
        except Exception as e:
            logger.error(f"查询反对票时发生异常: {e}")

        logger.info(f"最终统计完成：同意票 {yes_count}，反对票 {no_count}。")
            
        vote_info["yes"] = yes_count
        vote_info["no"] = no_count

        result_text = f"📢 投票结果 - 用户 {vote_info['target_name']}\n"
        result_text += f"[✅] 同意票: {yes_count}\n"
        result_text += f"[❓] 反对票: {no_count}\n"

        if yes_count > no_count:
            result_text += f"投票通过！该用户将被禁言 {vote_info['minutes']} 分钟。"
            await set_group_ban_via_napcat(vote_info['group_id'], vote_info['target_user_id'], vote_info['minutes'])
        else:
            result_text += f"投票未通过，该用户保留自由。"

        await send_group_text_via_napcat(vote_info['group_id'], result_text)
        vote_summaries.pop(message_id, None)

# ================= 插件注册 =================
@register_plugin
class VoteBanPlugin(BasePlugin):
    plugin_name: str = "vote_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["aiohttp"]
    config_file_name: str = "vote_ban_config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "vote_ban": "投票禁言配置",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="vote_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.1.2", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "debug_mode": ConfigField(type=bool, default=False, description="是否启用调试模式，开启后会打印更多日志"),
        },
        "vote_ban": {
            "default_minutes": ConfigField(type=int, default=1, description="默认禁言时长（分钟）"),
            "vote_duration": ConfigField(type=int, default=60, description="投票持续时间（秒）"),
        },
    }
    
    config: dict = {}

    async def on_load(self):
        self.config = self.get_plugin_config()
        if self.config.get("plugin", {}).get("debug_mode", False):
            logger.setLevel(logging.DEBUG)
            logger.info("Debug 模式已开启，将打印详细日志。")
        else:
            logger.setLevel(logging.INFO)
        logger.info(f"配置已加载：{self.config}")

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (VoteHelpCommand.get_command_info(), VoteHelpCommand),
            (VoteBanCommand.get_command_info(), VoteBanCommand),
        ]
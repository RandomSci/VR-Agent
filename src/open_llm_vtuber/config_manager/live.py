from pydantic import Field
from typing import Dict, ClassVar, List, Literal, Optional
from .i18n import I18nMixin, Description


class BiliBiliLiveConfig(I18nMixin):
    """Configuration for BiliBili Live platform."""

    room_ids: List[int] = Field([], alias="room_ids")
    sessdata: str = Field("", alias="sessdata")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "room_ids": Description(
            en="List of BiliBili live room IDs to monitor", zh="要监控的B站直播间ID列表"
        ),
        "sessdata": Description(
            en="SESSDATA cookie value for authenticated requests (optional)",
            zh="用于认证请求的SESSDATA cookie值（可选）",
        ),
    }


class YouTubeLiveConfig(I18nMixin):
    """Configuration for YouTube Live read-only chat integration."""

    youtube_live_enabled: bool = Field(False, alias="youtube_live_enabled")
    chat_source: Literal["playwright", "api"] = Field("playwright", alias="chat_source")
    api_key: str = Field("", alias="api_key")
    channel_id: str = Field("", alias="channel_id")
    channel_handle: str = Field("", alias="channel_handle")
    video_id: Optional[str] = Field(None, alias="video_id")
    prefer_live_chat_mode: bool = Field(True, alias="prefer_live_chat_mode")
    ignore_owner_messages: bool = Field(False, alias="ignore_owner_messages")
    live_check_interval_seconds: int = Field(120, alias="live_check_interval_seconds")
    playwright_headless: bool = Field(True, alias="playwright_headless")
    playwright_user_data_dir: str = Field("", alias="playwright_user_data_dir")
    playwright_user_agent: str = Field("", alias="playwright_user_agent")
    playwright_heartbeat_seconds: float = Field(5.0, alias="playwright_heartbeat_seconds")
    playwright_chat_load_timeout_seconds: int = Field(30, alias="playwright_chat_load_timeout_seconds")
    playwright_restart_min_seconds: float = Field(5.0, alias="playwright_restart_min_seconds")
    playwright_restart_max_seconds: float = Field(120.0, alias="playwright_restart_max_seconds")
    playwright_page_recycle_hours: float = Field(6.0, alias="playwright_page_recycle_hours")
    prefer_stream_list: bool = Field(True, alias="prefer_stream_list")
    message_buffer_seconds: int = Field(180, alias="message_buffer_seconds")
    response_cooldown_seconds: float = Field(5.0, alias="response_cooldown_seconds")
    max_buffer_messages: int = Field(100, alias="max_buffer_messages")
    same_user_cooldown_seconds: int = Field(90, alias="same_user_cooldown_seconds")
    idle_banter_enabled: bool = Field(False, alias="idle_banter_enabled")
    idle_banter_delay_seconds: int = Field(180, alias="idle_banter_delay_seconds")
    discovery_retry_seconds: int = Field(30, alias="discovery_retry_seconds")
    selector_max_messages: int = Field(12, alias="selector_max_messages")
    selector_model: Optional[str] = Field(None, alias="selector_model")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "youtube_live_enabled": Description(
            en="Enable YouTube Live chat ingestion", zh="启用 YouTube 直播聊天读取"
        ),
        "api_key": Description(
            en="YouTube Data API key, preferably provided through an environment variable",
            zh="YouTube Data API key，建议通过环境变量提供",
        ),
        "channel_id": Description(
            en="YouTube channel ID whose active livestream should be monitored",
            zh="要监控的 YouTube 频道 ID",
        ),
        "video_id": Description(
            en="Optional livestream video ID; useful for unlisted test streams",
            zh="可选直播视频 ID；适合非公开测试直播",
        ),
        "chat_source": Description(
            en="Where chat is read from: 'playwright' (live chat page, no API quota) or 'api' (YouTube Data API)",
            zh="聊天来源：'playwright'（读取直播聊天页面，无 API 配额）或 'api'（YouTube Data API）",
        ),
        "channel_handle": Description(
            en="Channel handle such as @selwynbuilds, used to find the active stream when channel_id is not set",
            zh="频道句柄（如 @name），未设置 channel_id 时用于查找直播",
        ),
        "prefer_live_chat_mode": Description(
            en="Switch the chat from 'Top chat' to 'Live chat' once after opening it",
            zh="打开聊天后切换一次到“所有聊天”模式",
        ),
    }


class VRAgentConfig(I18nMixin):
    """Livestream presentation and autonomous character behaviour."""

    overlay_title: str = Field("VR AGENT", alias="overlay_title")
    show_comment_card: bool = Field(True, alias="show_comment_card")
    comment_card_max_chars: int = Field(180, alias="comment_card_max_chars")
    show_state_indicator: bool = Field(True, alias="show_state_indicator")
    idle_motions_enabled: bool = Field(True, alias="idle_motions_enabled")
    idle_min_seconds: float = Field(10.0, alias="idle_min_seconds")
    idle_max_seconds: float = Field(15.0, alias="idle_max_seconds")
    idle_in_dev_mode: bool = Field(True, alias="idle_in_dev_mode")
    viewer_actions_enabled: bool = Field(True, alias="viewer_actions_enabled")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "overlay_title": Description(
            en="Text shown in the small LIVE badge in livestream mode", zh="直播模式角标文字"
        ),
        "show_comment_card": Description(
            en="Show the viewer comment the character is answering", zh="显示角色正在回复的观众评论"
        ),
        "idle_min_seconds": Description(
            en="Minimum seconds between idle motions", zh="待机动作最短间隔（秒）"
        ),
        "idle_max_seconds": Description(
            en="Maximum seconds between idle motions", zh="待机动作最长间隔（秒）"
        ),
        "viewer_actions_enabled": Description(
            en="Let viewers ask for supported gestures such as 'can you nod?'",
            zh="允许观众请求角色支持的动作",
        ),
    }


class LiveConfig(I18nMixin):
    """Configuration for live streaming platforms integration."""

    bilibili_live: BiliBiliLiveConfig = Field(
        BiliBiliLiveConfig(), alias="bilibili_live"
    )
    youtube_live: YouTubeLiveConfig = Field(
        YouTubeLiveConfig(), alias="youtube_live"
    )
    vr_agent: VRAgentConfig = Field(VRAgentConfig(), alias="vr_agent")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "bilibili_live": Description(
            en="Configuration for BiliBili Live platform", zh="B站直播平台配置"
        ),
        "youtube_live": Description(
            en="Configuration for YouTube Live chat integration",
            zh="YouTube 直播聊天集成配置",
        ),
        "vr_agent": Description(
            en="VR Agent livestream presentation and character behaviour",
            zh="VR Agent 直播展示与角色行为",
        ),
    }

from pydantic import Field
from typing import Dict, ClassVar, List, Optional
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
    api_key: str = Field("", alias="api_key")
    channel_id: str = Field("", alias="channel_id")
    video_id: Optional[str] = Field(None, alias="video_id")
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
    }


class LiveConfig(I18nMixin):
    """Configuration for live streaming platforms integration."""

    bilibili_live: BiliBiliLiveConfig = Field(
        BiliBiliLiveConfig(), alias="bilibili_live"
    )
    youtube_live: YouTubeLiveConfig = Field(
        YouTubeLiveConfig(), alias="youtube_live"
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "bilibili_live": Description(
            en="Configuration for BiliBili Live platform", zh="B站直播平台配置"
        ),
        "youtube_live": Description(
            en="Configuration for YouTube Live chat integration",
            zh="YouTube 直播聊天集成配置",
        ),
    }

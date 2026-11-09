# SPDX-License-Identifier: MPL-2.0
"""Sample plugins for the UI preview; nothing here is loaded, executed or written.

The dataclasses sketch what a manifest would declare. Replace mock_plugins() and
the mock_*candidate() functions with real discovery when the plugin runtime exists;
no GitHub request is made here. Real plugins
would supply their own localized text; samples use tr() so the preview follows
the interface language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .i18n import tr

MOCK_GESTURES = ("snap", "double_tap", "swipe_left", "swipe_right", "wave", "fist")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    title: str
    kind: str  # switch | choice | text | secret | number
    default: object
    help: str = ""
    options: tuple = ()  # (value, label) pairs for choice.
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class Action:
    key: str
    title: str


@dataclass(frozen=True)
class Permission:
    title: str
    detail: str
    sensitive: bool = False


@dataclass(frozen=True)
class PluginEvent:
    when: str
    gesture: str
    action: str
    error: str = ""


@dataclass
class StorageSlot:
    kind: str  # data | cache | settings | log
    title: str
    path: Path
    size: int
    is_directory: bool = True


@dataclass(frozen=True)
class Release:
    version: str
    commit: str
    notes: str
    new_permissions: tuple = ()


@dataclass
class GitHubSource:
    repo: str  # owner/name
    commit: str
    last_checked: str
    channel: str = "stable"  # stable | prerelease
    auto_update: bool = True
    update: Release | None = None


@dataclass
class Plugin:
    id: str
    name: str
    version: str
    author: str
    description: str
    settings: tuple = ()
    actions: tuple = ()
    permissions: tuple = ()
    bindings: list = field(default_factory=list)  # (gesture, action key)
    events: tuple = ()
    storage: list = field(default_factory=list)
    enabled: bool = False
    error: str = ""
    source: str = ""  # Local folder, or the repository URL when github is set.
    github: GitHubSource | None = None
    values: dict = field(default_factory=dict)

    def __post_init__(self):
        self.values = {spec.key: spec.default for spec in self.settings} | self.values

    def action_title(self, key):
        return next((action.title for action in self.actions if action.key == key), key)

    @property
    def data_size(self):
        return sum(slot.size for slot in self.storage)


def assigned_storage(root, plugin_id, *, data=0, cache=0, settings=0, log=0):
    """What Studio would hand each plugin: paths it owns, isolated by plugin id."""
    base = Path(root) / plugin_id
    return [StorageSlot("data", tr("数据目录"), base / "data", data),
            StorageSlot("cache", tr("缓存目录"), base / "cache", cache),
            StorageSlot("settings", tr("设置文件"), base / "settings.json", settings, False),
            StorageSlot("log", tr("日志文件"), base / "plugin.log", log, False)]


def github_plugin_id(repo):
    return "github." + repo.replace("/", ".").lower()


def _github(repo, commit, **values):
    return dict(source=f"https://github.com/{repo}",
                github=GitHubSource(repo, commit, tr("今天 {time}", time="09:12"), **values))


_GESTURE_EVENTS = Permission(tr("接收手势事件"), tr("手势名称和时间戳，不含原始 IMU 数据。"))


def mock_plugins(root):
    media = "example.media-control"
    webhook = "example.webhook"
    slides = "example.slides"
    return [
        Plugin(
            id=media, name=tr("媒体控制"), version="1.2.0", author=tr("OpenZilo 示例"),
            description=tr("用手势控制系统媒体的播放、切歌和音量。"), enabled=True,
            **_github("example-user/media-control", "a1b2c3d", auto_update=False,
                      update=Release("1.3.0", "9e8f7a6", tr("修复切歌时偶尔重复触发的问题；新增静音动作。"))),
            settings=(
                SettingSpec("target", tr("控制目标"), "choice", "system", tr("手势触发时发送到哪个播放器。"),
                            options=(("system", tr("系统当前会话")), ("browser", tr("浏览器")))),
                SettingSpec("volume_step", tr("音量步长"), "number", "5", tr("每次手势调整的百分比。"),
                            minimum=1, maximum=20),
                SettingSpec("notify", tr("显示系统通知"), "switch", True),
            ),
            actions=(Action("toggle", tr("播放 / 暂停")), Action("next", tr("下一首")),
                     Action("previous", tr("上一首")), Action("volume_up", tr("增大音量"))),
            permissions=(_GESTURE_EVENTS, Permission(tr("控制媒体播放"), tr("向操作系统发送媒体按键。"))),
            bindings=[("snap", "toggle"), ("swipe_right", "next"), ("swipe_left", "previous")],
            events=(PluginEvent("14:02:11", "snap", tr("播放 / 暂停")),
                    PluginEvent("14:01:47", "swipe_right", tr("下一首")),
                    PluginEvent("13:58:02", "swipe_left", tr("上一首"), tr("当前没有媒体会话"))),
            storage=assigned_storage(root, media, data=48_128, cache=1_310_720, settings=212, log=20_480),
        ),
        Plugin(
            id=webhook, name=tr("Webhook 转发"), version="0.4.1", author=tr("OpenZilo 示例"),
            description=tr("把识别到的手势发送到你配置的 HTTP 地址，用于连接自动化服务。"),
            **_github("example-user/gesture-webhook", "4d5e6f0",
                      update=Release("0.5.0", "c0d1e2f", tr("支持把事件写入本地队列，网络恢复后补发。"),
                                     (Permission(tr("后台运行"), tr("Studio 窗口关闭时继续补发事件。"), True),))),
            settings=(
                SettingSpec("url", tr("目标地址"), "text", "https://example.com/hook",
                            tr("只有识别结果会发送到这个地址。")),
                SettingSpec("token", tr("访问令牌"), "secret", "", tr("作为 Bearer 令牌发送。")),
                SettingSpec("timeout", tr("超时（秒）"), "number", "5", minimum=1, maximum=60),
                SettingSpec("retry", tr("失败时重试一次"), "switch", False),
            ),
            actions=(Action("post", tr("发送 POST 请求")),),
            permissions=(_GESTURE_EVENTS, Permission(tr("访问网络"), tr("向你配置的地址发送数据。"), True)),
            bindings=[("wave", "post")],
            storage=assigned_storage(root, webhook, settings=96),
        ),
        Plugin(
            id=slides, name=tr("演示翻页"), version="2.0.0", author=tr("OpenZilo 示例"),
            description=tr("演示时用手势翻到上一页或下一页。"),
            source=str(Path.home() / "dev" / "slides-plugin"),
            error=tr("清单声明的入口文件 main.py 不存在。"),
            settings=(SettingSpec("loop", tr("末页后回到首页"), "switch", False),),
            actions=(Action("next_slide", tr("下一页")), Action("previous_slide", tr("上一页"))),
            permissions=(_GESTURE_EVENTS, Permission(tr("模拟键盘输入"), tr("向当前窗口发送翻页按键。"), True)),
            bindings=[("swipe_right", "next_slide")],
            storage=assigned_storage(root, slides, log=4_096),
        ),
    ]


def mock_candidate(root, source):
    """The sample manifest shown for any chosen folder; the folder is not read."""
    plugin_id = "example.focus-timer"
    return Plugin(
        id=plugin_id, name=tr("专注计时"), version="0.1.0", author=tr("OpenZilo 示例"),
        description=tr("用手势开始或结束一段专注计时，并把记录保存到自己的数据目录。"), source=str(source),
        settings=(SettingSpec("minutes", tr("默认时长（分钟）"), "number", "25", minimum=1, maximum=180),),
        actions=(Action("start", tr("开始计时")), Action("stop", tr("结束计时"))),
        permissions=(_GESTURE_EVENTS,),
        storage=assigned_storage(root, plugin_id),
    )


def mock_github_candidate(root, repo):
    """The sample manifest shown for any repository; GitHub is not contacted."""
    plugin_id = github_plugin_id(repo)
    return Plugin(
        id=plugin_id, name=tr("专注计时"), version="0.1.0", author=repo.split("/")[0],
        description=tr("用手势开始或结束一段专注计时，并把记录保存到自己的数据目录。"),
        **_github(repo, "f00c1a7"),
        settings=(SettingSpec("minutes", tr("默认时长（分钟）"), "number", "25", minimum=1, maximum=180),),
        actions=(Action("start", tr("开始计时")), Action("stop", tr("结束计时"))),
        permissions=(_GESTURE_EVENTS,),
        storage=assigned_storage(root, plugin_id),
    )

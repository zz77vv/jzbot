"""NewsEmoji custom emoji IDs and HTML rendering helpers for LedgerFlow."""

from __future__ import annotations

DIAMOND = ("5427168083074628963", "💎")
GREEN = ("5416081784641168838", "🟢")
RED = ("5411225014148014586", "🔴")
UP = ("5244837092042750681", "📈")
DOWN = ("5246762912428603768", "📉")
CASH = ("5409048419211682843", "💵")
CALENDAR = ("5413879192267805083", "🗓")
TAG = ("5222444124698853913", "🔖")
SEARCH = ("5231012545799666522", "🔍")
CHART = ("5231200819986047254", "📊")
ANNOUNCE = ("5424818078833715060", "📣")
SETTINGS = ("5341715473882955310", "⚙️")
REFRESH = ("5375338737028841420", "🔄")
EDIT = ("5395444784611480792", "✏️")
TRASH = ("5445267414562389170", "🗑")
PIN_LOC = ("5391032818111363540", "📍")
PIN_MSG = ("5397782960512444700", "📌")
SHIELD = ("5251203410396458957", "🛡")
MAIL = ("5253742260054409879", "✉️")
HOME = ("5416041192905265756", "🏠")
OK = ("5206607081334906820", "✔️")
NO = ("5210952531676504517", "❌")
PLAY = ("5348125953090403204", "▶️")
PAUSE = ("5359543311897998264", "⏸")
GOLD = ("5440539497383087970", "🥇")
SILVER = ("5447203607294265305", "🥈")
BRONZE = ("5453902265922376865", "🥉")
BOLT = ("5456140674028019486", "⚡️")


def tg_emoji(emoji: tuple[str, str] | str, fallback: str | None = None) -> str:
    """Render a Telegram custom emoji HTML tag with standard emoji fallback."""
    if isinstance(emoji, tuple):
        eid, fb = emoji
    else:
        eid, fb = emoji, fallback or "✨"
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'

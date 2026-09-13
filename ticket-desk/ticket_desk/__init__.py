"""Ticket Desk · 给「一个人 + 一群 AI 窗口」用的工单台。"""

from .model import SLOTS, TASK_TIERS, TICKET_TYPES, TicketError
from .store import TicketStore

# 发布版本号。仓根 CHANGELOG.md 是它的真源，改这里必须同时改那里。
# ★这**不是**通道协议版本（那个是 channel.PROTOCOL_VERSION，管的是客户端与服务端
#   互相认不认对方的参数，与发布节奏无关）。两个号不要互相对齐，也不要合成一个。
__version__ = "1.02"

__all__ = ["SLOTS", "TASK_TIERS", "TICKET_TYPES", "TicketError", "TicketStore", "__version__"]

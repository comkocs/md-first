"""Ticket Desk · 给「一个人 + 一群 AI 窗口」用的工单台。"""

from .model import SLOTS, TASK_TIERS, TICKET_TYPES, TicketError
from .store import TicketStore

__all__ = ["SLOTS", "TASK_TIERS", "TICKET_TYPES", "TicketError", "TicketStore"]

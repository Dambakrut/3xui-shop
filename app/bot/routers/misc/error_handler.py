"""Minimal Telegram diagnostics: never serialize updates or exception payloads."""
import logging
from html import escape

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent

from app.bot.models import ServicesContainer
from app.config import Config

logger = logging.getLogger(__name__)
router = Router(name=__name__)


def diagnostic_summary(event: ErrorEvent) -> str:
    update = event.update
    try:
        update_type = update.event_type
        telegram_event = update.event
    except Exception:
        update_type, telegram_event = "unknown", None
    user = getattr(telegram_event, "from_user", None)
    chat = getattr(telegram_event, "chat", None)
    if chat is None:
        chat = getattr(getattr(telegram_event, "message", None), "chat", None)
    return (f"update_id={update.update_id} type={update_type} "
            f"user_id={getattr(user, 'id', None)} chat_id={getattr(chat, 'id', None)} "
            f"exception_type={type(event.exception).__name__} details=redacted")


@router.errors(ExceptionTypeFilter(Exception))
async def errors_handler(event: ErrorEvent, config: Config, services: ServicesContainer) -> bool:
    summary = diagnostic_summary(event)
    if isinstance(event.exception, (TelegramForbiddenError, TelegramBadRequest)):
        logger.warning("Telegram request rejected: %s", summary)
        return True

    # Preserve original stack frames, never the original exception string,
    # chained exception payloads, locals, or Update repr. A library exception
    # can contain payment data even when its type/message looks harmless.
    sanitized = RuntimeError("Exception details redacted")
    logger.error("Telegram handler failed: %s", summary,
                 exc_info=(RuntimeError, sanitized, event.exception.__traceback__))
    if config.bot.DEV_ID:
        try:
            await services.notification.notify_developer(text=escape(summary))
        except Exception:
            logger.error("Could not send sanitized error notification")
    return True

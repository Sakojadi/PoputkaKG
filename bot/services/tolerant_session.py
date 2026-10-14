"""aiohttp session that survives Telegram updates aiogram cannot parse.

Telegram adds new object types faster than aiogram ships models for them. When
one such update appears in a getUpdates batch, aiogram raises ClientDecodeError
before the offset moves, so the same batch is re-fetched forever and the bot
stops receiving anything. Here an unparseable update is replaced by a bare
``Update(update_id=...)``: the offset still advances and the dispatcher ignores
it as an unknown update type.
"""

import logging
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import ClientDecodeError
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import Update
from pydantic import ValidationError

logger = logging.getLogger(__name__)


class TolerantSession(AiohttpSession):
    def check_response(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        status_code: int,
        content: str,
    ) -> Response[TelegramType]:
        try:
            return super().check_response(bot, method, status_code, content)
        except ClientDecodeError as e:
            if not isinstance(method, GetUpdates):
                raise
            updates = self._salvage_updates(bot, e.data)
            if updates is None:
                raise
            return Response[list[Update]](ok=True, result=updates)  # type: ignore[return-value]

    @staticmethod
    def _salvage_updates(bot: Bot, data: Any) -> list[Update] | None:
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            return None
        updates: list[Update] = []
        for raw in data["result"]:
            if not isinstance(raw, dict) or "update_id" not in raw:
                return None
            try:
                updates.append(Update.model_validate(raw, context={"bot": bot}))
            except ValidationError as err:
                logger.error(
                    "Skipping update %s that aiogram cannot parse: %s",
                    raw["update_id"],
                    err.errors(include_url=False, include_input=False)[:3],
                )
                updates.append(Update(update_id=raw["update_id"]))
        return updates

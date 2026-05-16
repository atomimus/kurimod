import asyncio
import logging
from inspect import iscoroutinefunction, unwrap
from typing import Optional, Callable, Dict, List, Union

import pyrogram
from pyrogram.filters import Filter

from ..config import config
from ..exceptions import ListenerTimeout, ListenerStopped
from ..types import ListenerTypes, Identifier, Listener
from ..utils import should_patch, patch_into


log = logging.getLogger(__name__)


def is_async_callable(callback: Callable) -> bool:
    call = getattr(callback, "__call__", None)

    return (
        iscoroutinefunction(callback)
        or iscoroutinefunction(unwrap(callback))
        or (
            call is not None
            and (
                iscoroutinefunction(call)
                or iscoroutinefunction(unwrap(call))
            )
        )
    )


class ListenerAwareQueue:
    def __init__(self, client, queue: asyncio.Queue):
        self.client = client
        self.input_queue = asyncio.Queue()
        self.output_queue = queue
        self.pump_task = None

    def __getattr__(self, name):
        return getattr(self.output_queue, name)

    def put_nowait(self, packet):
        if packet is None:
            self.input_queue.put_nowait(packet)
            self.ensure_pump()
            return

        has_backlog = self.pump_task is not None and not self.pump_task.done()
        if not has_backlog and not self.client.has_listeners():
            self.output_queue.put_nowait(packet)
            return

        self.input_queue.put_nowait(packet)
        self.ensure_pump()

    async def put(self, packet):
        self.put_nowait(packet)

    async def get(self):
        return await self.output_queue.get()

    def empty(self):
        return self.input_queue.empty() and self.output_queue.empty()

    def qsize(self):
        return self.input_queue.qsize() + self.output_queue.qsize()

    def ensure_pump(self):
        if self.pump_task is None or self.pump_task.done():
            self.pump_task = self.client.loop.create_task(self.pump())

    async def pump(self):
        try:
            while True:
                packet = await self.input_queue.get()

                if packet is None:
                    self.output_queue.put_nowait(None)
                else:
                    consumed = False
                    try:
                        consumed = await self.client.resolve_listener_from_packet(
                            packet
                        )
                    except Exception:
                        log.exception(
                            "Failed to resolve kurimod listener before dispatch"
                        )

                    if not consumed:
                        self.output_queue.put_nowait(packet)

                if self.input_queue.empty():
                    break
        finally:
            self.pump_task = None
            if not self.input_queue.empty():
                self.ensure_pump()


@patch_into(pyrogram.client.Client)
class Client(pyrogram.client.Client):
    listeners: Dict[ListenerTypes, List[Listener]]
    old__init__: Callable

    @should_patch()
    def __init__(self, *args, **kwargs):
        self.listeners = {listener_type: [] for listener_type in ListenerTypes}
        self.kurimod_handler_tasks = set()
        self.old__init__(*args, **kwargs)
        self.dispatcher.updates_queue = ListenerAwareQueue(
            self, self.dispatcher.updates_queue
        )

    @should_patch()
    async def listen(
        self,
        filters: Optional[Filter] = None,
        listener_type: ListenerTypes = ListenerTypes.MESSAGE,
        timeout: Optional[int] = None,
        unallowed_click_alert: bool = True,
        chat_id: Union[Union[int, str], List[Union[int, str]]] = None,
        user_id: Union[Union[int, str], List[Union[int, str]]] = None,
        message_id: Union[int, List[int]] = None,
        inline_message_id: Union[str, List[str]] = None,
    ):
        pattern = Identifier(
            from_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

        loop = asyncio.get_event_loop()
        future = loop.create_future()

        listener = Listener(
            future=future,
            filters=filters,
            unallowed_click_alert=unallowed_click_alert,
            identifier=pattern,
            listener_type=listener_type,
        )

        future.add_done_callback(lambda _future: self.remove_listener(listener))

        self.listeners[listener_type].append(listener)

        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.exceptions.TimeoutError:
            if callable(config.timeout_handler):
                if iscoroutinefunction(config.timeout_handler.__call__):
                    await config.timeout_handler(pattern, listener, timeout)
                else:
                    await self.loop.run_in_executor(
                        None, config.timeout_handler, pattern, listener, timeout
                    )
            elif config.throw_exceptions:
                raise ListenerTimeout(timeout)

    @should_patch()
    async def ask(
        self,
        chat_id: Union[Union[int, str], List[Union[int, str]]],
        text: str,
        filters: Optional[Filter] = None,
        listener_type: ListenerTypes = ListenerTypes.MESSAGE,
        timeout: Optional[int] = None,
        unallowed_click_alert: bool = True,
        user_id: Union[Union[int, str], List[Union[int, str]]] = None,
        message_id: Union[int, List[int]] = None,
        inline_message_id: Union[str, List[str]] = None,
        *args,
        **kwargs,
    ):
        sent_message = None
        if text.strip() != "":
            chat_to_ask = chat_id[0] if isinstance(chat_id, list) else chat_id
            sent_message = await self.send_message(chat_to_ask, text, *args, **kwargs)

        response = await self.listen(
            filters=filters,
            listener_type=listener_type,
            timeout=timeout,
            unallowed_click_alert=unallowed_click_alert,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )
        if response:
            response.sent_message = sent_message

        return response

    @should_patch()
    def remove_listener(self, listener: Listener):
        try:
            self.listeners[listener.listener_type].remove(listener)
        except ValueError:
            pass

    @should_patch()
    def schedule_handler_callback(self, callback: Callable, *args):
        async def runner():
            try:
                if is_async_callable(callback):
                    await callback(self, *args)
                else:
                    await self.loop.run_in_executor(
                        self.executor, callback, self, *args
                    )
            except (pyrogram.StopPropagation, pyrogram.ContinuePropagation):
                pass
            except Exception:
                log.exception("Unhandled exception in kurimod handler callback")

        task = self.loop.create_task(runner())
        self.kurimod_handler_tasks.add(task)
        task.add_done_callback(self.kurimod_handler_tasks.discard)
        return task

    @should_patch()
    def has_listeners(self) -> bool:
        return any(self.listeners[listener_type] for listener_type in ListenerTypes)

    @should_patch()
    def compose_message_identifier(self, message) -> Identifier:
        from_user = message.from_user
        from_user_id = from_user.id if from_user else None
        from_user_username = from_user.username if from_user else None
        message_id = getattr(message, "id", getattr(message, "message_id", None))
        chat = message.chat
        chat_id = [chat.id, chat.username] if chat else None

        return Identifier(
            message_id=message_id,
            chat_id=chat_id,
            from_user_id=[from_user_id, from_user_username],
        )

    @should_patch()
    def compose_callback_query_identifier(self, query) -> Identifier:
        from_user = query.from_user
        from_user_id = from_user.id if from_user else None
        from_user_username = from_user.username if from_user else None

        chat_id = None
        message_id = None

        if query.message:
            message_id = getattr(
                query.message, "id", getattr(query.message, "message_id", None)
            )

            if query.message.chat:
                chat_id = [query.message.chat.id, query.message.chat.username]

        return Identifier(
            message_id=message_id,
            chat_id=chat_id,
            from_user_id=[from_user_id, from_user_username],
            inline_message_id=query.inline_message_id,
        )

    @should_patch()
    async def listener_filter_matches(self, listener: Listener, update) -> bool:
        filters = listener.filters

        if callable(filters):
            if is_async_callable(filters):
                return await filters(self, update)

            return await self.loop.run_in_executor(None, filters, self, update)

        return True

    @should_patch()
    async def resolve_listener_from_packet(self, packet) -> bool:
        if not self.has_listeners():
            return False

        update, users, chats = packet
        parser = self.dispatcher.update_parsers.get(type(update))

        if parser is None:
            return False

        parsed_update, handler_type = await parser(update, users, chats)
        if parsed_update is None:
            return False

        if handler_type is pyrogram.handlers.message_handler.MessageHandler:
            return await self.resolve_listener_update(
                parsed_update, ListenerTypes.MESSAGE
            )

        if (
            handler_type
            is pyrogram.handlers.callback_query_handler.CallbackQueryHandler
        ):
            return await self.resolve_listener_update(
                parsed_update, ListenerTypes.CALLBACK_QUERY
            )

        return False

    @should_patch()
    async def resolve_listener_update(
        self, update, listener_type: ListenerTypes
    ) -> bool:
        if listener_type == ListenerTypes.MESSAGE:
            identifier = self.compose_message_identifier(update)
        elif listener_type == ListenerTypes.CALLBACK_QUERY:
            identifier = self.compose_callback_query_identifier(update)
        else:
            return False

        listener = self.get_listener_matching_with_data(identifier, listener_type)
        if not listener:
            return False

        if not await self.listener_filter_matches(listener, update):
            return False

        self.remove_listener(listener)

        if listener.future:
            if listener.future.done():
                return False

            listener.future.set_result(update)
            return True

        if listener.callback:
            self.schedule_handler_callback(listener.callback, update)
            return True

        raise ValueError("Listener must have either a future or a callback")

    @should_patch()
    def get_listener_matching_with_data(
        self, data: Identifier, listener_type: ListenerTypes
    ) -> Optional[Listener]:
        matching = []
        for listener in self.listeners[listener_type]:
            if listener.identifier.matches(data):
                matching.append(listener)

        # in case of multiple matching listeners, the most specific should be returned
        def count_populated_attributes(listener_item: Listener):
            return listener_item.identifier.count_populated()

        return max(matching, key=count_populated_attributes, default=None)

    def get_listener_matching_with_identifier_pattern(
        self, pattern: Identifier, listener_type: ListenerTypes
    ) -> Optional[Listener]:
        matching = []
        for listener in self.listeners[listener_type]:
            if pattern.matches(listener.identifier):
                matching.append(listener)

        # in case of multiple matching listeners, the most specific should be returned

        def count_populated_attributes(listener_item: Listener):
            return listener_item.identifier.count_populated()

        return max(matching, key=count_populated_attributes, default=None)

    @should_patch()
    def get_many_listeners_matching_with_data(
        self,
        data: Identifier,
        listener_type: ListenerTypes,
    ) -> List[Listener]:
        listeners = []
        for listener in self.listeners[listener_type]:
            if listener.identifier.matches(data):
                listeners.append(listener)
        return listeners

    @should_patch()
    def get_many_listeners_matching_with_identifier_pattern(
        self,
        pattern: Identifier,
        listener_type: ListenerTypes,
    ) -> List[Listener]:
        listeners = []
        for listener in self.listeners[listener_type]:
            if pattern.matches(listener.identifier):
                listeners.append(listener)
        return listeners

    @should_patch()
    async def stop_listening(
        self,
        listener_type: ListenerTypes = ListenerTypes.MESSAGE,
        chat_id: Union[Union[int, str], List[Union[int, str]]] = None,
        user_id: Union[Union[int, str], List[Union[int, str]]] = None,
        message_id: Union[int, List[int]] = None,
        inline_message_id: Union[str, List[str]] = None,
    ):
        pattern = Identifier(
            from_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )
        listeners = self.get_many_listeners_matching_with_identifier_pattern(pattern, listener_type)

        for listener in listeners:
            await self.stop_listener(listener)

    @should_patch()
    async def stop_listener(self, listener: Listener):
        self.remove_listener(listener)

        if listener.future is None or listener.future.done():
            return

        if callable(config.stopped_handler):
            if iscoroutinefunction(config.stopped_handler.__call__):
                await config.stopped_handler(None, listener)
            else:
                await self.loop.run_in_executor(
                    None, config.stopped_handler, None, listener
                )
        elif config.throw_exceptions:
            listener.future.set_exception(ListenerStopped())

    @should_patch()
    def register_next_step_handler(
        self,
        callback: Callable,
        filters: Optional[Filter] = None,
        listener_type: ListenerTypes = ListenerTypes.MESSAGE,
        unallowed_click_alert: bool = True,
        chat_id: Union[Union[int, str], List[Union[int, str]]] = None,
        user_id: Union[Union[int, str], List[Union[int, str]]] = None,
        message_id: Union[int, List[int]] = None,
        inline_message_id: Union[str, List[str]] = None,
    ):
        pattern = Identifier(
            from_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

        listener = Listener(
            callback=callback,
            filters=filters,
            unallowed_click_alert=unallowed_click_alert,
            identifier=pattern,
            listener_type=listener_type,
        )

        self.listeners[listener_type].append(listener)

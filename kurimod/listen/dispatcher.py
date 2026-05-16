import asyncio
import inspect
import logging

import pyrogram
import pyrogram.dispatcher
from pyrogram.handlers import ErrorHandler, RawUpdateHandler

from ..utils import patch_into, should_patch


log = logging.getLogger(__name__)


def is_async_callback(callback):
    call = getattr(callback, "__call__", None)

    return (
        inspect.iscoroutinefunction(callback)
        or inspect.iscoroutinefunction(inspect.unwrap(callback))
        or (
            call is not None
            and (
                inspect.iscoroutinefunction(call)
                or inspect.iscoroutinefunction(inspect.unwrap(call))
            )
        )
    )


@patch_into(pyrogram.dispatcher.Dispatcher)
class Dispatcher(pyrogram.dispatcher.Dispatcher):
    @should_patch()
    async def handler_worker(self, lock):
        while True:
            packet = await self.updates_queue.get()

            if packet is None:
                break

            # Keep queue workers free while a handler waits on listen()/ask().
            task = self.client.loop.create_task(
                self.dispatch_update_packet(packet, lock)
            )
            tasks = getattr(self.client, "kurimod_handler_tasks", None)
            if tasks is not None:
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    @should_patch()
    async def stop(self, clear_handlers: bool = True):
        await self.oldstop(clear_handlers=clear_handlers)

        tasks = getattr(self.client, "kurimod_handler_tasks", None)
        if not tasks:
            return

        current_task = asyncio.current_task()
        pending = [
            task
            for task in list(tasks)
            if task is not current_task and not task.done()
        ]
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for task in pending:
            tasks.discard(task)

    @should_patch()
    async def dispatch_update_packet(self, packet, lock):
        try:
            update, users, chats = packet
            parser = self.update_parsers.get(type(update), None)

            parsed_update, handler_type = (
                await parser(update, users, chats)
                if parser is not None
                else (None, type(None))
            )

            # Snapshot handlers under the dispatcher lock, then run callbacks outside
            # it so long conversations do not block later updates.
            async with lock:
                groups = [list(group) for group in self.groups.values()]

            for group in groups:
                for handler in group:
                    if isinstance(handler, ErrorHandler):
                        continue

                    args = None

                    if isinstance(handler, handler_type):
                        try:
                            if await handler.check(self.client, parsed_update):
                                args = (parsed_update,)
                        except Exception as e:
                            log.exception(e)
                            continue

                    elif isinstance(handler, RawUpdateHandler):
                        try:
                            if await handler.check(self.client, update):
                                args = (update, users, chats)
                        except Exception as e:
                            log.exception(e)
                            continue

                    if args is None:
                        continue

                    try:
                        if is_async_callback(handler.callback):
                            await handler.callback(self.client, *args)
                        else:
                            await self.client.loop.run_in_executor(
                                self.client.executor,
                                handler.callback,
                                self.client,
                                *args,
                            )
                    except pyrogram.StopPropagation:
                        raise
                    except pyrogram.ContinuePropagation:
                        continue
                    except Exception as exc:
                        await self.handle_update_handler_exception(
                            exc, handler, update, users, chats
                        )

                    break
        except pyrogram.StopPropagation:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(e)

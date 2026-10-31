"""One task owns a stdio/session context from entry through exit.

AnyIO cancel scopes must not be entered in one task and exited in another.
Callers only exchange requests with the owner; no subprocess is shared across users.
"""
import asyncio
from contextlib import AsyncExitStack


class OwnedSession:
    def __init__(self, transport_factory, session_factory, timeout, init_timeout=None):
        self._transport = transport_factory
        self._session = session_factory
        self.timeout = timeout
        self.init_timeout = timeout if init_timeout is None else init_timeout
        self.ready = asyncio.get_running_loop().create_future()
        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        pending = None
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.init_timeout):
                    read, write = await stack.enter_async_context(self._transport())
                    session = await stack.enter_async_context(self._session(read, write))
                    await session.initialize()
                self.ready.set_result(self)
                while True:
                    method, args, pending = await self.queue.get()
                    if pending.cancelled():
                        pending = None
                        continue
                    try:
                        async with asyncio.timeout(self.timeout):
                            value = await getattr(session, method)(*args)
                        if not pending.done():
                            pending.set_result(value)
                    except BaseException as exc:
                        if not pending.done():
                            pending.set_exception(RuntimeError("MCP owner closed")
                                                  if isinstance(exc, asyncio.CancelledError) else exc)
                        raise
                    finally:
                        pending = None
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            if pending is not None and not pending.done():
                pending.set_exception(RuntimeError("MCP owner closed"))
            # Surface cleanup errors to the closing owner without logging payloads.
            if not isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError("MCP owner closed"))

    async def request(self, method, *args):
        if self.task.done():
            raise RuntimeError("MCP connection unavailable")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((method, args, future))
        try:
            return await future
        except asyncio.CancelledError:
            # Session owner/async context closes the transport on cancellation.
            future.cancel()
            raise

    async def list_tools(self):
        return await self.request("list_tools")

    async def call_tool(self, name, arguments):
        return await self.request("call_tool", name, arguments)

    async def aclose(self):
        if not self.task.done():
            self.task.cancel()
        try:
            await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if not self.task.done():
                raise
        finally:
            # Retrieve the readiness exception even when the caller was cancelled.
            if self.ready.done() and not self.ready.cancelled():
                self.ready.exception()

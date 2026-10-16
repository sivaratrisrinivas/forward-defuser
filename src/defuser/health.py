"""Minimal HTTP health endpoint for the worker (Cloud Run / Fly need a listening port)."""

import asyncio
import os


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await asyncio.wait_for(reader.readline(), timeout=2)
    except TimeoutError:
        pass
    body = b"ok"
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\n" + body)
    await writer.drain()
    writer.close()


async def start_worker_health_server() -> asyncio.Server | None:
    port = os.environ.get("PORT")
    if not port:
        return None
    return await asyncio.start_server(_handle, "0.0.0.0", int(port))

"""Job handlers. Importing this module registers them with the worker."""

import logging

from defuser.db import get_pool
from defuser.jobs.queue import Job
from defuser.jobs.worker import handler

log = logging.getLogger(__name__)


@handler("process_inbound")
async def process_inbound(payload: dict, job: Job) -> None:
    from defuser.pipeline import process_inbound_message

    await process_inbound_message(await get_pool(), payload["inbound_id"])

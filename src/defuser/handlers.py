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


@handler("recheck_claim")
async def recheck_claim(payload: dict, job: Job) -> None:
    from defuser import followup
    from defuser.pipeline import make_checker

    pool = await get_pool()
    outcome = await followup.recheck_claim(pool, payload["claim_id"], payload["hours"], make_checker(pool))
    log.info("recheck claim=%s hours=%s -> %s", payload["claim_id"], payload["hours"], outcome)


@handler("send_admin_alert")
async def send_admin_alert(payload: dict, job: Job) -> None:
    from defuser import followup

    await followup.send_admin_alert(await get_pool(), payload["key"])


@handler("maintenance")
async def maintenance(payload: dict, job: Job) -> None:
    from defuser import followup
    from defuser.config import get_settings
    from defuser.privacy_ops import apply_retention

    pool = await get_pool()
    out = await apply_retention(pool, get_settings().retention_days)
    log.info("retention applied: %s", out)
    await followup.schedule_maintenance(pool)

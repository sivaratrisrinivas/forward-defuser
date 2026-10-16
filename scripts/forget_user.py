"""Delete a person's data when they ask through a channel other than /forget.

uv run python scripts/forget_user.py telegram 123456789
uv run python scripts/forget_user.py whatsapp 919800000001
"""

import asyncio
import sys

from defuser.db import close_pool, get_pool
from defuser.privacy_ops import forget_user


async def main(channel: str, user_id: str) -> None:
    print(await forget_user(await get_pool(), channel, user_id))
    await close_pool()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("telegram", "whatsapp"):
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))

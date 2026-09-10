import asyncio

from adversarial_agent_mvp.worker import main


def run() -> None:
    asyncio.run(main())


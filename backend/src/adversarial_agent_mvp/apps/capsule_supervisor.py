import uvicorn

from adversarial_agent_mvp.settings import get_settings
from adversarial_agent_mvp.supervisor import create_supervisor_app


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_supervisor_app(settings),
        host=settings.capsule_supervisor_host,
        port=settings.capsule_supervisor_port,
    )

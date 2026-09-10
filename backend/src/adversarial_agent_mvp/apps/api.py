import uvicorn


def run() -> None:
    uvicorn.run("adversarial_agent_mvp.api:create_app", factory=True, host="0.0.0.0", port=8000)


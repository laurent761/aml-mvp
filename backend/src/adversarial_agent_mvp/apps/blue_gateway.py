import uvicorn


def run() -> None:
    uvicorn.run(
        "adversarial_agent_mvp.blue_gateway:create_blue_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
    )


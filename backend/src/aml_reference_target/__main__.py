import argparse
import os
from urllib.parse import urlsplit

import httpx
import uvicorn

from .agent import BlueTools, TargetAgent
from .models import BrokerCompletionModel, WiringFixtureModel
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic finance target")
    parser.add_argument("--mode", choices=["fixture", "model"], required=True)
    parser.add_argument("--model-url")
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.mode == "model":
        model_url = args.model_url or "http://blue:8080/v1/inference/"
        endpoint = urlsplit(model_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname != "blue"
            or endpoint.port != 8080
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path.rstrip("/") != "/v1/inference"
        ):
            parser.error("model mode requires an INF-02 inference route at http://blue:8080")
        model = BrokerCompletionModel(
            httpx.AsyncClient(base_url=model_url.rstrip("/") + "/", timeout=150, trust_env=False),
            os.environ["BLUE_EPISODE_ID"],
            os.environ["BLUE_INFERENCE_CAPABILITY"],
            args.model,
        )
    else:
        if args.model or args.model_url:
            parser.error("model settings cannot be used in fixture mode")
        model = WiringFixtureModel()
    blue = httpx.AsyncClient(base_url=os.environ["BLUE_GATEWAY_URL"], timeout=30, trust_env=False)
    tools = BlueTools(blue, os.environ["BLUE_EPISODE_ID"], os.environ["BLUE_CAPABILITY_TOKEN"])
    uvicorn.run(create_app(TargetAgent(model, tools)), host="0.0.0.0", port=8081)


if __name__ == "__main__":
    main()

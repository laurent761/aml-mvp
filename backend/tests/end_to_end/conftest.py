"""Network SDK harness: real API, leased worker, DB, gateway and target contracts.

Only container allocation and model inference are substituted. Tests never replace
API handlers, worker commands, persistence, artifact transfer, or SDK responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import secrets
import socket
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.blue import BlueEngine
from adversarial_agent_mvp.blue_gateway import create_blue_app
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.contracts import CapsuleHandle
from adversarial_agent_mvp.environment import AgentEnvironment
from adversarial_agent_mvp.models import HeuristicBaselineModel
from adversarial_agent_mvp.orchestrator import CampaignRunner
from adversarial_agent_mvp.policy import PolicyEngine
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.security import CapabilityTokenService
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Database, Repository
from adversarial_agent_mvp.target_adapter import HttpTargetAdapter
from adversarial_agent_mvp.verifier import DeterministicVerifier
from adversarial_agent_mvp.virtual_world import VirtualWorld
from adversarial_agent_mvp.worker import Worker
from aml_reference_target.agent import BlueTools, TargetAgent
from aml_reference_target.models import WiringFixtureModel
from aml_reference_target.server import create_app as create_target_app


class IsolatedSettings(Settings):
    """Explicit test inputs only; never inherit a developer's provider or service env."""

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


class ContractRuntime:
    """Run the production reference target and gateway without a Docker daemon."""

    def __init__(self, repository):
        self.repository = repository
        self.clients = {}
        self.transports = {}
        self.provisioned = []
        self.destroyed = []

    async def provision(self, episode_id, manifest):
        signing = secrets.token_urlsafe(32)
        supervisor = secrets.token_urlsafe(32)
        capability = CapabilityTokenService(signing).issue(
            episode_id, list(manifest.destination_routes)
        )
        bootstrap = httpx.AsyncClient(trust_env=False)
        agent = TargetAgent(
            WiringFixtureModel(), BlueTools(bootstrap, episode_id, capability)
        )
        gateway = create_blue_app(
            IsolatedSettings(capability_signing_key=signing, otel_enabled=False),
            supervisor_token=supervisor,
            target_transport=httpx.ASGITransport(app=create_target_app(agent)),
        )
        transport = httpx.ASGITransport(app=gateway)
        agent.tools.client = httpx.AsyncClient(
            base_url="http://blue:8080", transport=transport, trust_env=False
        )
        self.clients[episode_id] = [bootstrap, agent.tools.client]
        self.transports[episode_id] = transport
        self.provisioned.append(episode_id)
        return CapsuleHandle(
            capsule_id=episode_id,
            episode_id=episode_id,
            target_container_id="contract-target",
            network_id="contract-network",
            blue_alias="blue",
            gateway_ingress_url="http://blue:8080",
            supervisor_capability=supervisor,
        )

    async def healthcheck(self, handle):
        return handle.episode_id in self.clients

    async def destroy(self, handle):
        if handle and handle.episode_id in self.clients:
            for client in self.clients.pop(handle.episode_id):
                await client.aclose()
            self.transports.pop(handle.episode_id)
            self.destroyed.append(handle.episode_id)

    def environment(self, episode_id, manifest, policies, handle):
        client = httpx.AsyncClient(
            base_url="http://blue:8080",
            transport=self.transports[episode_id],
            trust_env=False,
        )
        self.clients[episode_id].append(client)
        adapter = HttpTargetAdapter(
            manifest,
            client,
            capsule_handle=handle,
            scenario_loader=ScenarioCatalog(self.repository).runtime,
            inference_recorder=lambda records: self.repository.record_target_inference(
                episode_id, records
            ),
        )
        return AgentEnvironment(
            episode_id,
            adapter,
            BlueEngine(PolicyEngine([], capsule_mode=True), VirtualWorld()),
            DeterministicVerifier(),
        )


class NetworkServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # A test server must not install process-wide signal handlers in pytest.
        yield


class ContractWorker(Worker):
    def __init__(self, settings, repository, runtime, store):
        self.runtime, self.store = runtime, store
        super().__init__(settings, repository)

    def _build_runner(self):
        return CampaignRunner(
            self.repository,
            HeuristicBaselineModel(),
            self.runtime.environment,
            self.runtime,
            artifact_store=self.store,
        )


class NetworkLab:
    def __init__(self, tmp_path, sdk):
        self.sdk = sdk
        self.repository = Repository(Database(f"sqlite:///{tmp_path / 'research.db'}"))
        self.repository.db.create_all()
        self.settings = IsolatedSettings(
            database_url=str(self.repository.db.engine.url),
            otel_enabled=False,
            research_auth_required=True,
            research_auth_tokens={
                hashlib.sha256(token.encode()).hexdigest(): {
                    "owner_id": token,
                    "scopes": ["research", "evaluation", "evidence"],
                }
                for token in ("alice", "bob")
            },
            artifact_root=tmp_path / "artifacts",
            research_upload_root=tmp_path / "uploads",
            worker_poll_seconds=0.01,
            worker_lease_seconds=30,
            max_worker_concurrency=2,
        )
        self.bundle = ScenarioCatalog(self.repository).register(
            reference_bundle("sha256:" + "a" * 64)
        )
        self.store = LocalArtifactStore(self.settings.artifact_root)
        self.runtime = ContractRuntime(self.repository)
        self.worker = ContractWorker(self.settings, self.repository, self.runtime, self.store)
        self.worker_task = None
        self.server_task = None
        self.api_database = None

    async def start_api(self):
        # A separate engine ensures the HTTP API reads durable records, not worker state.
        self.api_database = Database(self.settings.database_url)
        app = create_app(self.settings, database=self.api_database, artifact_store=self.store)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.base_url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = NetworkServer(uvicorn.Config(
            app, log_level="error", access_log=False, lifespan="on", ws="none"
        ))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                    raise AssertionError("API server exited before startup")
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=self.base_url, trust_env=False) as client:
            response = await client.get("/healthz")
            assert response.status_code == 200, response.text

    async def stop_api(self):
        try:
            if self.server_task:
                self.server.should_exit = True
                try:
                    await asyncio.wait_for(self.server_task, 10)
                finally:
                    self.server_task = None
                    self.socket.close()
        finally:
            if self.api_database:
                self.api_database.engine.dispose()
                self.api_database = None

    def start_worker(self):
        self.worker_task = asyncio.create_task(self.worker.run_forever())

    async def stop_worker(self):
        if self.worker_task:
            self.worker_task.cancel()
            result = (await asyncio.wait_for(
                asyncio.gather(self.worker_task, return_exceptions=True), 10
            ))[0]
            self.worker_task = None
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    def client(self, token="alice", **kwargs):
        return self.sdk.Client(self.base_url, token, operation_timeout=10, **kwargs)

    async def dataset(self, client, dataset_id):
        async with asyncio.timeout(10):
            while True:
                result = await client.dataset(dataset_id)
                if result["status"] != "pending":
                    assert result["status"] == "completed", result
                    return result
                assert self.worker_task is not None
                if self.worker_task.done():
                    await self.worker_task
                    raise AssertionError("worker exited before completing dataset")
                await asyncio.sleep(0.02)


@pytest.fixture
async def network_lab(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "sdk/src"))
    sdk = importlib.import_module("aml_research.client")
    lab = NetworkLab(tmp_path, sdk)
    try:
        await lab.start_api()
        lab.start_worker()
        yield lab
    finally:
        try:
            await lab.stop_worker()
        finally:
            try:
                await lab.stop_api()
            finally:
                lab.repository.db.engine.dispose()
        assert lab.runtime.clients == {}, "worker leaked a live target or gateway client"
        assert lab.runtime.transports == {}, "worker leaked a gateway transport"
        assert sorted(lab.runtime.destroyed) == sorted(lab.runtime.provisioned)

"""Montagem do sistema: único ponto que conhece todas as implementações concretas.

Execução local (a partir de ``src/``)::

    uvicorn app.main:criar_app --factory --reload
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.camera import criar_fonte
from app.adapters.database import (
    DynamoSpotRepository,
    InMemoryCameraRepository,
    InMemoryHistoryRepository,
    InMemoryParkingLotRepository,
    InMemorySessionRepository,
    InMemorySpotRepository,
    InMemoryUserRepository,
    PostgresCameraRepository,
    PostgresHistoryRepository,
    PostgresParkingLotRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    criar_tabelas,
)
from app.adapters.events import InMemoryEventPublisher
from app.adapters.occupancy import IntersectionOccupancyStrategy
from app.adapters.security import BcryptPasswordHasher
from app.api.routes import registrar_tratadores, router
from app.core.models import DadosInvalidos, Role
from app.core.ports import EventPublisher, OccupancyStrategy, VehicleDetector
from app.core.services import (
    AuthService,
    CameraManager,
    FrameProcessor,
    HistoryService,
    OccupancyService,
    ParkingLotService,
    ParkingSessionService,
    SpotCalibrationService,
)

logger = logging.getLogger(__name__)

_PESOS_PADRAO = Path(__file__).resolve().parents[1] / "model" / "car_only_pklot_carpk_acpds.pt"


class Settings(BaseSettings):
    """Configurações lidas de variáveis de ambiente com prefixo ``APP_`` (ou do ``.env``).

    Ex.: ``APP_MODO=aws``, ``APP_DATABASE_URL=postgresql+psycopg://...``.
    """

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    modo: Literal["local", "aws"] = "local"

    # Detecção e ocupação
    caminho_pesos: str = str(_PESOS_PADRAO)
    conf: float = 0.25
    imgsz: int = 1024
    device: str | None = None  # "0" (GPU), "cpu" ou None (automático)
    limiar_ocupacao: float = 0.4
    intervalo_captura_s: float = 1.0
    intervalo_snapshot_min: float = 5.0

    # Persistência (modo "aws")
    database_url: str | None = None
    dynamo_tabela: str = "vagas"
    aws_regiao: str | None = None
    dynamo_endpoint_url: str | None = None  # ex.: DynamoDB Local

    # Segurança
    jwt_segredo: str | None = None  # no modo local, gerado a cada execução se ausente
    jwt_expiracao_min: int = 480
    cookie_seguro: bool = False  # True em produção (HTTPS)
    cors_origens: list[str] = []  # origens do front-end, ex.: ["http://localhost:5173"]

    # Primeiro administrador (seed): criado na subida se ainda não existir
    admin_email: str | None = None
    admin_senha: str | None = None
    admin_nome: str = "Administrador"

    @model_validator(mode="after")
    def _validar(self) -> Settings:
        if self.modo == "aws":
            faltando = [n for n in ("database_url", "jwt_segredo") if not getattr(self, n)]
            if faltando:
                raise ValueError(f"Modo aws exige: {', '.join('APP_' + n.upper() for n in faltando)}.")
        if not self.jwt_segredo:
            self.jwt_segredo = secrets.token_urlsafe(32)
        return self


class Container:
    """Instancia os adaptadores, injeta-os nos serviços e expõe tudo para as rotas."""

    def __init__(self, config: Settings, detector: VehicleDetector | None = None) -> None:
        """``detector`` permite injetar um detector falso nos testes (sem GPU/YOLO)."""
        self.config = config

        # Adaptadores
        self.detector: VehicleDetector = detector or self._criar_detector()
        self.estrategia: OccupancyStrategy = IntersectionOccupancyStrategy(config.limiar_ocupacao)
        self._criar_repositorios()
        self.eventos: EventPublisher = InMemoryEventPublisher()
        self.hasher = BcryptPasswordHasher()

        # Serviços
        self.estacionamento_service = ParkingLotService(
            self.estacionamentos, self.usuarios, self.cameras
        )
        self.sessao_service = ParkingSessionService(self.sessoes, self.vagas)
        self.frame_processor = FrameProcessor(
            self.detector, self.estrategia, self.vagas, self.eventos, self.sessao_service
        )
        self.camera_manager = CameraManager(
            self.cameras, self.vagas, self.frame_processor, criar_fonte, config.intervalo_captura_s
        )
        self.calibracao = SpotCalibrationService(self.vagas, self.cameras)
        self.ocupacao = OccupancyService(self.vagas, self.estacionamentos)
        self.historico_service = HistoryService(self.vagas, self.estacionamentos, self.historico)
        self.auth = AuthService(self.usuarios, self.hasher)

        self._criar_admin_inicial()

    def _criar_detector(self) -> VehicleDetector:
        from app.adapters.yolo import YoloObbDetector  # importa o ultralytics só quando usado

        return YoloObbDetector(
            self.config.caminho_pesos, self.config.conf, self.config.imgsz, self.config.device
        )

    def _criar_repositorios(self) -> None:
        """Em memória no modo local; DynamoDB (vagas) e PostgreSQL (demais) no modo aws."""
        if self.config.modo == "local":
            self.estacionamentos = InMemoryParkingLotRepository()
            self.cameras = InMemoryCameraRepository()
            self.vagas = InMemorySpotRepository()
            self.historico = InMemoryHistoryRepository()
            self.sessoes = InMemorySessionRepository()
            self.usuarios = InMemoryUserRepository()
            return

        import sqlalchemy as sa

        engine = sa.create_engine(self.config.database_url, pool_pre_ping=True)
        criar_tabelas(engine)
        self.estacionamentos = PostgresParkingLotRepository(engine)
        self.cameras = PostgresCameraRepository(engine)
        self.vagas = DynamoSpotRepository(
            self.config.dynamo_tabela, self.config.aws_regiao, self.config.dynamo_endpoint_url
        )
        self.historico = PostgresHistoryRepository(engine)
        self.sessoes = PostgresSessionRepository(engine)
        self.usuarios = PostgresUserRepository(engine)

    def _criar_admin_inicial(self) -> None:
        """Seed do primeiro administrador (``APP_ADMIN_EMAIL`` e ``APP_ADMIN_SENHA``).

        A senha nunca é registrada no log; só o tamanho, para ajudar a detectar
        aspas ou espaços que vieram junto da variável de ambiente.
        """
        email, senha = self.config.admin_email, self.config.admin_senha
        faltando = [n for n, v in (("APP_ADMIN_EMAIL", email), ("APP_ADMIN_SENHA", senha)) if not v]
        if faltando:
            logger.warning(
                "Admin inicial não criado: %s não definida(s) no ambiente nem no .env.",
                ", ".join(faltando),
            )
            return
        existente = self.usuarios.buscar_por_email(email.strip().lower())
        if existente is not None:
            logger.info(
                "Admin inicial já existe: e-mail=%r papel=%s.", existente.email, existente.papel.value
            )
            return
        try:
            usuario = self.auth.cadastrar(self.config.admin_nome, email, senha, Role.ADMIN)
        except DadosInvalidos as erro:
            logger.error("Admin inicial não criado (e-mail=%r): %s", email, erro)
            return
        logger.info(
            "Admin inicial criado: e-mail=%r papel=%s senha=%d caracteres.",
            usuario.email,
            usuario.papel.value,
            len(senha),
        )


async def _agendar_snapshots(historico: HistoryService, intervalo_min: float) -> None:
    """Grava periodicamente a ocupação de cada estacionamento ativo."""
    while True:
        await asyncio.sleep(intervalo_min * 60)
        try:
            await asyncio.to_thread(historico.registrar_snapshots)
        except Exception:
            logger.exception("Falha ao registrar snapshots de ocupação.")


def criar_app(config: Settings | None = None, container: Container | None = None) -> FastAPI:
    """Cria o ``Container``, registra rotas e tratadores e controla o ciclo de vida.

    Na subida, inicia as câmeras ativas e o agendamento dos snapshots; ao
    desligar, encerra os laços de captura.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    container = container or Container(config or Settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.camera_manager.iniciar_todas()
        agendamento = asyncio.create_task(
            _agendar_snapshots(container.historico_service, container.config.intervalo_snapshot_min)
        )
        try:
            yield
        finally:
            agendamento.cancel()
            await container.camera_manager.parar_todas()

    app = FastAPI(title="Controle de estacionamento", lifespan=lifespan)
    app.state.container = container
    if container.config.cors_origens:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=container.config.cors_origens,
            allow_credentials=True,  # necessário para o cookie com o JWT
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(router)
    registrar_tratadores(app)

    @app.get("/saude", include_in_schema=False)
    def saude() -> dict:
        """Verificação de saúde (ex.: health check do ALB)."""
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:criar_app", factory=True, host="0.0.0.0", port=8000)

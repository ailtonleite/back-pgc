"""Interfaces (portas) do núcleo.

Definem apenas o que o núcleo precisa, não como é feito. As implementações
concretas ficam em ``adapters/`` e são conectadas em ``main.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, AsyncIterator

from app.core.models import (
    Camera,
    Detection,
    OccupancySnapshot,
    ParkingLot,
    ParkingSession,
    ParkingSpot,
    SpotChange,
    SpotStatus,
    User,
)

if TYPE_CHECKING:
    from numpy import ndarray  # frame BGR do OpenCV; só usado nas anotações


class VehicleDetector(ABC):
    @abstractmethod
    def detectar(self, frame: ndarray) -> list[Detection]:
        """Detecta veículos em um frame."""


class OccupancyStrategy(ABC):
    """Recebe todas as vagas de uma câmera de uma vez, para que cada detecção
    seja atribuída a uma única vaga."""

    @abstractmethod
    def classificar(
        self, vagas: list[ParkingSpot], deteccoes: list[Detection]
    ) -> dict[str, SpotStatus]:
        """Status calculado para cada vaga, indexado pelo id."""


class FrameSource(ABC):
    @abstractmethod
    def abrir(self) -> None:
        """Abre a conexão com a fonte."""

    @abstractmethod
    def ler_frame(self) -> ndarray | None:
        """Próximo frame; ``None`` se a fonte terminou ou falhou."""

    @abstractmethod
    def fechar(self) -> None:
        """Libera a conexão."""

    @abstractmethod
    def esta_aberta(self) -> bool:
        """Se a fonte está disponível."""


class ParkingLotRepository(ABC):
    @abstractmethod
    def listar(self, apenas_ativos: bool = False) -> list[ParkingLot]:
        """Estacionamentos."""

    @abstractmethod
    def listar_por_ids(self, ids: list[str]) -> list[ParkingLot]:
        """Estacionamentos de um admin."""

    @abstractmethod
    def buscar(self, estacionamento_id: str) -> ParkingLot | None:
        """Estacionamento pelo id."""

    @abstractmethod
    def salvar(self, estacionamento: ParkingLot) -> None:
        """Cria ou atualiza."""

    @abstractmethod
    def remover(self, estacionamento_id: str) -> None:
        """Remove."""


class CameraRepository(ABC):
    @abstractmethod
    def listar_por_estacionamento(self, estacionamento_id: str) -> list[Camera]:
        """Câmeras de um estacionamento."""

    @abstractmethod
    def listar_ativas(self) -> list[Camera]:
        """Câmeras em monitoramento (usado na subida da aplicação)."""

    @abstractmethod
    def buscar(self, camera_id: str) -> Camera | None:
        """Câmera pelo id."""

    @abstractmethod
    def salvar(self, camera: Camera) -> None:
        """Cria ou atualiza."""

    @abstractmethod
    def remover(self, camera_id: str) -> None:
        """Remove a câmera."""


class SpotRepository(ABC):
    """Estado atual das vagas (em produção: DynamoDB)."""

    @abstractmethod
    def listar_por_camera(self, camera_id: str) -> list[ParkingSpot]:
        """Vagas de uma câmera."""

    @abstractmethod
    def listar_por_estacionamento(self, estacionamento_id: str) -> list[ParkingSpot]:
        """Vagas de um estacionamento."""

    @abstractmethod
    def buscar(self, vaga_id: str) -> ParkingSpot | None:
        """Vaga pelo id."""

    @abstractmethod
    def salvar(self, vaga: ParkingSpot) -> None:
        """Cria ou atualiza uma vaga."""

    @abstractmethod
    def salvar_varias(self, vagas: list[ParkingSpot]) -> None:
        """Salva em lote (usado na demarcação)."""

    @abstractmethod
    def remover(self, vaga_id: str) -> None:
        """Remove uma vaga."""

    @abstractmethod
    def remover_por_camera(self, camera_id: str) -> None:
        """Remove todas as vagas de uma câmera."""


class HistoryRepository(ABC):
    """Histórico de ocupação (em produção: PostgreSQL/RDS)."""

    @abstractmethod
    def salvar_snapshot(self, snapshot: OccupancySnapshot) -> None:
        """Registra um snapshot de ocupação."""

    @abstractmethod
    def consultar_snapshots(
        self, estacionamento_id: str, inicio: datetime, fim: datetime
    ) -> list[OccupancySnapshot]:
        """Snapshots em um intervalo."""

    @abstractmethod
    def expurgar_anteriores_a(self, data: datetime) -> int:
        """Remove registros antigos; retorna a quantidade removida."""


class SessionRepository(ABC):
    """Registros de "onde estacionei" (em produção: PostgreSQL/RDS)."""

    @abstractmethod
    def salvar(self, sessao: ParkingSession) -> None:
        """Cria ou atualiza."""

    @abstractmethod
    def sessao_ativa(self, usuario_id: str) -> ParkingSession | None:
        """Sessão em aberto do usuário."""

    @abstractmethod
    def sessoes_ativas_na_vaga(self, vaga_id: str) -> list[ParkingSession]:
        """Sessões em aberto em uma vaga (para encerramento automático)."""

    @abstractmethod
    def expurgar_anteriores_a(self, data: datetime) -> int:
        """Remove sessões antigas; retorna a quantidade removida."""


class UserRepository(ABC):
    @abstractmethod
    def buscar(self, usuario_id: str) -> User | None:
        """Usuário pelo id."""

    @abstractmethod
    def buscar_por_email(self, email: str) -> User | None:
        """Usuário pelo login."""

    @abstractmethod
    def listar_por_estacionamento(self, estacionamento_id: str) -> list[User]:
        """Administradores responsáveis por um estacionamento."""

    @abstractmethod
    def salvar(self, usuario: User) -> None:
        """Cria ou atualiza."""


class PasswordHasher(ABC):
    @abstractmethod
    def gerar_hash(self, senha: str) -> str:
        """Gera o hash da senha."""

    @abstractmethod
    def verificar(self, senha: str, senha_hash: str) -> bool:
        """Confere a senha com o hash."""


class EventPublisher(ABC):
    @abstractmethod
    def publicar(self, mudanca: SpotChange) -> None:
        """Envia um evento a todos os inscritos."""

    @abstractmethod
    def inscrever(self, estacionamento_id: str) -> AsyncIterator[SpotChange]:
        """Fluxo assíncrono de eventos de um estacionamento (consumido pela rota SSE).

        Implementações podem ser geradores assíncronos (``async def`` com ``yield``).
        """

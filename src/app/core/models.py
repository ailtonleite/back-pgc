"""Entidades de domínio e exceções.

Classes de dados puras: não acessam banco, rede ou modelo de IA.
A única dependência externa é o ``shapely``, usado nas operações com polígonos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from shapely.geometry import Polygon as _ShapelyPolygon


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _para_poligono(pontos: list[Point]) -> _ShapelyPolygon:
    """Converte uma lista de ``Point`` em polígono do shapely, corrigindo autointerseções."""
    poligono = _ShapelyPolygon([(p.x, p.y) for p in pontos])
    if not poligono.is_valid:
        poligono = poligono.buffer(0)
    return poligono


# --- Exceções de domínio (traduzidas em códigos HTTP pela camada api/) ---


class ErroDominio(Exception):
    """Base das exceções de domínio."""


class NaoEncontrado(ErroDominio):
    """Entidade inexistente (HTTP 404)."""


class AcessoNegado(ErroDominio):
    """Usuário sem permissão, ex.: admin de outro estacionamento (HTTP 403)."""


class DadosInvalidos(ErroDominio):
    """Regra de validação violada, ex.: polígono inválido ou placa em formato errado (HTTP 422)."""


class FonteIndisponivel(ErroDominio):
    """Não foi possível conectar à câmera ou ler um frame (HTTP 503)."""


# --- Entidades ---


class SpotStatus(str, Enum):
    """Estado de ocupação de uma vaga."""

    LIVRE = "LIVRE"
    OCUPADA = "OCUPADA"
    DESCONHECIDA = "DESCONHECIDA"


class Role(str, Enum):
    """Perfil de acesso do usuário."""

    ADMIN = "ADMIN"
    MOTORISTA = "MOTORISTA"
    VISITANTE = "VISITANTE"


@dataclass(frozen=True)
class Point:
    """Ponto na imagem da câmera, em pixels."""

    x: float
    y: float


@dataclass
class ParkingLot:
    """Estacionamento: agrupa câmeras e vagas."""

    id: str
    nome: str
    endereco: str | None = None
    ativo: bool = False
    criado_em: datetime = field(default_factory=_agora)

    def ativar(self) -> None:
        """Torna o estacionamento visível aos motoristas."""
        self.ativo = True

    def desativar(self) -> None:
        """Oculta o estacionamento dos motoristas."""
        self.ativo = False


@dataclass
class Camera:
    """Câmera cadastrada em um estacionamento."""

    id: str
    estacionamento_id: str
    nome: str
    stream_url: str
    tipo_fonte: str  # "arquivo", "rtsp" ou "kinesis"
    ativa: bool = False
    criada_em: datetime = field(default_factory=_agora)

    def ativar(self) -> None:
        self.ativa = True

    def desativar(self) -> None:
        self.ativa = False


@dataclass
class ParkingSpot:
    """Vaga demarcada manualmente pelo administrador sobre a imagem de uma câmera."""

    id: str
    estacionamento_id: str
    camera_id: str
    rotulo: str
    poligono: list[Point]
    status: SpotStatus = SpotStatus.DESCONHECIDA
    atualizado_em: datetime = field(default_factory=_agora)

    def area(self) -> float:
        """Área do polígono em pixels²."""
        return _para_poligono(self.poligono).area

    def fracao_coberta(self, poligono: list[Point]) -> float:
        """Fração da área da vaga coberta por outro polígono (0 a 1). Decide a ocupação."""
        vaga = _para_poligono(self.poligono)
        if vaga.area <= 0:
            return 0.0
        outro = _para_poligono(poligono)
        return min(1.0, vaga.intersection(outro).area / vaga.area)

    def atualizar_status(self, novo: SpotStatus) -> bool:
        """Atualiza o status; retorna ``True`` somente se ele mudou."""
        if novo == self.status:
            return False
        self.status = novo
        self.atualizado_em = _agora()
        return True


@dataclass
class Detection:
    """Detecção de veículo, independente do formato do Ultralytics."""

    poligono: list[Point]  # caixa orientada (OBB) com 4 cantos
    confianca: float
    classe: str = "car"


@dataclass
class SpotChange:
    """Evento gerado quando uma vaga muda de estado (enviado ao front-end via SSE)."""

    vaga_id: str
    estacionamento_id: str
    status_anterior: SpotStatus
    status_novo: SpotStatus
    timestamp: datetime = field(default_factory=_agora)


@dataclass
class OccupancySnapshot:
    """Fotografia agregada da ocupação de um estacionamento (unidade do histórico)."""

    timestamp: datetime
    estacionamento_id: str
    total: int
    livres: int
    ocupadas: int

    @property
    def taxa_ocupacao(self) -> float:
        return self.ocupadas / self.total if self.total else 0.0

    @classmethod
    def de_vagas(
        cls, estacionamento_id: str, vagas: Iterable[ParkingSpot]
    ) -> OccupancySnapshot:
        """Cria o snapshot contando os status de uma lista de vagas."""
        vagas = list(vagas)
        return cls(
            timestamp=_agora(),
            estacionamento_id=estacionamento_id,
            total=len(vagas),
            livres=sum(1 for v in vagas if v.status == SpotStatus.LIVRE),
            ocupadas=sum(1 for v in vagas if v.status == SpotStatus.OCUPADA),
        )


@dataclass
class User:
    """Usuário do sistema.

    LGPD: a placa é informada pelo próprio usuário, com seu consentimento.
    """

    id: str
    nome: str
    email: str
    senha_hash: str
    papel: Role
    placa: str | None = None
    estacionamentos: list[str] = field(default_factory=list)  # somente admin

    def e_admin(self) -> bool:
        return self.papel == Role.ADMIN

    def pode_gerenciar(self, estacionamento_id: str) -> bool:
        """``True`` se for admin e responsável pelo estacionamento."""
        return self.e_admin() and estacionamento_id in self.estacionamentos


@dataclass
class ParkingSession:
    """Registro de onde o motorista estacionou (UC03), indicado por ele no mapa."""

    id: str
    usuario_id: str
    vaga_id: str
    estacionamento_id: str
    inicio: datetime = field(default_factory=_agora)
    fim: datetime | None = None

    @property
    def ativa(self) -> bool:
        return self.fim is None

    def encerrar(self, momento: datetime | None = None) -> None:
        """Preenche ``fim``; lança ``DadosInvalidos`` se a sessão já estiver encerrada."""
        if not self.ativa:
            raise DadosInvalidos("Sessão de estacionamento já encerrada.")
        self.fim = momento or _agora()

    def duracao(self, agora: datetime | None = None) -> timedelta:
        """Tempo desde o ``inicio`` (até ``fim``, se encerrada)."""
        return (self.fim or agora or _agora()) - self.inicio

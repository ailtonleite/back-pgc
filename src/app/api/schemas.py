"""Modelos Pydantic de entrada (``...In``) e saída (``...Out``) da API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.models import (
    Camera,
    OccupancySnapshot,
    ParkingLot,
    ParkingSession,
    ParkingSpot,
    Point,
    Role,
    SpotStatus,
    User,
)

TipoFonte = Literal["arquivo", "rtsp", "kinesis"]


# --- Usuários e autenticação ---


class LoginIn(BaseModel):
    email: str
    senha: str


class CadastroIn(BaseModel):
    nome: str
    email: str
    senha: str
    placa: str | None = None


class UsuarioOut(BaseModel):
    id: str
    nome: str
    email: str
    papel: Role
    placa: str | None

    @classmethod
    def de_entidade(cls, usuario: User) -> UsuarioOut:
        return cls(
            id=usuario.id,
            nome=usuario.nome,
            email=usuario.email,
            papel=usuario.papel,
            placa=usuario.placa,
        )


class PlacaIn(BaseModel):
    placa: str | None  # None ou vazio remove a placa


# --- Estacionamentos ---


class EstacionamentoIn(BaseModel):
    nome: str
    endereco: str | None = None
    ativo: bool | None = None  # na edição, torna o estacionamento visível/oculto


class EstacionamentoOut(BaseModel):
    id: str
    nome: str
    endereco: str | None
    ativo: bool

    @classmethod
    def de_entidade(cls, estacionamento: ParkingLot) -> EstacionamentoOut:
        return cls(
            id=estacionamento.id,
            nome=estacionamento.nome,
            endereco=estacionamento.endereco,
            ativo=estacionamento.ativo,
        )


class AdminIn(BaseModel):
    usuario_id: str


# --- Câmeras e vagas ---


class CameraIn(BaseModel):
    nome: str
    stream_url: str
    tipo_fonte: TipoFonte


class CameraOut(BaseModel):
    """Não expõe ``stream_url``."""

    id: str
    estacionamento_id: str
    nome: str
    tipo_fonte: TipoFonte
    ativa: bool

    @classmethod
    def de_entidade(cls, camera: Camera) -> CameraOut:
        return cls(
            id=camera.id,
            estacionamento_id=camera.estacionamento_id,
            nome=camera.nome,
            tipo_fonte=camera.tipo_fonte,
            ativa=camera.ativa,
        )


class PontoIn(BaseModel):
    """Ponto de um polígono, em pixels da imagem da câmera."""

    x: float
    y: float

    def para_entidade(self) -> Point:
        return Point(self.x, self.y)


class VagaIn(BaseModel):
    rotulo: str
    poligono: list[PontoIn] = Field(min_length=4, max_length=4)


class VagaOut(BaseModel):
    id: str
    camera_id: str
    rotulo: str
    poligono: list[PontoIn]
    status: SpotStatus
    atualizado_em: datetime

    @classmethod
    def de_entidade(cls, vaga: ParkingSpot) -> VagaOut:
        return cls(
            id=vaga.id,
            camera_id=vaga.camera_id,
            rotulo=vaga.rotulo,
            poligono=[PontoIn(x=p.x, y=p.y) for p in vaga.poligono],
            status=vaga.status,
            atualizado_em=vaga.atualizado_em,
        )


# --- Ocupação, histórico e relatórios ---


class TotaisOut(BaseModel):
    total: int
    livres: int
    ocupadas: int
    taxa_ocupacao: float

    @classmethod
    def de_entidade(cls, snapshot: OccupancySnapshot) -> TotaisOut:
        return cls(
            total=snapshot.total,
            livres=snapshot.livres,
            ocupadas=snapshot.ocupadas,
            taxa_ocupacao=snapshot.taxa_ocupacao,
        )


class SnapshotOut(BaseModel):
    timestamp: datetime
    livres: int
    ocupadas: int
    taxa_ocupacao: float

    @classmethod
    def de_entidade(cls, snapshot: OccupancySnapshot) -> SnapshotOut:
        return cls(
            timestamp=snapshot.timestamp,
            livres=snapshot.livres,
            ocupadas=snapshot.ocupadas,
            taxa_ocupacao=snapshot.taxa_ocupacao,
        )


class RelatorioOut(BaseModel):
    """Taxas de 0 a 1; ``None`` quando não há registros no período."""

    inicio: datetime
    fim: datetime
    media: float | None
    pico: float | None
    horario_pico: datetime | None

    @classmethod
    def de_entidade(cls, relatorio: dict) -> RelatorioOut:
        return cls(**relatorio)


# --- "Onde estacionei" (UC03) ---


class RegistroVagaIn(BaseModel):
    vaga_id: str


class SessaoOut(BaseModel):
    vaga_id: str
    vaga_rotulo: str
    estacionamento_id: str
    estacionamento_nome: str | None
    inicio: datetime
    tempo_estacionado_min: int

    @classmethod
    def de_entidade(
        cls, sessao: ParkingSession, vaga: ParkingSpot, estacionamento: ParkingLot | None
    ) -> SessaoOut:
        return cls(
            vaga_id=vaga.id,
            vaga_rotulo=vaga.rotulo,
            estacionamento_id=sessao.estacionamento_id,
            estacionamento_nome=estacionamento.nome if estacionamento else None,
            inicio=sessao.inicio,
            tempo_estacionado_min=int(sessao.duracao().total_seconds() // 60),
        )

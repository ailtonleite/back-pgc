"""Modelos Pydantic de entrada (``...In``) e saída (``...Out``) da API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.models import (
    Camera,
    Demarcacao,
    Endereco,
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


class EnderecoIn(BaseModel):
    """Endereço. ``estado``: sigla da UF; ``cep``: 8 dígitos, com ou sem hífen."""

    rua: str
    numero: str
    complemento: str | None = None
    bairro: str
    cidade: str
    estado: str
    cep: str

    def para_entidade(self) -> Endereco:
        return Endereco(**self.model_dump())


class EnderecoOut(BaseModel):
    """``cep`` sai só com os 8 dígitos."""

    rua: str
    numero: str
    complemento: str | None
    bairro: str
    cidade: str
    estado: str
    cep: str

    @classmethod
    def de_entidade(cls, endereco: Endereco) -> EnderecoOut:
        return cls(
            rua=endereco.rua,
            numero=endereco.numero,
            complemento=endereco.complemento,
            bairro=endereco.bairro,
            cidade=endereco.cidade,
            estado=endereco.estado,
            cep=endereco.cep,
        )


class EstacionamentoIn(BaseModel):
    """Cadastro de estacionamento."""

    nome: str
    endereco: EnderecoIn | None = None
    ativo: bool | None = None


class EstacionamentoEdicaoIn(BaseModel):
    """Edição parcial: só os campos enviados são alterados.

    O ``endereco`` é substituído por inteiro (envie todos os campos dele);
    ``endereco: null`` remove o endereço.
    """

    nome: str | None = None
    endereco: EnderecoIn | None = None
    ativo: bool | None = None  # torna o estacionamento visível/oculto aos motoristas


class EstacionamentoOut(BaseModel):
    id: str
    nome: str
    endereco: EnderecoOut | None
    ativo: bool

    @classmethod
    def de_entidade(cls, estacionamento: ParkingLot) -> EstacionamentoOut:
        endereco = estacionamento.endereco
        return cls(
            id=estacionamento.id,
            nome=estacionamento.nome,
            endereco=EnderecoOut.de_entidade(endereco) if endereco else None,
            ativo=estacionamento.ativo,
        )


class AdminIn(BaseModel):
    """Administrador a tornar responsável, pelo id ou pelo e-mail (exatamente um)."""

    usuario_id: str | None = None
    email: str | None = None

    @model_validator(mode="after")
    def _um_identificador(self) -> AdminIn:
        if bool(self.usuario_id) == bool(self.email):
            raise ValueError("Informe usuario_id ou email (apenas um).")
        return self


class AdminOut(BaseModel):
    id: str
    nome: str
    email: str

    @classmethod
    def de_entidade(cls, usuario: User) -> AdminOut:
        return cls(id=usuario.id, nome=usuario.nome, email=usuario.email)


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
    """Vaga demarcada. Com ``id``, atualiza a vaga existente; sem ``id``, cria uma nova."""

    id: str | None = None
    rotulo: str
    poligono: list[PontoIn] = Field(min_length=4, max_length=4)
    preferencial: bool = False

    def para_entidade(self) -> Demarcacao:
        return Demarcacao(
            rotulo=self.rotulo,
            poligono=[p.para_entidade() for p in self.poligono],
            id=self.id,
            preferencial=self.preferencial,
        )


class VagaOut(BaseModel):
    id: str
    camera_id: str
    camera_nome: str | None
    rotulo: str
    poligono: list[PontoIn]
    status: SpotStatus
    atualizado_em: datetime
    preferencial: bool

    @classmethod
    def de_entidade(cls, vaga: ParkingSpot, camera_nome: str | None = None) -> VagaOut:
        return cls(
            id=vaga.id,
            camera_id=vaga.camera_id,
            camera_nome=camera_nome,
            rotulo=vaga.rotulo,
            poligono=[PontoIn(x=p.x, y=p.y) for p in vaga.poligono],
            status=vaga.status,
            atualizado_em=vaga.atualizado_em,
            preferencial=vaga.preferencial,
        )


# --- Ocupação, histórico e relatórios ---


class TotaisOut(BaseModel):
    """Totais de todas as vagas; os ``preferenciais_*`` contam só as preferenciais."""

    total: int
    livres: int
    ocupadas: int
    taxa_ocupacao: float
    preferenciais_total: int = 0
    preferenciais_livres: int = 0
    preferenciais_ocupadas: int = 0

    @classmethod
    def de_entidade(
        cls, snapshot: OccupancySnapshot, preferenciais: OccupancySnapshot | None = None
    ) -> TotaisOut:
        totais = cls(
            total=snapshot.total,
            livres=snapshot.livres,
            ocupadas=snapshot.ocupadas,
            taxa_ocupacao=snapshot.taxa_ocupacao,
        )
        if preferenciais is not None:
            totais.preferenciais_total = preferenciais.total
            totais.preferenciais_livres = preferenciais.livres
            totais.preferenciais_ocupadas = preferenciais.ocupadas
        return totais


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

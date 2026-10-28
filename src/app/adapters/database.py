"""Repositórios: em memória (testes e modo local), DynamoDB (vagas) e PostgreSQL (demais)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

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
from app.core.ports import (
    CameraRepository,
    HistoryRepository,
    ParkingLotRepository,
    SessionRepository,
    SpotRepository,
    UserRepository,
)


# ============================================================================
# Em memória
# ============================================================================


class _InMemoryRepository:
    """Base dos repositórios em memória.

    Guarda e devolve cópias, para se comportar como um banco real: alterar uma
    entidade só tem efeito depois de ``salvar``. A trava protege o acesso
    concorrente das threads de captura.
    """

    def __init__(self) -> None:
        self._dados: dict[str, Any] = {}
        self._trava = threading.Lock()

    def _guardar(self, entidade: Any) -> None:
        with self._trava:
            self._dados[entidade.id] = deepcopy(entidade)

    def _obter(self, chave: str) -> Any:
        with self._trava:
            return deepcopy(self._dados.get(chave))

    def _filtrar(self, condicao: Callable[[Any], bool]) -> list[Any]:
        with self._trava:
            return [deepcopy(e) for e in self._dados.values() if condicao(e)]

    def _apagar(self, chave: str) -> None:
        with self._trava:
            self._dados.pop(chave, None)


class InMemoryParkingLotRepository(_InMemoryRepository, ParkingLotRepository):
    def listar(self, apenas_ativos: bool = False) -> list[ParkingLot]:
        return self._filtrar(lambda e: e.ativo or not apenas_ativos)

    def listar_por_ids(self, ids: list[str]) -> list[ParkingLot]:
        alvo = set(ids)
        return self._filtrar(lambda e: e.id in alvo)

    def buscar(self, estacionamento_id: str) -> ParkingLot | None:
        return self._obter(estacionamento_id)

    def salvar(self, estacionamento: ParkingLot) -> None:
        self._guardar(estacionamento)

    def remover(self, estacionamento_id: str) -> None:
        self._apagar(estacionamento_id)


class InMemoryCameraRepository(_InMemoryRepository, CameraRepository):
    def listar_por_estacionamento(self, estacionamento_id: str) -> list[Camera]:
        return self._filtrar(lambda c: c.estacionamento_id == estacionamento_id)

    def listar_ativas(self) -> list[Camera]:
        return self._filtrar(lambda c: c.ativa)

    def buscar(self, camera_id: str) -> Camera | None:
        return self._obter(camera_id)

    def salvar(self, camera: Camera) -> None:
        self._guardar(camera)

    def remover(self, camera_id: str) -> None:
        self._apagar(camera_id)


class InMemorySpotRepository(_InMemoryRepository, SpotRepository):
    def listar_por_camera(self, camera_id: str) -> list[ParkingSpot]:
        return self._filtrar(lambda v: v.camera_id == camera_id)

    def listar_por_estacionamento(self, estacionamento_id: str) -> list[ParkingSpot]:
        return self._filtrar(lambda v: v.estacionamento_id == estacionamento_id)

    def buscar(self, vaga_id: str) -> ParkingSpot | None:
        return self._obter(vaga_id)

    def salvar(self, vaga: ParkingSpot) -> None:
        self._guardar(vaga)

    def salvar_varias(self, vagas: list[ParkingSpot]) -> None:
        for vaga in vagas:
            self._guardar(vaga)

    def remover(self, vaga_id: str) -> None:
        self._apagar(vaga_id)

    def remover_por_camera(self, camera_id: str) -> None:
        with self._trava:
            for vaga_id in [k for k, v in self._dados.items() if v.camera_id == camera_id]:
                del self._dados[vaga_id]


class InMemoryHistoryRepository(HistoryRepository):
    def __init__(self) -> None:
        self._dados: list[OccupancySnapshot] = []
        self._trava = threading.Lock()

    def salvar_snapshot(self, snapshot: OccupancySnapshot) -> None:
        with self._trava:
            self._dados.append(deepcopy(snapshot))

    def consultar_snapshots(
        self, estacionamento_id: str, inicio: datetime, fim: datetime
    ) -> list[OccupancySnapshot]:
        with self._trava:
            return sorted(
                (
                    deepcopy(s)
                    for s in self._dados
                    if s.estacionamento_id == estacionamento_id and inicio <= s.timestamp <= fim
                ),
                key=lambda s: s.timestamp,
            )

    def expurgar_anteriores_a(self, data: datetime) -> int:
        with self._trava:
            antes = len(self._dados)
            self._dados = [s for s in self._dados if s.timestamp >= data]
            return antes - len(self._dados)


class InMemorySessionRepository(_InMemoryRepository, SessionRepository):
    def salvar(self, sessao: ParkingSession) -> None:
        self._guardar(sessao)

    def sessao_ativa(self, usuario_id: str) -> ParkingSession | None:
        ativas = self._filtrar(lambda s: s.usuario_id == usuario_id and s.ativa)
        return max(ativas, key=lambda s: s.inicio, default=None)

    def sessoes_ativas_na_vaga(self, vaga_id: str) -> list[ParkingSession]:
        return self._filtrar(lambda s: s.vaga_id == vaga_id and s.ativa)

    def expurgar_anteriores_a(self, data: datetime) -> int:
        """Remove sessões encerradas antes de ``data`` (sessões ativas são mantidas)."""
        with self._trava:
            antigas = [k for k, s in self._dados.items() if s.fim is not None and s.fim < data]
            for chave in antigas:
                del self._dados[chave]
            return len(antigas)


class InMemoryUserRepository(_InMemoryRepository, UserRepository):
    def buscar(self, usuario_id: str) -> User | None:
        return self._obter(usuario_id)

    def buscar_por_email(self, email: str) -> User | None:
        encontrados = self._filtrar(lambda u: u.email == email)
        return encontrados[0] if encontrados else None

    def listar_por_estacionamento(self, estacionamento_id: str) -> list[User]:
        return self._filtrar(lambda u: estacionamento_id in u.estacionamentos)

    def salvar(self, usuario: User) -> None:
        self._guardar(usuario)


# ============================================================================
# DynamoDB
# ============================================================================


class DynamoSpotRepository(SpotRepository):
    """Estado atual das vagas no DynamoDB.

    Esquema esperado da tabela (criada pela infraestrutura):
      - chave de partição ``estacionamento_id`` (S) e de ordenação ``vaga_id`` (S);
      - índice global ``camera_id-index`` (partição ``camera_id``, projeção ALL);
      - índice global ``vaga_id-index`` (partição ``vaga_id``, projeção ALL),
        necessário porque ``buscar`` e ``remover`` recebem apenas o id da vaga.
    """

    INDICE_CAMERA = "camera_id-index"
    INDICE_VAGA = "vaga_id-index"

    def __init__(
        self,
        nome_tabela: str,
        regiao: str | None = None,
        endpoint_url: str | None = None,  # ex.: DynamoDB Local
    ) -> None:
        import boto3  # dependência só necessária no modo AWS

        self.nome_tabela = nome_tabela
        self._tabela = boto3.resource(
            "dynamodb", region_name=regiao, endpoint_url=endpoint_url
        ).Table(nome_tabela)

    def _consultar(self, **parametros: Any) -> list[dict]:
        """``query`` com paginação."""
        itens: list[dict] = []
        while True:
            resposta = self._tabela.query(**parametros)
            itens.extend(resposta.get("Items", []))
            if "LastEvaluatedKey" not in resposta:
                return itens
            parametros["ExclusiveStartKey"] = resposta["LastEvaluatedKey"]

    def _itens_da_camera(self, camera_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        return self._consultar(
            IndexName=self.INDICE_CAMERA, KeyConditionExpression=Key("camera_id").eq(camera_id)
        )

    def listar_por_camera(self, camera_id: str) -> list[ParkingSpot]:
        return [self._de_item(i) for i in self._itens_da_camera(camera_id)]

    def listar_por_estacionamento(self, estacionamento_id: str) -> list[ParkingSpot]:
        from boto3.dynamodb.conditions import Key

        itens = self._consultar(
            KeyConditionExpression=Key("estacionamento_id").eq(estacionamento_id)
        )
        return [self._de_item(i) for i in itens]

    def buscar(self, vaga_id: str) -> ParkingSpot | None:
        from boto3.dynamodb.conditions import Key

        itens = self._consultar(
            IndexName=self.INDICE_VAGA, KeyConditionExpression=Key("vaga_id").eq(vaga_id)
        )
        return self._de_item(itens[0]) if itens else None

    def salvar(self, vaga: ParkingSpot) -> None:
        self._tabela.put_item(Item=self._para_item(vaga))

    def salvar_varias(self, vagas: list[ParkingSpot]) -> None:
        with self._tabela.batch_writer() as lote:
            for vaga in vagas:
                lote.put_item(Item=self._para_item(vaga))

    def remover(self, vaga_id: str) -> None:
        vaga = self.buscar(vaga_id)
        if vaga is not None:
            self._tabela.delete_item(
                Key={"estacionamento_id": vaga.estacionamento_id, "vaga_id": vaga.id}
            )

    def remover_por_camera(self, camera_id: str) -> None:
        with self._tabela.batch_writer() as lote:
            for item in self._itens_da_camera(camera_id):
                lote.delete_item(
                    Key={"estacionamento_id": item["estacionamento_id"], "vaga_id": item["vaga_id"]}
                )

    @staticmethod
    def _para_item(vaga: ParkingSpot) -> dict:
        """Converte ``ParkingSpot`` em item do DynamoDB (números como ``Decimal``)."""
        return {
            "estacionamento_id": vaga.estacionamento_id,
            "vaga_id": vaga.id,
            "camera_id": vaga.camera_id,
            "rotulo": vaga.rotulo,
            "poligono": [{"x": Decimal(str(p.x)), "y": Decimal(str(p.y))} for p in vaga.poligono],
            "status": vaga.status.value,
            "atualizado_em": vaga.atualizado_em.isoformat(),
        }

    @staticmethod
    def _de_item(item: dict) -> ParkingSpot:
        """Converte item do DynamoDB em ``ParkingSpot``."""
        return ParkingSpot(
            id=item["vaga_id"],
            estacionamento_id=item["estacionamento_id"],
            camera_id=item["camera_id"],
            rotulo=item["rotulo"],
            poligono=[Point(float(p["x"]), float(p["y"])) for p in item["poligono"]],
            status=SpotStatus(item["status"]),
            atualizado_em=datetime.fromisoformat(item["atualizado_em"]),
        )


# ============================================================================
# PostgreSQL (SQLAlchemy Core; os tipos genéricos também funcionam com SQLite)
# ============================================================================

_metadata = sa.MetaData()
_ID = sa.String(64)

_estacionamentos = sa.Table(
    "estacionamentos",
    _metadata,
    sa.Column("id", _ID, primary_key=True),
    sa.Column("nome", sa.String(200), nullable=False),
    sa.Column("endereco", sa.String(300)),
    sa.Column("ativo", sa.Boolean, nullable=False),
    sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
)

_cameras = sa.Table(
    "cameras",
    _metadata,
    sa.Column("id", _ID, primary_key=True),
    sa.Column("estacionamento_id", _ID, sa.ForeignKey("estacionamentos.id"), nullable=False, index=True),
    sa.Column("nome", sa.String(200), nullable=False),
    sa.Column("stream_url", sa.Text, nullable=False),
    sa.Column("tipo_fonte", sa.String(20), nullable=False),
    sa.Column("ativa", sa.Boolean, nullable=False),
    sa.Column("criada_em", sa.DateTime(timezone=True), nullable=False),
)

_usuarios = sa.Table(
    "usuarios",
    _metadata,
    sa.Column("id", _ID, primary_key=True),
    sa.Column("nome", sa.String(200), nullable=False),
    sa.Column("email", sa.String(254), nullable=False, unique=True),
    sa.Column("senha_hash", sa.String(100), nullable=False),
    sa.Column("papel", sa.String(20), nullable=False),
    sa.Column("placa", sa.String(7)),
)

_usuario_estacionamento = sa.Table(
    "usuario_estacionamento",
    _metadata,
    sa.Column("usuario_id", _ID, sa.ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True),
    sa.Column(
        "estacionamento_id",
        _ID,
        sa.ForeignKey("estacionamentos.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)

_snapshots = sa.Table(
    "snapshots",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("estacionamento_id", _ID, nullable=False),
    sa.Column("total", sa.Integer, nullable=False),
    sa.Column("livres", sa.Integer, nullable=False),
    sa.Column("ocupadas", sa.Integer, nullable=False),
    sa.Index("ix_snapshots_estacionamento_timestamp", "estacionamento_id", "timestamp"),
)

_sessoes = sa.Table(
    "sessoes",
    _metadata,
    sa.Column("id", _ID, primary_key=True),
    sa.Column("usuario_id", _ID, sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("vaga_id", _ID, nullable=False, index=True),  # vaga fica no DynamoDB: sem FK
    sa.Column("estacionamento_id", _ID, nullable=False),
    sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fim", sa.DateTime(timezone=True)),
)


def criar_tabelas(engine: sa.Engine) -> None:
    """Cria as tabelas se não existirem."""
    _metadata.create_all(engine)


def _utc(momento: datetime | None) -> datetime | None:
    """Garante ``datetime`` com fuso (bancos sem suporte a fuso devolvem horário ingênuo)."""
    if momento is not None and momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento


def _upsert(conexao: sa.Connection, tabela: sa.Table, valores: dict) -> None:
    """Atualiza a linha pelo id ou a insere se ainda não existir (portável entre bancos)."""
    resultado = conexao.execute(tabela.update().where(tabela.c.id == valores["id"]).values(**valores))
    if resultado.rowcount == 0:
        conexao.execute(tabela.insert().values(**valores))


class _PostgresRepository:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine


class PostgresParkingLotRepository(_PostgresRepository, ParkingLotRepository):
    def listar(self, apenas_ativos: bool = False) -> list[ParkingLot]:
        consulta = sa.select(_estacionamentos).order_by(_estacionamentos.c.nome)
        if apenas_ativos:
            consulta = consulta.where(_estacionamentos.c.ativo.is_(True))
        with self._engine.connect() as conexao:
            return [self._de_linha(l) for l in conexao.execute(consulta)]

    def listar_por_ids(self, ids: list[str]) -> list[ParkingLot]:
        if not ids:
            return []
        consulta = (
            sa.select(_estacionamentos)
            .where(_estacionamentos.c.id.in_(ids))
            .order_by(_estacionamentos.c.nome)
        )
        with self._engine.connect() as conexao:
            return [self._de_linha(l) for l in conexao.execute(consulta)]

    def buscar(self, estacionamento_id: str) -> ParkingLot | None:
        consulta = sa.select(_estacionamentos).where(_estacionamentos.c.id == estacionamento_id)
        with self._engine.connect() as conexao:
            linha = conexao.execute(consulta).first()
        return self._de_linha(linha) if linha else None

    def salvar(self, estacionamento: ParkingLot) -> None:
        with self._engine.begin() as conexao:
            _upsert(
                conexao,
                _estacionamentos,
                {
                    "id": estacionamento.id,
                    "nome": estacionamento.nome,
                    "endereco": estacionamento.endereco,
                    "ativo": estacionamento.ativo,
                    "criado_em": estacionamento.criado_em,
                },
            )

    def remover(self, estacionamento_id: str) -> None:
        with self._engine.begin() as conexao:
            conexao.execute(
                _usuario_estacionamento.delete().where(
                    _usuario_estacionamento.c.estacionamento_id == estacionamento_id
                )
            )
            conexao.execute(_estacionamentos.delete().where(_estacionamentos.c.id == estacionamento_id))

    @staticmethod
    def _de_linha(linha: sa.Row) -> ParkingLot:
        return ParkingLot(
            id=linha.id,
            nome=linha.nome,
            endereco=linha.endereco,
            ativo=linha.ativo,
            criado_em=_utc(linha.criado_em),
        )


class PostgresCameraRepository(_PostgresRepository, CameraRepository):
    def _listar(self, condicao: sa.ColumnElement[bool]) -> list[Camera]:
        consulta = sa.select(_cameras).where(condicao).order_by(_cameras.c.nome)
        with self._engine.connect() as conexao:
            return [self._de_linha(l) for l in conexao.execute(consulta)]

    def listar_por_estacionamento(self, estacionamento_id: str) -> list[Camera]:
        return self._listar(_cameras.c.estacionamento_id == estacionamento_id)

    def listar_ativas(self) -> list[Camera]:
        return self._listar(_cameras.c.ativa.is_(True))

    def buscar(self, camera_id: str) -> Camera | None:
        with self._engine.connect() as conexao:
            linha = conexao.execute(sa.select(_cameras).where(_cameras.c.id == camera_id)).first()
        return self._de_linha(linha) if linha else None

    def salvar(self, camera: Camera) -> None:
        with self._engine.begin() as conexao:
            _upsert(
                conexao,
                _cameras,
                {
                    "id": camera.id,
                    "estacionamento_id": camera.estacionamento_id,
                    "nome": camera.nome,
                    "stream_url": camera.stream_url,
                    "tipo_fonte": camera.tipo_fonte,
                    "ativa": camera.ativa,
                    "criada_em": camera.criada_em,
                },
            )

    def remover(self, camera_id: str) -> None:
        with self._engine.begin() as conexao:
            conexao.execute(_cameras.delete().where(_cameras.c.id == camera_id))

    @staticmethod
    def _de_linha(linha: sa.Row) -> Camera:
        return Camera(
            id=linha.id,
            estacionamento_id=linha.estacionamento_id,
            nome=linha.nome,
            stream_url=linha.stream_url,
            tipo_fonte=linha.tipo_fonte,
            ativa=linha.ativa,
            criada_em=_utc(linha.criada_em),
        )


class PostgresHistoryRepository(_PostgresRepository, HistoryRepository):
    def salvar_snapshot(self, snapshot: OccupancySnapshot) -> None:
        with self._engine.begin() as conexao:
            conexao.execute(
                _snapshots.insert().values(
                    timestamp=snapshot.timestamp,
                    estacionamento_id=snapshot.estacionamento_id,
                    total=snapshot.total,
                    livres=snapshot.livres,
                    ocupadas=snapshot.ocupadas,
                )
            )

    def consultar_snapshots(
        self, estacionamento_id: str, inicio: datetime, fim: datetime
    ) -> list[OccupancySnapshot]:
        consulta = (
            sa.select(_snapshots)
            .where(
                _snapshots.c.estacionamento_id == estacionamento_id,
                _snapshots.c.timestamp.between(inicio, fim),
            )
            .order_by(_snapshots.c.timestamp)
        )
        with self._engine.connect() as conexao:
            return [self._de_linha(l) for l in conexao.execute(consulta)]

    def expurgar_anteriores_a(self, data: datetime) -> int:
        with self._engine.begin() as conexao:
            return conexao.execute(_snapshots.delete().where(_snapshots.c.timestamp < data)).rowcount

    @staticmethod
    def _de_linha(linha: sa.Row) -> OccupancySnapshot:
        return OccupancySnapshot(
            timestamp=_utc(linha.timestamp),
            estacionamento_id=linha.estacionamento_id,
            total=linha.total,
            livres=linha.livres,
            ocupadas=linha.ocupadas,
        )


class PostgresSessionRepository(_PostgresRepository, SessionRepository):
    def salvar(self, sessao: ParkingSession) -> None:
        with self._engine.begin() as conexao:
            _upsert(
                conexao,
                _sessoes,
                {
                    "id": sessao.id,
                    "usuario_id": sessao.usuario_id,
                    "vaga_id": sessao.vaga_id,
                    "estacionamento_id": sessao.estacionamento_id,
                    "inicio": sessao.inicio,
                    "fim": sessao.fim,
                },
            )

    def sessao_ativa(self, usuario_id: str) -> ParkingSession | None:
        consulta = (
            sa.select(_sessoes)
            .where(_sessoes.c.usuario_id == usuario_id, _sessoes.c.fim.is_(None))
            .order_by(_sessoes.c.inicio.desc())
        )
        with self._engine.connect() as conexao:
            linha = conexao.execute(consulta).first()
        return self._de_linha(linha) if linha else None

    def sessoes_ativas_na_vaga(self, vaga_id: str) -> list[ParkingSession]:
        consulta = sa.select(_sessoes).where(_sessoes.c.vaga_id == vaga_id, _sessoes.c.fim.is_(None))
        with self._engine.connect() as conexao:
            return [self._de_linha(l) for l in conexao.execute(consulta)]

    def expurgar_anteriores_a(self, data: datetime) -> int:
        """Remove sessões encerradas antes de ``data`` (sessões ativas são mantidas)."""
        with self._engine.begin() as conexao:
            return conexao.execute(
                _sessoes.delete().where(_sessoes.c.fim.is_not(None), _sessoes.c.fim < data)
            ).rowcount

    @staticmethod
    def _de_linha(linha: sa.Row) -> ParkingSession:
        return ParkingSession(
            id=linha.id,
            usuario_id=linha.usuario_id,
            vaga_id=linha.vaga_id,
            estacionamento_id=linha.estacionamento_id,
            inicio=_utc(linha.inicio),
            fim=_utc(linha.fim),
        )


class PostgresUserRepository(_PostgresRepository, UserRepository):
    """``User.estacionamentos`` fica na tabela de associação ``usuario_estacionamento``."""

    def _carregar(self, conexao: sa.Connection, linhas: Iterable[sa.Row]) -> list[User]:
        """Converte as linhas e preenche os estacionamentos de todos em uma consulta."""
        usuarios = [self._de_linha(l) for l in linhas]
        if usuarios:
            por_id = {u.id: u for u in usuarios}
            associacoes = conexao.execute(
                sa.select(_usuario_estacionamento).where(
                    _usuario_estacionamento.c.usuario_id.in_(por_id)
                )
            )
            for associacao in associacoes:
                por_id[associacao.usuario_id].estacionamentos.append(associacao.estacionamento_id)
        return usuarios

    def _buscar_um(self, condicao: sa.ColumnElement[bool]) -> User | None:
        with self._engine.connect() as conexao:
            linha = conexao.execute(sa.select(_usuarios).where(condicao)).first()
            return self._carregar(conexao, [linha])[0] if linha else None

    def buscar(self, usuario_id: str) -> User | None:
        return self._buscar_um(_usuarios.c.id == usuario_id)

    def buscar_por_email(self, email: str) -> User | None:
        return self._buscar_um(_usuarios.c.email == email)

    def listar_por_estacionamento(self, estacionamento_id: str) -> list[User]:
        consulta = sa.select(_usuarios).join(
            _usuario_estacionamento, _usuario_estacionamento.c.usuario_id == _usuarios.c.id
        ).where(_usuario_estacionamento.c.estacionamento_id == estacionamento_id)
        with self._engine.connect() as conexao:
            return self._carregar(conexao, conexao.execute(consulta).all())

    def salvar(self, usuario: User) -> None:
        with self._engine.begin() as conexao:
            _upsert(
                conexao,
                _usuarios,
                {
                    "id": usuario.id,
                    "nome": usuario.nome,
                    "email": usuario.email,
                    "senha_hash": usuario.senha_hash,
                    "papel": usuario.papel.value,
                    "placa": usuario.placa,
                },
            )
            conexao.execute(
                _usuario_estacionamento.delete().where(
                    _usuario_estacionamento.c.usuario_id == usuario.id
                )
            )
            if usuario.estacionamentos:
                conexao.execute(
                    _usuario_estacionamento.insert(),
                    [
                        {"usuario_id": usuario.id, "estacionamento_id": e}
                        for e in dict.fromkeys(usuario.estacionamentos)
                    ],
                )

    @staticmethod
    def _de_linha(linha: sa.Row) -> User:
        return User(
            id=linha.id,
            nome=linha.nome,
            email=linha.email,
            senha_hash=linha.senha_hash,
            papel=Role(linha.papel),
            placa=linha.placa,
        )

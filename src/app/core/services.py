"""Regras de negócio.

Cada serviço recebe suas dependências pelo construtor, tipadas pelas portas de
``core/ports.py``. Métodos que recebem ``usuario`` verificam a permissão antes de
agir e lançam ``AcessoNegado`` quando o admin não é responsável pelo estacionamento.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, TypeVar

from app.core.models import (
    AcessoNegado,
    Camera,
    DadosInvalidos,
    FonteIndisponivel,
    NaoEncontrado,
    OccupancySnapshot,
    ParkingLot,
    ParkingSession,
    ParkingSpot,
    Point,
    Role,
    SpotChange,
    SpotStatus,
    User,
)
from app.core.ports import (
    CameraRepository,
    EventPublisher,
    FrameSource,
    HistoryRepository,
    OccupancyStrategy,
    ParkingLotRepository,
    PasswordHasher,
    SessionRepository,
    SpotRepository,
    UserRepository,
    VehicleDetector,
)

if TYPE_CHECKING:
    from numpy import ndarray

logger = logging.getLogger(__name__)

T = TypeVar("T")

TIPOS_FONTE = {"arquivo", "rtsp", "kinesis"}
GRANULARIDADES = {"hora", "dia", "semana"}
FUSO_PADRAO = timezone(timedelta(hours=-3), "BRT")  # horário de Brasília (sem horário de verão)
TAMANHO_MIN_SENHA = 8
_FORMATO_PLACA = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")  # antigo AAA1234 e Mercosul AAA1A23


# --- Auxiliares ---


def _novo_id() -> str:
    return uuid.uuid4().hex


def _obter(entidade: T | None, descricao: str) -> T:
    """Devolve a entidade ou lança ``NaoEncontrado``."""
    if entidade is None:
        raise NaoEncontrado(f"{descricao} não encontrado(a).")
    return entidade


def _exigir_admin(usuario: User) -> None:
    if not usuario.e_admin():
        raise AcessoNegado("Operação restrita a administradores.")


def _exigir_gestao(usuario: User, estacionamento_id: str) -> None:
    if not usuario.pode_gerenciar(estacionamento_id):
        raise AcessoNegado("Usuário não é responsável por este estacionamento.")


def _aplicar_campos(entidade: object, campos: dict[str, Any], permitidos: set[str]) -> None:
    """Atribui ``campos`` à entidade, recusando campos fora de ``permitidos``."""
    invalidos = set(campos) - permitidos
    if invalidos:
        raise DadosInvalidos(f"Campos não editáveis: {', '.join(sorted(invalidos))}.")
    for nome, valor in campos.items():
        setattr(entidade, nome, valor)


def _validar_texto(valor: str, descricao: str) -> str:
    valor = (valor or "").strip()
    if not valor:
        raise DadosInvalidos(f"{descricao} não pode ser vazio(a).")
    return valor


# --- Serviços ---


class ParkingLotService:
    """Cadastro de estacionamentos e responsabilidade dos administradores (UC09)."""

    def __init__(
        self,
        estacionamentos: ParkingLotRepository,
        usuarios: UserRepository,
        cameras: CameraRepository,
    ) -> None:
        self.estacionamentos = estacionamentos
        self.usuarios = usuarios
        self.cameras = cameras  # para impedir a remoção de estacionamento com câmeras

    def criar(self, usuario: User, nome: str, endereco: str | None = None) -> ParkingLot:
        """Cria o estacionamento e torna o criador responsável por ele."""
        _exigir_admin(usuario)
        estacionamento = ParkingLot(
            id=_novo_id(), nome=_validar_texto(nome, "Nome"), endereco=endereco
        )
        self.estacionamentos.salvar(estacionamento)
        usuario.estacionamentos.append(estacionamento.id)
        self.usuarios.salvar(usuario)
        return estacionamento

    def editar(self, usuario: User, estacionamento_id: str, **campos: Any) -> ParkingLot:
        """Altera nome, endereço ou status ativo."""
        _exigir_gestao(usuario, estacionamento_id)
        estacionamento = _obter(self.estacionamentos.buscar(estacionamento_id), "Estacionamento")
        if "nome" in campos:
            campos["nome"] = _validar_texto(campos["nome"], "Nome")
        _aplicar_campos(estacionamento, campos, {"nome", "endereco", "ativo"})
        self.estacionamentos.salvar(estacionamento)
        return estacionamento

    def remover(self, usuario: User, estacionamento_id: str) -> None:
        """Remove o estacionamento (somente sem câmeras cadastradas)."""
        _exigir_gestao(usuario, estacionamento_id)
        _obter(self.estacionamentos.buscar(estacionamento_id), "Estacionamento")
        if self.cameras.listar_por_estacionamento(estacionamento_id):
            raise DadosInvalidos("Remova as câmeras antes de remover o estacionamento.")
        for admin in self.usuarios.listar_por_estacionamento(estacionamento_id):
            admin.estacionamentos.remove(estacionamento_id)
            self.usuarios.salvar(admin)
        if estacionamento_id in usuario.estacionamentos:
            usuario.estacionamentos.remove(estacionamento_id)
        self.estacionamentos.remover(estacionamento_id)

    def listar_para(self, usuario: User) -> list[ParkingLot]:
        """Admin: estacionamentos sob sua responsabilidade. Demais perfis: todos os ativos."""
        if usuario.e_admin():
            return self.estacionamentos.listar_por_ids(usuario.estacionamentos)
        return self.estacionamentos.listar(apenas_ativos=True)

    def atribuir_admin(self, usuario: User, estacionamento_id: str, admin_id: str) -> None:
        """Torna outro administrador responsável pelo estacionamento."""
        _exigir_gestao(usuario, estacionamento_id)
        _obter(self.estacionamentos.buscar(estacionamento_id), "Estacionamento")
        admin = _obter(self.usuarios.buscar(admin_id), "Usuário")
        if not admin.e_admin():
            raise DadosInvalidos("Somente administradores podem ser responsáveis.")
        if estacionamento_id not in admin.estacionamentos:
            admin.estacionamentos.append(estacionamento_id)
            self.usuarios.salvar(admin)

    def remover_admin(self, usuario: User, estacionamento_id: str, admin_id: str) -> None:
        """Retira a responsabilidade, impedindo remover o último responsável."""
        _exigir_gestao(usuario, estacionamento_id)
        responsaveis = self.usuarios.listar_por_estacionamento(estacionamento_id)
        admin = _obter(next((u for u in responsaveis if u.id == admin_id), None), "Responsável")
        if len(responsaveis) <= 1:
            raise DadosInvalidos("Não é possível remover o último responsável.")
        admin.estacionamentos.remove(estacionamento_id)
        self.usuarios.salvar(admin)
        if admin.id == usuario.id and estacionamento_id in usuario.estacionamentos:
            usuario.estacionamentos.remove(estacionamento_id)


class CameraManager:
    """Cadastro de câmeras e controle dos laços de captura (UC08).

    Os laços são tarefas ``asyncio``; leitura de frames e inferência rodam em
    threads (``asyncio.to_thread``) para não bloquear a API.
    """

    def __init__(
        self,
        cameras: CameraRepository,
        vagas: SpotRepository,
        processador: FrameProcessor,
        fabrica_fonte: Callable[[Camera], FrameSource],
        intervalo_s: float = 1.0,
    ) -> None:
        self.cameras = cameras
        self.vagas = vagas
        self.processador = processador
        self.fabrica_fonte = fabrica_fonte
        self.intervalo_s = intervalo_s
        self._tarefas: dict[str, asyncio.Task] = {}
        self._sinais_parada: dict[str, asyncio.Event] = {}

    def _camera_gerenciavel(self, usuario: User, camera_id: str) -> Camera:
        camera = _obter(self.cameras.buscar(camera_id), "Câmera")
        _exigir_gestao(usuario, camera.estacionamento_id)
        return camera

    @staticmethod
    def _validar_tipo_fonte(tipo_fonte: str) -> None:
        if tipo_fonte not in TIPOS_FONTE:
            raise DadosInvalidos(f"tipo_fonte deve ser um de: {', '.join(sorted(TIPOS_FONTE))}.")

    def listar(self, usuario: User, estacionamento_id: str) -> list[Camera]:
        """Câmeras de um estacionamento."""
        _exigir_gestao(usuario, estacionamento_id)
        return self.cameras.listar_por_estacionamento(estacionamento_id)

    def cadastrar(
        self, usuario: User, estacionamento_id: str, nome: str, stream_url: str, tipo_fonte: str
    ) -> Camera:
        """Cria a câmera (inativa) no estacionamento."""
        _exigir_gestao(usuario, estacionamento_id)
        self._validar_tipo_fonte(tipo_fonte)
        camera = Camera(
            id=_novo_id(),
            estacionamento_id=estacionamento_id,
            nome=_validar_texto(nome, "Nome"),
            stream_url=_validar_texto(stream_url, "Endereço da transmissão"),
            tipo_fonte=tipo_fonte,
        )
        self.cameras.salvar(camera)
        return camera

    async def atualizar(self, usuario: User, camera_id: str, **campos: Any) -> Camera:
        """Edita parâmetros (UC08, fluxo A1). Reinicia o laço se a fonte mudar."""
        camera = self._camera_gerenciavel(usuario, camera_id)
        if "tipo_fonte" in campos:
            self._validar_tipo_fonte(campos["tipo_fonte"])
        if "nome" in campos:
            campos["nome"] = _validar_texto(campos["nome"], "Nome")
        if "stream_url" in campos:
            campos["stream_url"] = _validar_texto(campos["stream_url"], "Endereço da transmissão")
        _aplicar_campos(camera, campos, {"nome", "stream_url", "tipo_fonte"})
        self.cameras.salvar(camera)
        if {"stream_url", "tipo_fonte"} & campos.keys() and camera_id in self._tarefas:
            await self._parar_laco(camera_id)
            self._iniciar_laco(camera)
        return camera

    async def remover(self, usuario: User, camera_id: str) -> None:
        """Para o monitoramento e remove a câmera e suas vagas (UC08, fluxo A2)."""
        self._camera_gerenciavel(usuario, camera_id)
        await self._parar_laco(camera_id)
        self.vagas.remover_por_camera(camera_id)
        self.cameras.remover(camera_id)

    def capturar_frame(self, usuario: User, camera_id: str) -> ndarray:
        """Captura um frame de referência para a demarcação das vagas (UC08, fluxo E1).

        Bloqueante: deve ser chamado fora do loop assíncrono (ex.: rota síncrona).
        """
        camera = self._camera_gerenciavel(usuario, camera_id)
        fonte = self.fabrica_fonte(camera)
        try:
            fonte.abrir()
            frame = fonte.ler_frame() if fonte.esta_aberta() else None
        except Exception as erro:
            raise FonteIndisponivel(f"Falha ao conectar à câmera: {erro}") from erro
        finally:
            fonte.fechar()
        if frame is None:
            raise FonteIndisponivel("Não foi possível ler um frame da câmera.")
        return frame

    async def iniciar_monitoramento(self, usuario: User, camera_id: str) -> None:
        """Ativa a câmera e inicia seu laço de captura."""
        camera = self._camera_gerenciavel(usuario, camera_id)
        camera.ativar()
        self.cameras.salvar(camera)
        self._iniciar_laco(camera)

    async def parar_monitoramento(self, usuario: User, camera_id: str) -> None:
        """Encerra o laço, desativa a câmera e marca suas vagas como ``DESCONHECIDA``."""
        camera = self._camera_gerenciavel(usuario, camera_id)
        await self._parar_laco(camera_id)
        camera.desativar()
        self.cameras.salvar(camera)
        self.processador.marcar_desconhecidas(camera)

    async def iniciar_todas(self) -> None:
        """Inicia as câmeras ativas (subida da aplicação, sem verificação de usuário)."""
        for camera in self.cameras.listar_ativas():
            self._iniciar_laco(camera)

    async def parar_todas(self) -> None:
        """Encerra todos os laços sem desativar as câmeras (desligamento da aplicação)."""
        for camera_id in list(self._tarefas):
            await self._parar_laco(camera_id)

    def _iniciar_laco(self, camera: Camera) -> None:
        tarefa = self._tarefas.get(camera.id)
        if tarefa is not None and not tarefa.done():
            return
        self._sinais_parada[camera.id] = asyncio.Event()
        self._tarefas[camera.id] = asyncio.create_task(
            self._laco_captura(camera), name=f"captura-{camera.id}"
        )

    async def _parar_laco(self, camera_id: str) -> None:
        """Sinaliza a parada e aguarda o fim do frame em processamento."""
        sinal = self._sinais_parada.get(camera_id)
        tarefa = self._tarefas.get(camera_id)
        if sinal is not None:
            sinal.set()
        if tarefa is not None:
            await tarefa

    async def _laco_captura(self, camera: Camera) -> None:
        """Lê frames em loop e chama ``processador.processar``.

        Ao terminar (parada ou queda da fonte), marca as vagas como ``DESCONHECIDA``.
        """
        parar = self._sinais_parada[camera.id]
        fonte = self.fabrica_fonte(camera)
        try:
            await asyncio.to_thread(fonte.abrir)
            while not parar.is_set():
                frame = await asyncio.to_thread(fonte.ler_frame)
                if frame is None:
                    logger.warning("Fonte da câmera %s indisponível; encerrando captura.", camera.id)
                    break
                await asyncio.to_thread(self.processador.processar, camera, frame)
                try:
                    await asyncio.wait_for(parar.wait(), timeout=self.intervalo_s)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            logger.exception("Erro no laço de captura da câmera %s.", camera.id)
        finally:
            await asyncio.to_thread(fonte.fechar)
            self.processador.marcar_desconhecidas(camera)
            if self._tarefas.get(camera.id) is asyncio.current_task():
                del self._tarefas[camera.id]
                del self._sinais_parada[camera.id]


class SpotCalibrationService:
    """Demarcação manual das vagas sobre o frame de referência (UC08, passo 4)."""

    def __init__(self, vagas: SpotRepository, cameras: CameraRepository) -> None:
        self.vagas = vagas
        self.cameras = cameras

    def _camera_gerenciavel(self, usuario: User, camera_id: str) -> Camera:
        camera = _obter(self.cameras.buscar(camera_id), "Câmera")
        _exigir_gestao(usuario, camera.estacionamento_id)
        return camera

    def listar_vagas(self, usuario: User, camera_id: str) -> list[ParkingSpot]:
        """Vagas já demarcadas na câmera."""
        self._camera_gerenciavel(usuario, camera_id)
        return self.vagas.listar_por_camera(camera_id)

    def definir_vagas(
        self, usuario: User, camera_id: str, demarcacoes: list[tuple[str, list[Point]]]
    ) -> list[ParkingSpot]:
        """Substitui as vagas da câmera pelas demarcações (rótulo + polígono).

        Vagas com rótulo já existente na câmera mantêm o id e o status, para não
        invalidar sessões abertas nelas.
        """
        camera = self._camera_gerenciavel(usuario, camera_id)
        demarcacoes = [(_validar_texto(r, "Rótulo"), p) for r, p in demarcacoes]
        for _, poligono in demarcacoes:
            self._validar_poligono(poligono)
        atuais = {v.rotulo: v for v in self.vagas.listar_por_camera(camera_id)}
        self._validar_rotulos(
            demarcacoes, camera.estacionamento_id, ignorar={v.id for v in atuais.values()}
        )

        novas: list[ParkingSpot] = []
        for rotulo, poligono in demarcacoes:
            existente = atuais.pop(rotulo, None)
            if existente is not None:
                existente.poligono = list(poligono)
                novas.append(existente)
            else:
                novas.append(
                    ParkingSpot(
                        id=_novo_id(),
                        estacionamento_id=camera.estacionamento_id,
                        camera_id=camera_id,
                        rotulo=rotulo,
                        poligono=list(poligono),
                    )
                )
        for removida in atuais.values():
            self.vagas.remover(removida.id)
        self.vagas.salvar_varias(novas)
        return novas

    def editar_vaga(
        self,
        usuario: User,
        vaga_id: str,
        rotulo: str | None = None,
        poligono: list[Point] | None = None,
    ) -> ParkingSpot:
        """Altera uma vaga existente."""
        vaga = _obter(self.vagas.buscar(vaga_id), "Vaga")
        _exigir_gestao(usuario, vaga.estacionamento_id)
        if poligono is not None:
            self._validar_poligono(poligono)
            vaga.poligono = list(poligono)
        if rotulo is not None:
            rotulo = _validar_texto(rotulo, "Rótulo")
            self._validar_rotulos([(rotulo, vaga.poligono)], vaga.estacionamento_id, {vaga.id})
            vaga.rotulo = rotulo
        self.vagas.salvar(vaga)
        return vaga

    def remover_vaga(self, usuario: User, vaga_id: str) -> None:
        """Remove uma vaga."""
        vaga = _obter(self.vagas.buscar(vaga_id), "Vaga")
        _exigir_gestao(usuario, vaga.estacionamento_id)
        self.vagas.remover(vaga_id)

    @staticmethod
    def _validar_poligono(poligono: list[Point]) -> None:
        """Exige 4 pontos e área positiva."""
        if len(poligono) != 4:
            raise DadosInvalidos("A vaga deve ter exatamente 4 pontos.")
        teste = ParkingSpot(id="", estacionamento_id="", camera_id="", rotulo="", poligono=poligono)
        if teste.area() <= 0:
            raise DadosInvalidos("O polígono da vaga deve ter área positiva.")

    def _validar_rotulos(
        self,
        demarcacoes: list[tuple[str, list[Point]]],
        estacionamento_id: str,
        ignorar: set[str] = frozenset(),
    ) -> None:
        """Impede rótulos repetidos no mesmo estacionamento (desconsidera as vagas em ``ignorar``)."""
        rotulos = [rotulo for rotulo, _ in demarcacoes]
        repetidos = {r for r in rotulos if rotulos.count(r) > 1}
        existentes = {
            v.rotulo
            for v in self.vagas.listar_por_estacionamento(estacionamento_id)
            if v.id not in ignorar
        }
        repetidos |= set(rotulos) & existentes
        if repetidos:
            raise DadosInvalidos(f"Rótulos repetidos no estacionamento: {', '.join(sorted(repetidos))}.")


class FrameProcessor:
    """Processa um frame e atualiza o estado das vagas da câmera."""

    def __init__(
        self,
        detector: VehicleDetector,
        estrategia: OccupancyStrategy,
        vagas: SpotRepository,
        eventos: EventPublisher,
        sessoes: ParkingSessionService | None = None,
    ) -> None:
        self.detector = detector
        self.estrategia = estrategia
        self.vagas = vagas
        self.eventos = eventos
        self.sessoes = sessoes

    def processar(self, camera: Camera, frame: ndarray) -> list[SpotChange]:
        """Detecta veículos, classifica as vagas e salva/publica apenas as que mudaram."""
        deteccoes = self.detector.detectar(frame)
        vagas = self.vagas.listar_por_camera(camera.id)
        novos = self.estrategia.classificar(vagas, deteccoes)
        return self._aplicar(vagas, novos)

    def marcar_desconhecidas(self, camera: Camera) -> list[SpotChange]:
        """Marca as vagas da câmera como ``DESCONHECIDA`` (câmera parada ou fonte caiu)."""
        vagas = self.vagas.listar_por_camera(camera.id)
        return self._aplicar(vagas, {v.id: SpotStatus.DESCONHECIDA for v in vagas})

    def _aplicar(
        self, vagas: list[ParkingSpot], novos: dict[str, SpotStatus]
    ) -> list[SpotChange]:
        mudancas: list[SpotChange] = []
        for vaga in vagas:
            novo = novos.get(vaga.id)
            anterior = vaga.status
            if novo is None or not vaga.atualizar_status(novo):
                continue
            self.vagas.salvar(vaga)
            mudanca = SpotChange(
                vaga_id=vaga.id,
                estacionamento_id=vaga.estacionamento_id,
                status_anterior=anterior,
                status_novo=novo,
                timestamp=vaga.atualizado_em,
            )
            self.eventos.publicar(mudanca)
            mudancas.append(mudanca)
            if novo == SpotStatus.LIVRE and self.sessoes is not None:
                self.sessoes.vaga_liberada(vaga.id, vaga.atualizado_em)
        return mudancas


class OccupancyService:
    """Consulta do estado atual (RF03, RF05, UC01, UC02, UC05)."""

    def __init__(self, vagas: SpotRepository, estacionamentos: ParkingLotRepository) -> None:
        self.vagas = vagas
        self.estacionamentos = estacionamentos

    def _validar_estacionamento(self, estacionamento_id: str) -> None:
        estacionamento = self.estacionamentos.buscar(estacionamento_id)
        if estacionamento is None or not estacionamento.ativo:
            raise NaoEncontrado("Estacionamento não encontrado.")

    def estado_atual(self, estacionamento_id: str) -> list[ParkingSpot]:
        """Vagas com o status atual (para o mapa)."""
        self._validar_estacionamento(estacionamento_id)
        return self.vagas.listar_por_estacionamento(estacionamento_id)

    def totais(self, estacionamento_id: str) -> OccupancySnapshot:
        """Totais de vagas livres/ocupadas e taxa de ocupação."""
        return OccupancySnapshot.de_vagas(estacionamento_id, self.estado_atual(estacionamento_id))


class HistoryService:
    """Histórico e relatórios (RF04, UC06, UC07).

    O agrupamento por hora/dia/semana usa o fuso ``fuso`` (padrão: horário de Brasília).
    """

    def __init__(
        self,
        vagas: SpotRepository,
        estacionamentos: ParkingLotRepository,
        historico: HistoryRepository,
        fuso: tzinfo = FUSO_PADRAO,
    ) -> None:
        self.vagas = vagas
        self.estacionamentos = estacionamentos
        self.historico = historico
        self.fuso = fuso

    def registrar_snapshots(self) -> list[OccupancySnapshot]:
        """Grava a ocupação atual de cada estacionamento ativo (chamado periodicamente)."""
        snapshots = []
        for estacionamento in self.estacionamentos.listar(apenas_ativos=True):
            vagas = self.vagas.listar_por_estacionamento(estacionamento.id)
            snapshot = OccupancySnapshot.de_vagas(estacionamento.id, vagas)
            self.historico.salvar_snapshot(snapshot)
            snapshots.append(snapshot)
        return snapshots

    def consultar(
        self,
        usuario: User,
        estacionamento_id: str,
        inicio: datetime,
        fim: datetime,
        granularidade: str,
    ) -> list[OccupancySnapshot]:
        """Histórico agregado por ``"hora"``, ``"dia"`` ou ``"semana"``."""
        _exigir_gestao(usuario, estacionamento_id)
        if granularidade not in GRANULARIDADES:
            raise DadosInvalidos(f"Granularidade deve ser uma de: {', '.join(sorted(GRANULARIDADES))}.")
        self._validar_intervalo(inicio, fim)
        snapshots = self.historico.consultar_snapshots(estacionamento_id, inicio, fim)
        return self._agregar(snapshots, granularidade)

    def gerar_relatorio(
        self, usuario: User, estacionamento_id: str, inicio: datetime, fim: datetime
    ) -> dict:
        """Indicadores consolidados: média, pico e horário de pico da taxa de ocupação."""
        _exigir_gestao(usuario, estacionamento_id)
        self._validar_intervalo(inicio, fim)
        snapshots = self.historico.consultar_snapshots(estacionamento_id, inicio, fim)
        relatorio: dict[str, Any] = {
            "inicio": inicio,
            "fim": fim,
            "media": None,
            "pico": None,
            "horario_pico": None,
        }
        if snapshots:
            maior = max(snapshots, key=lambda s: s.taxa_ocupacao)
            relatorio["media"] = sum(s.taxa_ocupacao for s in snapshots) / len(snapshots)
            relatorio["pico"] = maior.taxa_ocupacao
            relatorio["horario_pico"] = maior.timestamp.astimezone(self.fuso)
        return relatorio

    @staticmethod
    def _validar_intervalo(inicio: datetime, fim: datetime) -> None:
        if inicio >= fim:
            raise DadosInvalidos("O início do intervalo deve ser anterior ao fim.")

    def _agregar(
        self, snapshots: list[OccupancySnapshot], granularidade: str
    ) -> list[OccupancySnapshot]:
        """Agrupa por período e calcula a média (valores arredondados para inteiros)."""
        grupos: dict[datetime, list[OccupancySnapshot]] = defaultdict(list)
        for snapshot in snapshots:
            grupos[self._inicio_periodo(snapshot.timestamp, granularidade)].append(snapshot)

        agregados = []
        for periodo in sorted(grupos):
            grupo = grupos[periodo]
            n = len(grupo)
            agregados.append(
                OccupancySnapshot(
                    timestamp=periodo,
                    estacionamento_id=grupo[0].estacionamento_id,
                    total=round(sum(s.total for s in grupo) / n),
                    livres=round(sum(s.livres for s in grupo) / n),
                    ocupadas=round(sum(s.ocupadas for s in grupo) / n),
                )
            )
        return agregados

    def _inicio_periodo(self, momento: datetime, granularidade: str) -> datetime:
        local = momento.astimezone(self.fuso).replace(minute=0, second=0, microsecond=0)
        if granularidade == "hora":
            return local
        local = local.replace(hour=0)
        if granularidade == "dia":
            return local
        return local - timedelta(days=local.weekday())  # semana começa na segunda-feira


class ParkingSessionService:
    """Registro e consulta de onde o motorista estacionou (UC03)."""

    def __init__(self, sessoes: SessionRepository, vagas: SpotRepository) -> None:
        self.sessoes = sessoes
        self.vagas = vagas

    def registrar(self, usuario: User, vaga_id: str) -> ParkingSession:
        """Encerra a sessão ativa anterior (se houver) e cria uma nova na vaga indicada.

        Só aceita vaga ``OCUPADA``, usando a detecção do modelo como confirmação.
        """
        vaga = _obter(self.vagas.buscar(vaga_id), "Vaga")
        if vaga.status != SpotStatus.OCUPADA:
            raise DadosInvalidos("A vaga indicada não está ocupada segundo a câmera.")
        anterior = self.sessoes.sessao_ativa(usuario.id)
        if anterior is not None:
            if anterior.vaga_id == vaga_id:
                return anterior
            anterior.encerrar()
            self.sessoes.salvar(anterior)
        sessao = ParkingSession(
            id=_novo_id(),
            usuario_id=usuario.id,
            vaga_id=vaga.id,
            estacionamento_id=vaga.estacionamento_id,
        )
        self.sessoes.salvar(sessao)
        return sessao

    def atual(self, usuario: User) -> tuple[ParkingSession, ParkingSpot] | None:
        """Sessão ativa e dados da vaga, para exibir no mapa com o tempo decorrido."""
        sessao = self.sessoes.sessao_ativa(usuario.id)
        if sessao is None:
            return None
        vaga = self.vagas.buscar(sessao.vaga_id)
        if vaga is None:  # vaga removida pelo admin: a sessão perde o sentido
            sessao.encerrar()
            self.sessoes.salvar(sessao)
            return None
        return sessao, vaga

    def encerrar(self, usuario: User) -> None:
        """Encerra manualmente a sessão ativa ("saí do estacionamento")."""
        sessao = _obter(self.sessoes.sessao_ativa(usuario.id), "Sessão ativa")
        sessao.encerrar()
        self.sessoes.salvar(sessao)

    def vaga_liberada(self, vaga_id: str, momento: datetime) -> None:
        """Encerra as sessões ativas em uma vaga que ficou ``LIVRE``."""
        for sessao in self.sessoes.sessoes_ativas_na_vaga(vaga_id):
            sessao.encerrar(momento)
            self.sessoes.salvar(sessao)


class AuthService:
    """Autenticação e cadastro de usuários (UC04). O JWT fica na camada ``api/``."""

    def __init__(self, usuarios: UserRepository, hasher: PasswordHasher) -> None:
        self.usuarios = usuarios
        self.hasher = hasher

    @staticmethod
    def _normalizar_email(email: str) -> str:
        return (email or "").strip().lower()

    def autenticar(self, email: str, senha: str) -> User | None:
        """Usuário se as credenciais forem válidas; ``None`` sem indicar qual campo errou.

        O motivo da recusa vai apenas para o log do servidor, nunca para a resposta.
        """
        email = self._normalizar_email(email)
        usuario = self.usuarios.buscar_por_email(email)
        if usuario is None:
            logger.info("Login recusado: e-mail %r não cadastrado.", email)
            return None
        if not self.hasher.verificar(senha, usuario.senha_hash):
            logger.info("Login recusado: senha incorreta para %r.", email)
            return None
        return usuario

    def obter(self, usuario_id: str) -> User | None:
        """Usuário pelo id (usado para validar o token de sessão)."""
        return self.usuarios.buscar(usuario_id)

    def cadastrar(
        self,
        nome: str,
        email: str,
        senha: str,
        papel: Role = Role.MOTORISTA,
        placa: str | None = None,
    ) -> User:
        """Cria um usuário com a senha já em hash (placa opcional, validada antes de salvar)."""
        placa = self._normalizar_placa(placa)
        email = self._normalizar_email(email)
        if "@" not in email:
            raise DadosInvalidos("E-mail inválido.")
        if len(senha or "") < TAMANHO_MIN_SENHA:
            raise DadosInvalidos(f"A senha deve ter ao menos {TAMANHO_MIN_SENHA} caracteres.")
        if self.usuarios.buscar_por_email(email) is not None:
            raise DadosInvalidos("E-mail já cadastrado.")
        usuario = User(
            id=_novo_id(),
            nome=_validar_texto(nome, "Nome"),
            email=email,
            senha_hash=self.hasher.gerar_hash(senha),
            papel=papel,
            placa=placa,
        )
        self.usuarios.salvar(usuario)
        return usuario

    def atualizar_placa(self, usuario_id: str, placa: str | None) -> User:
        """Normaliza e valida a placa (antiga ``AAA1234`` ou Mercosul ``AAA1A23``).

        ``None`` ou texto vazio remove a placa da conta.
        """
        usuario = _obter(self.usuarios.buscar(usuario_id), "Usuário")
        usuario.placa = self._normalizar_placa(placa)
        self.usuarios.salvar(usuario)
        return usuario

    @staticmethod
    def _normalizar_placa(placa: str | None) -> str | None:
        """Maiúsculas, sem hífen/espaços; ``None`` se vazia. Lança ``DadosInvalidos`` se inválida."""
        normalizada = re.sub(r"[\s-]", "", placa or "").upper()
        if normalizada and not _FORMATO_PLACA.match(normalizada):
            raise DadosInvalidos("Placa inválida. Use AAA1234 ou AAA1A23.")
        return normalizada or None

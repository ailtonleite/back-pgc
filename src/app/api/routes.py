"""Endpoints FastAPI.

Usa os serviços do ``Container`` (montado em ``main.py`` e guardado em
``app.state.container``) por meio de ``Depends``. A verificação de qual
estacionamento o admin gerencia fica nos serviços; aqui só se exige o perfil.

O ``Container`` precisa expor: ``auth``, ``estacionamento_service``,
``camera_manager``, ``calibracao``, ``ocupacao``, ``historico_service``,
``sessao_service``, ``eventos`` e ``config`` com ``jwt_segredo``,
``jwt_expiracao_min`` e ``cookie_seguro``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import cv2
import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.schemas import (
    AdminIn,
    CadastroIn,
    CameraIn,
    CameraOut,
    EstacionamentoIn,
    EstacionamentoOut,
    LoginIn,
    PlacaIn,
    RegistroVagaIn,
    RelatorioOut,
    SessaoOut,
    SnapshotOut,
    TotaisOut,
    UsuarioOut,
    VagaIn,
    VagaOut,
)
from app.core.models import (
    AcessoNegado,
    DadosInvalidos,
    ErroDominio,
    FonteIndisponivel,
    NaoEncontrado,
    Role,
    SpotChange,
    User,
)
from app.core.ports import EventPublisher
from app.core.services import (
    AuthService,
    CameraManager,
    HistoryService,
    OccupancyService,
    ParkingLotService,
    ParkingSessionService,
    SpotCalibrationService,
)

router = APIRouter()
logger = logging.getLogger(__name__)

COOKIE_TOKEN = "access_token"
ALGORITMO_JWT = "HS256"
INTERVALO_PING_SSE_S = 15


# ============================================================================
# Acesso ao Container e tratamento de erros
# ============================================================================


def _container(request: Request) -> Any:
    return request.app.state.container


def _auth(request: Request) -> AuthService:
    return _container(request).auth


def _estacionamentos(request: Request) -> ParkingLotService:
    return _container(request).estacionamento_service


def _cameras(request: Request) -> CameraManager:
    return _container(request).camera_manager


def _calibracao(request: Request) -> SpotCalibrationService:
    return _container(request).calibracao


def _ocupacao(request: Request) -> OccupancyService:
    return _container(request).ocupacao


def _historico(request: Request) -> HistoryService:
    return _container(request).historico_service


def _sessoes(request: Request) -> ParkingSessionService:
    return _container(request).sessao_service


def _eventos(request: Request) -> EventPublisher:
    return _container(request).eventos


_CODIGOS_HTTP: dict[type[ErroDominio], int] = {
    NaoEncontrado: status.HTTP_404_NOT_FOUND,
    AcessoNegado: status.HTTP_403_FORBIDDEN,
    DadosInvalidos: status.HTTP_422_UNPROCESSABLE_CONTENT,
    FonteIndisponivel: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def registrar_tratadores(app: FastAPI) -> None:
    """Converte as exceções de domínio em respostas HTTP (chamado em ``main.py``)."""

    async def tratar(request: Request, erro: ErroDominio) -> JSONResponse:
        codigo = _CODIGOS_HTTP.get(type(erro), status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=codigo, content={"detail": str(erro)})

    app.add_exception_handler(ErroDominio, tratar)


# ============================================================================
# Dependências de segurança
# ============================================================================


def criar_token(usuario: User, resposta: Response, config: Any) -> None:
    """Gera o JWT e o grava em cookie ``HttpOnly``."""
    expiracao = timedelta(minutes=config.jwt_expiracao_min)
    token = jwt.encode(
        {"sub": usuario.id, "exp": datetime.now(timezone.utc) + expiracao},
        config.jwt_segredo,
        algorithm=ALGORITMO_JWT,
    )
    resposta.set_cookie(
        COOKIE_TOKEN,
        token,
        max_age=int(expiracao.total_seconds()),
        httponly=True,
        secure=config.cookie_seguro,  # True em produção (HTTPS)
        samesite="lax",
    )


def usuario_atual(request: Request) -> User:
    """Lê e valida o JWT do cookie; retorna o ``User``."""
    nao_autenticado = HTTPException(status.HTTP_401_UNAUTHORIZED, "Não autenticado.")
    token = request.cookies.get(COOKIE_TOKEN)
    if not token:
        raise nao_autenticado
    try:
        dados = jwt.decode(
            token, _container(request).config.jwt_segredo, algorithms=[ALGORITMO_JWT]
        )
    except jwt.PyJWTError:
        raise nao_autenticado
    usuario = _auth(request).obter(dados.get("sub", ""))
    if usuario is None:
        raise nao_autenticado
    return usuario


def _negar(request: Request, usuario: User, mensagem: str) -> HTTPException:
    logger.warning(
        "403 em %s %s: token de %r (papel=%s).",
        request.method,
        request.url.path,
        usuario.email,
        usuario.papel.value,
    )
    return HTTPException(status.HTTP_403_FORBIDDEN, mensagem)


def exigir_admin(request: Request, usuario: Annotated[User, Depends(usuario_atual)]) -> User:
    """Erro 403 se o usuário não for ``ADMIN``."""
    if not usuario.e_admin():
        raise _negar(request, usuario, "Operação restrita a administradores.")
    return usuario


def exigir_motorista(request: Request, usuario: Annotated[User, Depends(usuario_atual)]) -> User:
    """Erro 403 se o usuário não for ``MOTORISTA``."""
    if usuario.papel != Role.MOTORISTA:
        raise _negar(request, usuario, "Operação restrita a motoristas.")
    return usuario


Autenticado = Annotated[User, Depends(usuario_atual)]
Admin = Annotated[User, Depends(exigir_admin)]
Motorista = Annotated[User, Depends(exigir_motorista)]


def _com_fuso(momento: datetime, historico: HistoryService) -> datetime:
    """Datas sem fuso na query string são interpretadas no fuso do histórico."""
    return momento if momento.tzinfo else momento.replace(tzinfo=historico.fuso)


# ============================================================================
# Autenticação e conta (UC04)
# ============================================================================


@router.post("/auth/cadastro", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED)
def cadastrar(dados: CadastroIn, auth: Annotated[AuthService, Depends(_auth)]) -> UsuarioOut:
    """Cadastro público: sempre cria um motorista."""
    usuario = auth.cadastrar(dados.nome, dados.email, dados.senha, Role.MOTORISTA, dados.placa)
    return UsuarioOut.de_entidade(usuario)


@router.post("/auth/login", response_model=UsuarioOut)
def login(
    dados: LoginIn,
    resposta: Response,
    request: Request,
    auth: Annotated[AuthService, Depends(_auth)],
) -> UsuarioOut:
    usuario = auth.autenticar(dados.email, dados.senha)
    if usuario is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou senha inválidos.")
    criar_token(usuario, resposta, _container(request).config)
    logger.info("Login: %r (papel=%s).", usuario.email, usuario.papel.value)
    return UsuarioOut.de_entidade(usuario)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(usuario: Autenticado, resposta: Response) -> None:
    resposta.delete_cookie(COOKIE_TOKEN, httponly=True, samesite="lax")
    logger.info("Logout: %r.", usuario.email)


@router.get("/me", response_model=UsuarioOut)
def me(usuario: Autenticado) -> UsuarioOut:
    return UsuarioOut.de_entidade(usuario)


@router.put("/me/placa", response_model=UsuarioOut)
def atualizar_placa(
    dados: PlacaIn, usuario: Motorista, auth: Annotated[AuthService, Depends(_auth)]
) -> UsuarioOut:
    return UsuarioOut.de_entidade(auth.atualizar_placa(usuario.id, dados.placa))


# ============================================================================
# "Onde estacionei" (UC03)
# ============================================================================


def _sessao_out(
    usuario: User, sessoes: ParkingSessionService, estacionamentos: ParkingLotService
) -> SessaoOut | None:
    atual = sessoes.atual(usuario)
    if atual is None:
        return None
    sessao, vaga = atual
    estacionamento = next(
        (e for e in estacionamentos.listar_para(usuario) if e.id == sessao.estacionamento_id), None
    )
    return SessaoOut.de_entidade(sessao, vaga, estacionamento)


@router.get("/me/sessao", response_model=SessaoOut | None)
def sessao_atual(
    usuario: Motorista,
    sessoes: Annotated[ParkingSessionService, Depends(_sessoes)],
    estacionamentos: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> SessaoOut | None:
    """Sessão ativa do motorista, ou ``null`` se não houver."""
    return _sessao_out(usuario, sessoes, estacionamentos)


@router.post("/me/sessao", response_model=SessaoOut, status_code=status.HTTP_201_CREATED)
def registrar_sessao(
    dados: RegistroVagaIn,
    usuario: Motorista,
    sessoes: Annotated[ParkingSessionService, Depends(_sessoes)],
    estacionamentos: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> SessaoOut:
    sessoes.registrar(usuario, dados.vaga_id)
    return _sessao_out(usuario, sessoes, estacionamentos)


@router.delete("/me/sessao", status_code=status.HTTP_204_NO_CONTENT)
def encerrar_sessao(
    usuario: Motorista, sessoes: Annotated[ParkingSessionService, Depends(_sessoes)]
) -> None:
    sessoes.encerrar(usuario)


# ============================================================================
# Estacionamentos (UC01, UC09)
# ============================================================================


@router.get("/estacionamentos", response_model=list[EstacionamentoOut])
def listar_estacionamentos(
    usuario: Autenticado, servico: Annotated[ParkingLotService, Depends(_estacionamentos)]
) -> list[EstacionamentoOut]:
    return [EstacionamentoOut.de_entidade(e) for e in servico.listar_para(usuario)]


@router.post(
    "/estacionamentos", response_model=EstacionamentoOut, status_code=status.HTTP_201_CREATED
)
def criar_estacionamento(
    dados: EstacionamentoIn,
    usuario: Admin,
    servico: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> EstacionamentoOut:
    estacionamento = servico.criar(usuario, dados.nome, dados.endereco)
    if dados.ativo:
        estacionamento = servico.editar(usuario, estacionamento.id, ativo=True)
    return EstacionamentoOut.de_entidade(estacionamento)


@router.put("/estacionamentos/{estacionamento_id}", response_model=EstacionamentoOut)
def editar_estacionamento(
    estacionamento_id: str,
    dados: EstacionamentoIn,
    usuario: Admin,
    servico: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> EstacionamentoOut:
    """Altera apenas os campos enviados."""
    campos = dados.model_dump(exclude_unset=True)
    return EstacionamentoOut.de_entidade(servico.editar(usuario, estacionamento_id, **campos))


@router.delete("/estacionamentos/{estacionamento_id}", status_code=status.HTTP_204_NO_CONTENT)
def remover_estacionamento(
    estacionamento_id: str,
    usuario: Admin,
    servico: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> None:
    servico.remover(usuario, estacionamento_id)


@router.post(
    "/estacionamentos/{estacionamento_id}/admins", status_code=status.HTTP_204_NO_CONTENT
)
def atribuir_admin(
    estacionamento_id: str,
    dados: AdminIn,
    usuario: Admin,
    servico: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> None:
    servico.atribuir_admin(usuario, estacionamento_id, dados.usuario_id)


@router.delete(
    "/estacionamentos/{estacionamento_id}/admins/{admin_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remover_admin(
    estacionamento_id: str,
    admin_id: str,
    usuario: Admin,
    servico: Annotated[ParkingLotService, Depends(_estacionamentos)],
) -> None:
    servico.remover_admin(usuario, estacionamento_id, admin_id)


# ============================================================================
# Ocupação em tempo real (RF03, RF05, UC01, UC02, UC05)
# ============================================================================


@router.get("/estacionamentos/{estacionamento_id}/vagas", response_model=list[VagaOut])
def estado_atual(
    estacionamento_id: str,
    _: Autenticado,
    ocupacao: Annotated[OccupancyService, Depends(_ocupacao)],
) -> list[VagaOut]:
    return [VagaOut.de_entidade(v) for v in ocupacao.estado_atual(estacionamento_id)]


@router.get("/estacionamentos/{estacionamento_id}/totais", response_model=TotaisOut)
def totais(
    estacionamento_id: str,
    _: Autenticado,
    ocupacao: Annotated[OccupancyService, Depends(_ocupacao)],
) -> TotaisOut:
    return TotaisOut.de_entidade(ocupacao.totais(estacionamento_id))


def _evento_sse(mudanca: SpotChange) -> str:
    dados = {
        "vaga_id": mudanca.vaga_id,
        "estacionamento_id": mudanca.estacionamento_id,
        "status_anterior": mudanca.status_anterior.value,
        "status_novo": mudanca.status_novo.value,
        "timestamp": mudanca.timestamp.isoformat(),
    }
    return f"event: vaga\ndata: {json.dumps(dados)}\n\n"


async def _fluxo_sse(eventos: EventPublisher, estacionamento_id: str) -> AsyncIterator[str]:
    """Repassa os eventos e envia um comentário periódico para manter a conexão aberta."""
    fila: asyncio.Queue[SpotChange] = asyncio.Queue()

    async def consumir() -> None:
        async for mudanca in eventos.inscrever(estacionamento_id):
            await fila.put(mudanca)

    consumidor = asyncio.create_task(consumir())
    try:
        yield ": conectado\n\n"
        while True:
            try:
                mudanca = await asyncio.wait_for(fila.get(), timeout=INTERVALO_PING_SSE_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _evento_sse(mudanca)
    finally:  # cliente desconectou
        consumidor.cancel()


@router.get("/estacionamentos/{estacionamento_id}/eventos")
def eventos_sse(
    estacionamento_id: str,
    _: Autenticado,
    ocupacao: Annotated[OccupancyService, Depends(_ocupacao)],
    eventos: Annotated[EventPublisher, Depends(_eventos)],
) -> StreamingResponse:
    """Mudanças de status das vagas via Server-Sent Events (evento ``vaga``)."""
    ocupacao.estado_atual(estacionamento_id)  # 404 se não existir ou estiver inativo
    return StreamingResponse(
        _fluxo_sse(eventos, estacionamento_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================================
# Histórico e relatórios (RF04, UC06, UC07)
# ============================================================================


@router.get("/estacionamentos/{estacionamento_id}/historico", response_model=list[SnapshotOut])
def historico(
    estacionamento_id: str,
    inicio: datetime,
    fim: datetime,
    usuario: Admin,
    servico: Annotated[HistoryService, Depends(_historico)],
    granularidade: Literal["hora", "dia", "semana"] = Query("hora"),
) -> list[SnapshotOut]:
    snapshots = servico.consultar(
        usuario,
        estacionamento_id,
        _com_fuso(inicio, servico),
        _com_fuso(fim, servico),
        granularidade,
    )
    return [SnapshotOut.de_entidade(s) for s in snapshots]


@router.get("/estacionamentos/{estacionamento_id}/relatorios", response_model=RelatorioOut)
def relatorio(
    estacionamento_id: str,
    inicio: datetime,
    fim: datetime,
    usuario: Admin,
    servico: Annotated[HistoryService, Depends(_historico)],
) -> RelatorioOut:
    return RelatorioOut.de_entidade(
        servico.gerar_relatorio(
            usuario, estacionamento_id, _com_fuso(inicio, servico), _com_fuso(fim, servico)
        )
    )


# ============================================================================
# Câmeras e demarcação de vagas (RF01, RF02, UC08)
# ============================================================================


@router.get("/estacionamentos/{estacionamento_id}/cameras", response_model=list[CameraOut])
def listar_cameras(
    estacionamento_id: str,
    usuario: Admin,
    cameras: Annotated[CameraManager, Depends(_cameras)],
) -> list[CameraOut]:
    return [CameraOut.de_entidade(c) for c in cameras.listar(usuario, estacionamento_id)]


@router.post(
    "/estacionamentos/{estacionamento_id}/cameras",
    response_model=CameraOut,
    status_code=status.HTTP_201_CREATED,
)
def cadastrar_camera(
    estacionamento_id: str,
    dados: CameraIn,
    usuario: Admin,
    cameras: Annotated[CameraManager, Depends(_cameras)],
) -> CameraOut:
    camera = cameras.cadastrar(
        usuario, estacionamento_id, dados.nome, dados.stream_url, dados.tipo_fonte
    )
    return CameraOut.de_entidade(camera)


@router.put("/cameras/{camera_id}", response_model=CameraOut)
async def atualizar_camera(
    camera_id: str,
    dados: CameraIn,
    usuario: Admin,
    cameras: Annotated[CameraManager, Depends(_cameras)],
) -> CameraOut:
    camera = await cameras.atualizar(usuario, camera_id, **dados.model_dump(exclude_unset=True))
    return CameraOut.de_entidade(camera)


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remover_camera(
    camera_id: str, usuario: Admin, cameras: Annotated[CameraManager, Depends(_cameras)]
) -> None:
    await cameras.remover(usuario, camera_id)


@router.get(
    "/cameras/{camera_id}/frame",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def capturar_frame(
    camera_id: str, usuario: Admin, cameras: Annotated[CameraManager, Depends(_cameras)]
) -> Response:
    """Frame de referência (JPEG) para o admin demarcar as vagas.

    Rota síncrona de propósito: a conexão com a câmera é bloqueante e roda no
    threadpool do FastAPI.
    """
    frame = cameras.capturar_frame(usuario, camera_id)
    ok, jpeg = cv2.imencode(".jpg", frame)
    if not ok:
        raise FonteIndisponivel("Não foi possível codificar o frame.")
    return Response(content=jpeg.tobytes(), media_type="image/jpeg")


@router.post("/cameras/{camera_id}/iniciar", status_code=status.HTTP_204_NO_CONTENT)
async def iniciar_monitoramento(
    camera_id: str, usuario: Admin, cameras: Annotated[CameraManager, Depends(_cameras)]
) -> None:
    await cameras.iniciar_monitoramento(usuario, camera_id)


@router.post("/cameras/{camera_id}/parar", status_code=status.HTTP_204_NO_CONTENT)
async def parar_monitoramento(
    camera_id: str, usuario: Admin, cameras: Annotated[CameraManager, Depends(_cameras)]
) -> None:
    await cameras.parar_monitoramento(usuario, camera_id)


@router.get("/cameras/{camera_id}/vagas", response_model=list[VagaOut])
def listar_vagas(
    camera_id: str,
    usuario: Admin,
    calibracao: Annotated[SpotCalibrationService, Depends(_calibracao)],
) -> list[VagaOut]:
    return [VagaOut.de_entidade(v) for v in calibracao.listar_vagas(usuario, camera_id)]


@router.put("/cameras/{camera_id}/vagas", response_model=list[VagaOut])
def definir_vagas(
    camera_id: str,
    dados: list[VagaIn],
    usuario: Admin,
    calibracao: Annotated[SpotCalibrationService, Depends(_calibracao)],
) -> list[VagaOut]:
    """Substitui todas as vagas da câmera pelas demarcações enviadas."""
    demarcacoes = [(v.rotulo, [p.para_entidade() for p in v.poligono]) for v in dados]
    return [VagaOut.de_entidade(v) for v in calibracao.definir_vagas(usuario, camera_id, demarcacoes)]

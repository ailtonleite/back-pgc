"""Fontes de vídeo (arquivo, RTSP e Kinesis Video Streams)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import cv2

from app.core.models import Camera, FonteIndisponivel
from app.core.ports import FrameSource

if TYPE_CHECKING:
    from numpy import ndarray

logger = logging.getLogger(__name__)

_EXTENSOES_IMAGEM = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class VideoFileSource(FrameSource):
    """Arquivo de vídeo ou imagem (útil para desenvolvimento e demonstração).

    Com ``tempo_real``, os frames acompanham o relógio: lendo um frame por
    segundo, o vídeo avança um segundo, e não apenas um frame.
    """

    def __init__(self, caminho: str, repetir: bool = True, tempo_real: bool = True) -> None:
        self.caminho = caminho
        self.repetir = repetir
        self.tempo_real = tempo_real
        self._captura: cv2.VideoCapture | None = None
        self._imagem: ndarray | None = None
        self._imagem_entregue = False
        self._fps = 0.0
        self._total_frames = 0
        self._posicao = 0  # índice do próximo frame a ser lido
        self._inicio = 0.0

    def abrir(self) -> None:
        self.fechar()
        if Path(self.caminho).suffix.lower() in _EXTENSOES_IMAGEM:
            self._imagem = cv2.imread(self.caminho)
            if self._imagem is None:
                raise FonteIndisponivel(f"Não foi possível abrir a imagem {self.caminho}.")
            self._imagem_entregue = False
            return
        captura = cv2.VideoCapture(self.caminho)
        if not captura.isOpened():
            captura.release()
            raise FonteIndisponivel(f"Não foi possível abrir o vídeo {self.caminho}.")
        self._captura = captura
        self._fps = captura.get(cv2.CAP_PROP_FPS) or 0.0
        self._total_frames = int(captura.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._reiniciar()

    def ler_frame(self) -> ndarray | None:
        if self._imagem is not None:
            if self._imagem_entregue and not self.repetir:
                return None
            self._imagem_entregue = True
            return self._imagem.copy()
        if self._captura is None:
            return None
        if self.tempo_real and self._fps > 0:
            alvo = int((time.monotonic() - self._inicio) * self._fps)
            if self._total_frames and alvo >= self._total_frames:
                if not self.repetir:
                    return None
                self._reiniciar()
                alvo = 0
            while self._posicao < alvo and self._captura.grab():  # pula sem decodificar
                self._posicao += 1
        ok, frame = self._captura.read()
        if not ok and self.repetir:
            self._reiniciar()
            ok, frame = self._captura.read()
        if not ok:
            return None
        self._posicao += 1
        return frame

    def fechar(self) -> None:
        if self._captura is not None:
            self._captura.release()
        self._captura = None
        self._imagem = None

    def esta_aberta(self) -> bool:
        return self._imagem is not None or (
            self._captura is not None and self._captura.isOpened()
        )

    def _reiniciar(self) -> None:
        self._captura.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._posicao = 0
        self._inicio = time.monotonic()


class _LeitorContinuo(FrameSource):
    """Base das fontes de rede.

    Uma thread lê a transmissão continuamente e guarda apenas o frame mais
    recente. Sem isso, lendo um frame por segundo, o buffer do OpenCV acumula
    e o sistema passa a processar imagens cada vez mais atrasadas.
    """

    def __init__(self, tentativas_reconexao: int = 3, timeout_s: float = 10.0) -> None:
        self.tentativas_reconexao = tentativas_reconexao
        self.timeout_s = timeout_s
        self._captura: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._parar = threading.Event()
        self._condicao = threading.Condition()
        self._frame: ndarray | None = None
        self._sequencia = 0  # frames recebidos
        self._entregue = 0  # último frame devolvido por ler_frame
        self._encerrada = True

    def _endereco(self) -> str:
        """Endereço que o OpenCV deve abrir (resolvido a cada conexão)."""
        raise NotImplementedError

    def _conectar(self) -> bool:
        timeout_ms = int(self.timeout_s * 1000)
        captura = cv2.VideoCapture(
            self._endereco(),
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms],
        )
        if not captura.isOpened():
            captura.release()
            return False
        if self._captura is not None:
            self._captura.release()
        self._captura = captura
        return True

    def abrir(self) -> None:
        self.fechar()
        try:
            conectou = self._conectar()
        except Exception as erro:
            raise FonteIndisponivel(f"Falha ao conectar à fonte: {erro}") from erro
        if not conectou:
            raise FonteIndisponivel("Não foi possível conectar à fonte de vídeo.")
        self._parar.clear()
        self._encerrada = False
        self._sequencia = self._entregue = 0
        self._thread = threading.Thread(target=self._ler_continuamente, daemon=True)
        self._thread.start()

    def _ler_continuamente(self) -> None:
        falhas = 0
        try:
            while not self._parar.is_set():
                ok, frame = self._captura.read()
                if ok:
                    falhas = 0
                    with self._condicao:
                        self._frame = frame
                        self._sequencia += 1
                        self._condicao.notify_all()
                    continue
                falhas += 1
                if falhas > self.tentativas_reconexao:
                    logger.error("Fonte sem resposta após %d tentativas.", self.tentativas_reconexao)
                    break
                logger.warning("Falha na leitura; reconectando (tentativa %d).", falhas)
                if self._parar.wait(min(2**falhas, 30)):
                    break
                try:
                    self._conectar()
                except Exception:
                    logger.exception("Erro ao reconectar.")
        finally:
            with self._condicao:
                self._encerrada = True
                self._condicao.notify_all()

    def ler_frame(self) -> ndarray | None:
        """Próximo frame ainda não entregue; ``None`` se nada chegar em ``timeout_s``."""
        with self._condicao:
            self._condicao.wait_for(
                lambda: self._sequencia > self._entregue or self._encerrada,
                timeout=self.timeout_s,
            )
            if self._sequencia <= self._entregue:
                return None
            self._entregue = self._sequencia
            return self._frame

    def fechar(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + 1)
            self._thread = None
        if self._captura is not None:
            self._captura.release()
            self._captura = None

    def esta_aberta(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._encerrada


class RtspSource(_LeitorContinuo):
    """Câmera IP via RTSP."""

    def __init__(self, url: str, tentativas_reconexao: int = 3, timeout_s: float = 10.0) -> None:
        super().__init__(tentativas_reconexao, timeout_s)
        self.url = url

    def _endereco(self) -> str:
        return self.url


class KinesisSource(_LeitorContinuo):
    """Stream do Kinesis Video Streams, lido pelo OpenCV através de uma URL HLS.

    A URL é temporária; cada reconexão obtém uma nova.
    """

    DURACAO_URL_S = 43200  # máximo permitido pela AWS (12 h)

    def __init__(
        self,
        nome_stream: str,
        regiao: str | None = None,  # None: região padrão da sessão boto3
        tentativas_reconexao: int = 3,
        timeout_s: float = 10.0,
    ) -> None:
        super().__init__(tentativas_reconexao, timeout_s)
        self.nome_stream = nome_stream
        self.regiao = regiao

    def _endereco(self) -> str:
        return self._obter_url_hls()

    def _obter_url_hls(self) -> str:
        import boto3  # dependência só necessária no modo AWS

        kvs = boto3.client("kinesisvideo", region_name=self.regiao)
        endpoint = kvs.get_data_endpoint(
            StreamName=self.nome_stream, APIName="GET_HLS_STREAMING_SESSION_URL"
        )["DataEndpoint"]
        midia = boto3.client(
            "kinesis-video-archived-media", region_name=self.regiao, endpoint_url=endpoint
        )
        return midia.get_hls_streaming_session_url(
            StreamName=self.nome_stream, PlaybackMode="LIVE", Expires=self.DURACAO_URL_S
        )["HLSStreamingSessionURL"]


def criar_fonte(camera: Camera) -> FrameSource:
    """Fábrica usada pelo ``CameraManager``: escolhe a classe conforme ``tipo_fonte``."""
    if camera.tipo_fonte == "arquivo":
        return VideoFileSource(camera.stream_url)
    if camera.tipo_fonte == "rtsp":
        return RtspSource(camera.stream_url)
    if camera.tipo_fonte == "kinesis":
        return KinesisSource(camera.stream_url)
    raise ValueError(f"tipo_fonte desconhecido: {camera.tipo_fonte}")

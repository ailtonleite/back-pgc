"""Publicação de eventos em tempo real (consumidos pela rota SSE)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.core.models import SpotChange
from app.core.ports import EventPublisher

logger = logging.getLogger(__name__)


class InMemoryEventPublisher(EventPublisher):
    """Distribui eventos para as filas dos clientes SSE de cada estacionamento.

    ``publicar`` pode ser chamado das threads de captura; a entrega é feita no
    loop da aplicação via ``call_soon_threadsafe``.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop | None = None, tamanho_fila: int = 100
    ) -> None:
        self._inscritos: dict[str, set[asyncio.Queue[SpotChange]]] = {}
        self._loop = loop  # se None, é capturado na primeira inscrição
        self._tamanho_fila = tamanho_fila

    def publicar(self, mudanca: SpotChange) -> None:
        if self._loop is None:  # ninguém se inscreveu ainda
            return
        try:
            self._loop.call_soon_threadsafe(self._entregar, mudanca)
        except RuntimeError:  # loop encerrado (desligamento da aplicação)
            pass

    def _entregar(self, mudanca: SpotChange) -> None:
        for fila in self._inscritos.get(mudanca.estacionamento_id, ()):
            try:
                fila.put_nowait(mudanca)
            except asyncio.QueueFull:
                logger.warning("Cliente SSE lento; evento da vaga %s descartado.", mudanca.vaga_id)

    async def inscrever(self, estacionamento_id: str) -> AsyncIterator[SpotChange]:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        fila: asyncio.Queue[SpotChange] = asyncio.Queue(maxsize=self._tamanho_fila)
        filas = self._inscritos.setdefault(estacionamento_id, set())
        filas.add(fila)
        try:
            while True:
                yield await fila.get()
        finally:
            filas.discard(fila)
            if not filas:
                self._inscritos.pop(estacionamento_id, None)

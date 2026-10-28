"""Cálculo de ocupação por interseção entre vagas demarcadas e detecções."""

from __future__ import annotations

from app.core.models import Detection, ParkingSpot, SpotStatus
from app.core.ports import OccupancyStrategy


class IntersectionOccupancyStrategy(OccupancyStrategy):
    """Uma vaga fica ``OCUPADA`` quando recebe ao menos uma detecção.

    Cada detecção é atribuída a uma única vaga (a de maior fração coberta), para
    que a caixa de um carro que invade a vaga vizinha não ocupe as duas.
    """

    def __init__(self, limiar: float = 0.4) -> None:
        if not 0 < limiar <= 1:
            raise ValueError("limiar deve estar em (0, 1].")
        self.limiar = limiar

    def classificar(
        self, vagas: list[ParkingSpot], deteccoes: list[Detection]
    ) -> dict[str, SpotStatus]:
        atribuidas = self._atribuir(vagas, deteccoes)
        return {
            vaga.id: SpotStatus.OCUPADA if vaga.id in atribuidas else SpotStatus.LIVRE
            for vaga in vagas
        }

    def _atribuir(
        self, vagas: list[ParkingSpot], deteccoes: list[Detection]
    ) -> dict[str, Detection]:
        """Associa cada detecção à vaga com maior fração coberta, se ``>= limiar``."""
        atribuidas: dict[str, Detection] = {}
        melhor_fracao: dict[str, float] = {}
        if not vagas:
            return atribuidas
        for deteccao in deteccoes:
            fracao, vaga = max(
                ((v.fracao_coberta(deteccao.poligono), v) for v in vagas),
                key=lambda par: par[0],
            )
            if fracao >= self.limiar and fracao > melhor_fracao.get(vaga.id, 0.0):
                atribuidas[vaga.id] = deteccao
                melhor_fracao[vaga.id] = fracao
        return atribuidas

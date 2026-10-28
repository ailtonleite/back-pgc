"""Detector de veículos com YOLO11-OBB (Ultralytics)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ultralytics import YOLO

from app.core.models import Detection, Point
from app.core.ports import VehicleDetector

if TYPE_CHECKING:
    from numpy import ndarray
    from ultralytics.engine.results import Results


class YoloObbDetector(VehicleDetector):
    """Detector baseado no modelo treinado ``car_only_pklot_carpk_acpds``."""

    def __init__(
        self,
        caminho_pesos: str,
        conf: float = 0.25,
        imgsz: int = 1024,
        device: str | int | None = None,  # None: o Ultralytics escolhe (GPU se houver)
    ) -> None:
        self.caminho_pesos = caminho_pesos
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        self._modelo = YOLO(caminho_pesos, task="obb")
        # O predictor do Ultralytics não é thread-safe e cada câmera roda em sua thread.
        self._trava = threading.Lock()

    def detectar(self, frame: ndarray) -> list[Detection]:
        with self._trava:
            resultados = self._modelo.predict(
                frame, conf=self.conf, imgsz=self.imgsz, device=self.device, verbose=False
            )
        return self._converter(resultados[0])

    @staticmethod
    def _converter(resultado: Results) -> list[Detection]:
        """Converte ``result.obb.xyxyxyxy`` do Ultralytics em objetos ``Detection``."""
        obb = resultado.obb
        if obb is None or len(obb) == 0:
            return []
        cantos = obb.xyxyxyxy.cpu().numpy()  # (N, 4, 2)
        confiancas = obb.conf.cpu().numpy()
        classes = obb.cls.cpu().numpy().astype(int)
        return [
            Detection(
                poligono=[Point(float(x), float(y)) for x, y in caixa],
                confianca=float(confianca),
                classe=resultado.names[classe],
            )
            for caixa, confianca, classe in zip(cantos, confiancas, classes)
        ]

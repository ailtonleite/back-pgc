"""Hash de senhas."""

from __future__ import annotations

import base64
import hashlib

import bcrypt

from app.core.ports import PasswordHasher


class BcryptPasswordHasher(PasswordHasher):
    """Hash com bcrypt.

    A senha passa antes por SHA-256 (em base64), pois o bcrypt só aceita até
    72 bytes; assim senhas longas não são truncadas nem rejeitadas.
    """

    def __init__(self, rodadas: int = 12) -> None:
        self.rodadas = rodadas

    @staticmethod
    def _preparar(senha: str) -> bytes:
        return base64.b64encode(hashlib.sha256(senha.encode("utf-8")).digest())

    def gerar_hash(self, senha: str) -> str:
        return bcrypt.hashpw(self._preparar(senha), bcrypt.gensalt(self.rodadas)).decode("ascii")

    def verificar(self, senha: str, senha_hash: str) -> bool:
        try:
            return bcrypt.checkpw(self._preparar(senha), senha_hash.encode("ascii"))
        except ValueError:  # hash malformado
            return False

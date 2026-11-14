#!/usr/bin/env bash
# Inicia o back-end em modo local (dados em memória), criando o admin inicial.
#
# Uso (de qualquer pasta):
#   ./iniciar-backend.sh                # porta 8000, recarrega ao salvar o código
#   PORTA=9000 ./iniciar-backend.sh     # outra porta
#   ./iniciar-backend.sh --no-reload    # argumentos extras vão para o uvicorn
#
# As credenciais abaixo podem ser sobrescritas pelo ambiente:
#   APP_ADMIN_SENHA='outra-senha' ./iniciar-backend.sh
#
# Atenção: no modo local tudo fica em memória. Ao reiniciar (ou ao recarregar
# depois de salvar um arquivo), os dados somem e o admin é recriado.

set -euo pipefail

# --- Credenciais do admin inicial (somente para desenvolvimento local) -------
export APP_ADMIN_EMAIL="${APP_ADMIN_EMAIL:-admin@ufabc.br}"
export APP_ADMIN_SENHA="${APP_ADMIN_SENHA:-Ufabc@2k26}"
export APP_ADMIN_NOME="${APP_ADMIN_NOME:-Administrador}"

PORTA="${PORTA:-8000}"

# --- Validações -------------------------------------------------------------
if [ "${#APP_ADMIN_SENHA}" -lt 8 ]; then
    echo "Erro: APP_ADMIN_SENHA precisa ter ao menos 8 caracteres." >&2
    exit 1
fi

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Python: .venv do projeto, se existir; senão o launcher do Windows (py) ou python3.
if [ -x "$RAIZ/.venv/Scripts/python.exe" ]; then
    PYTHON=("$RAIZ/.venv/Scripts/python.exe")
elif [ -x "$RAIZ/.venv/bin/python" ]; then
    PYTHON=("$RAIZ/.venv/bin/python")
elif command -v py >/dev/null 2>&1; then
    PYTHON=(py)
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=(python3)
else
    echo "Erro: Python não encontrado (procurei .venv, py e python3)." >&2
    exit 1
fi

if ! "${PYTHON[@]}" -c "import uvicorn, fastapi, sqlalchemy, bcrypt, jwt, pydantic_settings" 2>/dev/null; then
    echo "Erro: dependências ausentes. Instale com:" >&2
    echo "  ${PYTHON[*]} -m pip install -r \"$RAIZ/requirements.txt\"" >&2
    exit 1
fi

# --- Inicialização ----------------------------------------------------------
cd "$RAIZ/src"
echo "Admin inicial: $APP_ADMIN_EMAIL"
echo "API:           http://127.0.0.1:$PORTA  (documentação em /docs)"
echo

RECARREGAR=(--reload)
for arg in "$@"; do
    [ "$arg" = "--no-reload" ] && RECARREGAR=()
done
EXTRAS=()
for arg in "$@"; do
    [ "$arg" != "--no-reload" ] && EXTRAS+=("$arg")
done

exec "${PYTHON[@]}" -m uvicorn app.main:criar_app --factory \
    --host 127.0.0.1 --port "$PORTA" \
    ${RECARREGAR[@]+"${RECARREGAR[@]}"} ${EXTRAS[@]+"${EXTRAS[@]}"}

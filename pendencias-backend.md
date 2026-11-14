# Pendências do back-end

Ajustes no back-end (`TCC-back`) identificados durante a implementação do front-end. Inclui os itens da seção 12 do `frontend.md` e os encontrados ao escrever o código.

Para cada item: o problema, o impacto no front, a sugestão de correção e o contorno adotado no front enquanto não houver correção.

## Resumo

| # | Pendência | Prioridade | Contorno no front |
|---|---|---|---|
| 1 | `PUT /estacionamentos/{id}` exige `nome` | Alta | Sim |
| 2 | CORS em produção | Alta (produção) | Não há |
| 3 | Cookie entre domínios em produção | Alta (produção) | Não há |
| 4 | Salvar vagas recria os ids e quebra as sessões | Média | Aviso ao salvar |
| 5 | Admin não vê o mapa de estacionamento inativo | Média | Oculta o mapa |
| 6 | Nome da câmera no mapa do motorista | Média | Abas numeradas |
| 7 | Atribuição de admin por e-mail | Média | Campo de id |
| 8 | Listar os admins de um estacionamento | Média | Não lista |
| 9 | Falta `GET /cameras/{id}` | Baixa | Parâmetro na URL |
| 10 | Falta `GET /estacionamentos/{id}` | Baixa | Lista tudo e filtra |
| 11 | Polígonos que se cruzam são corrigidos em silêncio | Baixa | Não há |

---

## Prioridade alta

### 1. `PUT /estacionamentos/{id}` exige `nome`

**Problema.** A rota usa `EstacionamentoIn`, em que `nome: str` é obrigatório. A docstring da rota diz "altera apenas os campos enviados", e o serviço já trata isso: `editar` só valida `nome` se ele vier nos campos. Mas o Pydantic recusa a requisição antes, com 422, quando o `nome` não é enviado.

**Impacto.** O switch "Ativo" da tela de gerenciamento envia só `{ ativo }` (seções 3.3 e 7.6 do `frontend.md`) e recebe 422.

**Sugestão.** Um modelo próprio para a edição, com todos os campos opcionais:

```python
class EstacionamentoEdicaoIn(BaseModel):
    nome: str | None = None
    endereco: str | None = None
    ativo: bool | None = None
```

O `POST` continua usando `EstacionamentoIn`, com `nome` obrigatório.

**Contorno no front.** `GerenciarEstacionamentosView` envia `{ nome, ativo }`. Isso funciona com o back-end atual e continua funcionando depois da correção.

### 2. CORS em produção

**Problema.** Em desenvolvimento, o proxy do Vite deixa front e API na mesma origem. Em produção, eles ficam em origens diferentes, e as requisições usam credenciais (cookie).

**Sugestão.** `CORSMiddleware` com `allow_origins=[<URL do front>]` e `allow_credentials=True`. Nunca use `*`, que o navegador recusa quando há credenciais.

### 3. Cookie entre domínios em produção

**Problema.** O cookie `access_token` é criado com `SameSite=lax`. Assim, ele só é enviado quando front e API são do mesmo *site*.

**Sugestão.** Publicar os dois em subdomínios do mesmo domínio (`app.` e `api.`). Se não for possível, usar `SameSite=None; Secure`.

---

## Prioridade média

### 4. Salvar as vagas recria os ids e quebra as sessões

**Problema.** `PUT /cameras/{id}/vagas` substitui todas as vagas da câmera e gera ids novos. As sessões ativas de motoristas apontam para o `vaga_id` antigo, que deixa de existir. Isso acontece mesmo quando o admin só renomeia uma vaga ou ajusta um vértice.

**Sugestão.** Aceitar um `id` opcional em `VagaIn`: as vagas com `id` são atualizadas, as sem `id` são criadas e as ausentes são removidas. Alternativa mínima: encerrar as sessões das vagas removidas, para nenhuma ficar órfã.

**Contorno no front.** O editor já guarda o id do servidor de cada vaga existente (`chave`). Ele pode enviá-lo assim que o back-end aceitar. Por enquanto, a `DemarcacaoView` avisa sobre a perda de referência antes de salvar, quando a câmera está em monitoramento.

### 5. Admin não vê o mapa de estacionamento inativo

**Problema.** `OccupancyService._validar_estacionamento` responde 404 para estacionamento inativo em `/vagas`, `/totais` e `/eventos`, inclusive para o admin responsável.

**Impacto.** O admin não consegue conferir o mapa e a detecção antes de liberar o estacionamento para os motoristas.

**Sugestão.** Permitir o acesso a estacionamentos inativos para o admin responsável por eles.

**Contorno no front.** Para estacionamentos inativos, a `EstacionamentosView` não busca os totais nem mostra "Ver mapa". O admin continua acessando as câmeras e o histórico.

### 6. Nome da câmera no mapa do motorista

**Problema.** O motorista não acessa `GET /estacionamentos/{id}/cameras`, que é exclusiva de admin.

**Impacto.** As abas do mapa aparecem como "Câmera 1, 2…" para o motorista.

**Sugestão.** Incluir `camera_nome` em `VagaOut`.

### 7. Atribuição de admin por e-mail

**Problema.** `AdminIn` exige o `usuario_id`, e o admin não tem como descobrir o id de outro usuário.

**Sugestão.** Aceitar `email` em `AdminIn`, ou criar `GET /usuarios?email=` (somente admin).

**Contorno no front.** O `AdminsDialog` pede o id diretamente.

### 8. Listar os admins de um estacionamento

**Problema.** Não há rota que liste os responsáveis por um estacionamento.

**Impacto.** O `AdminsDialog` não mostra quem já é responsável, o que dificulta a remoção.

**Sugestão.** `GET /estacionamentos/{id}/admins`.

---

## Prioridade baixa

### 9. Falta `GET /cameras/{id}`

**Problema.** A tela de demarcação (`/admin/cameras/:id/vagas`) só conhece o id da câmera. Para saber o nome, o estacionamento e se ela está em monitoramento, precisaria de uma rota que devolvesse a câmera.

**Sugestão.** `GET /cameras/{id}` retornando `CameraOut`.

**Contorno no front.** A `CamerasView` passa `?estacionamento=<id>` na URL. A `DemarcacaoView` então lista as câmeras desse estacionamento e procura a atual. Sem o parâmetro (acesso direto pela URL), a tela funciona, mas não mostra o aviso de câmera em monitoramento do item 4.

### 10. Falta `GET /estacionamentos/{id}`

**Problema.** Não há rota para buscar um único estacionamento.

**Contorno no front.** As telas de mapa, câmeras e histórico chamam `GET /estacionamentos` e filtram pelo id para mostrar o nome no título.

**Sugestão.** `GET /estacionamentos/{id}` retornando `EstacionamentoOut`.

### 11. Polígonos que se cruzam são corrigidos em silêncio

**Problema.** `_para_poligono` (`core/models.py`) aplica `buffer(0)` quando o polígono é inválido, por exemplo quando os vértices formam um "laço". O polígono salvo passa a ser diferente do que o admin desenhou. Além disso, o front valida a área pela fórmula do laço (*shoelace*), que pode dar um resultado diferente do shapely nesses casos.

**Sugestão.** Em `_validar_poligono`, recusar polígonos que se cruzam (`not poligono.is_valid`) com uma mensagem clara, em vez de corrigi-los. Assim o front pode aplicar a mesma regra no editor.

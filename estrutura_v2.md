# Especificação de classes do back-end

**Projeto:** Desenvolvimento de sistema de controle de estacionamento utilizando visão computacional (TCC — UFABC)
**Arquitetura:** Ports and Adapters (hexagonal), versão simplificada
**Linguagem/framework:** Python 3 + FastAPI
**Versão:** 2 (17/10/2026)

### Alterações em relação à versão 1

- **Estacionamento como entidade.** Foi criada a classe `ParkingLot`. Câmeras, vagas e histórico passam a pertencer a um estacionamento, e cada administrador é responsável por um ou mais estacionamentos.
- **Vagas marcadas manualmente.** As vagas são demarcadas pelo administrador em cada câmera. A ocupação é decidida pela interseção entre a vaga marcada e a caixa de detecção do veículo. Foram removidos o classificador de ocupação (`ClassifierOccupancyStrategy`) e o `SpotCropper`.
- **Placa cadastrada pelo usuário.** Não há leitura automática de placas. Foram removidos `PlateReader`, `EasyOcrPlateReader` e `PlateReading`.
- **UC03 reformulado.** O UC03 passou a se chamar "Registrar e consultar onde estacionei". O motorista indica no mapa a vaga em que parou, e o sistema guarda a vaga e o horário. A nova entidade `ParkingSession` substitui a `PlateReading`. A leitura automática de placas fica registrada como trabalho futuro.
- **Arquivo de visão renomeado.** O arquivo `adapters/vision.py` voltou a se chamar `yolo.py`, já que agora contém apenas o detector.

---

## 1. Visão geral

```
backend/
├── app/
│   ├── core/
│   │   ├── models.py      # entidades de domínio e exceções
│   │   ├── ports.py       # interfaces abstratas (portas)
│   │   └── services.py    # regras de negócio
│   ├── adapters/
│   │   ├── yolo.py        # detector de veículos (YOLO11-OBB)
│   │   ├── occupancy.py   # cálculo de ocupação por interseção
│   │   ├── database.py    # repositórios (memória, DynamoDB, PostgreSQL)
│   │   ├── camera.py      # fontes de vídeo (arquivo, RTSP, Kinesis)
│   │   ├── events.py      # publicação de eventos em tempo real (SSE)
│   │   └── security.py    # hash de senhas
│   ├── api/
│   │   ├── routes.py      # endpoints FastAPI
│   │   └── schemas.py     # modelos Pydantic de entrada/saída
│   └── main.py            # monta o sistema (conecta adaptadores às portas)
├── tests/
├── Dockerfile
└── requirements.txt
```

### Regra de dependência

```
api/  ──►  core/  ◄──  adapters/
             ▲
          main.py  (único lugar que conhece todos)
```

- `core/` **não importa nada** de `adapters/` nem de `api/`.
- `core/services.py` recebe as dependências **pelo construtor**, sempre tipadas pelas interfaces de `core/ports.py`.
- `main.py` decide quais adaptadores concretos são usados (ex.: repositório em memória nos testes, DynamoDB em produção).

### Modelo de dados (relacionamentos)

```
ParkingLot 1 ──── N Camera 1 ──── N ParkingSpot
     N                                  1
     │ (responsável)                    │
     N                                  N
   User (ADMIN)          User 1 ──── N ParkingSession
```

### Convenções deste documento

- Atributos iniciados com `_` são internos (não fazem parte da interface pública).
- `frame` é sempre uma imagem `numpy.ndarray` no formato BGR (padrão do OpenCV).
- Coordenadas de polígonos são em **pixels** da imagem da câmera.

---

## 2. `core/models.py` — Entidades de domínio

Classes de dados puras (`@dataclass`). Não acessam banco, rede ou modelo de IA. A única dependência externa permitida é uma biblioteca de geometria (ex.: `shapely`) para as operações com polígonos.

### 2.1 `SpotStatus` (Enum)

Estado de ocupação de uma vaga.

| Valor | Significado |
|---|---|
| `LIVRE` | Vaga disponível |
| `OCUPADA` | Vaga com veículo |
| `DESCONHECIDA` | Sem informação suficiente (câmera inativa ou vaga recém-cadastrada) |

### 2.2 `Role` (Enum)

Perfil de acesso do usuário.

| Valor | Significado |
|---|---|
| `ADMIN` | Administrador de um ou mais estacionamentos |
| `MOTORISTA` | Usuário motorista |

### 2.3 `Point` (dataclass, imutável)

| Atributo | Tipo | Descrição |
|---|---|---|
| `x` | `float` | Coordenada horizontal em pixels |
| `y` | `float` | Coordenada vertical em pixels |

### 2.4 `ParkingLot`

Estacionamento. Agrupa câmeras e vagas e é a unidade de consulta do motorista e de gestão do administrador.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `nome` | `str` | Ex.: "UFABC — Bloco A" |
| `endereco` | `str \| None` | Endereço (opcional) |
| `ativo` | `bool` | Se aparece para os motoristas |
| `criado_em` | `datetime` | Data de cadastro |

| Método | Retorno | Descrição |
|---|---|---|
| `ativar()` | `None` | Torna o estacionamento visível aos motoristas |
| `desativar()` | `None` | Oculta o estacionamento dos motoristas |

### 2.5 `Camera`

Câmera cadastrada em um estacionamento.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `estacionamento_id` | `str` | Estacionamento ao qual pertence |
| `nome` | `str` | Nome amigável (ex.: "Entrada principal") |
| `stream_url` | `str` | Endereço da transmissão (arquivo, RTSP ou nome do stream Kinesis) |
| `tipo_fonte` | `str` | `"arquivo"`, `"rtsp"` ou `"kinesis"` |
| `ativa` | `bool` | Se está sendo monitorada |
| `criada_em` | `datetime` | Data de cadastro |

| Método | Retorno | Descrição |
|---|---|---|
| `ativar()` | `None` | Marca a câmera como ativa |
| `desativar()` | `None` | Marca a câmera como inativa |

### 2.6 `ParkingSpot`

Vaga demarcada manualmente pelo administrador sobre a imagem de uma câmera.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `estacionamento_id` | `str` | Estacionamento (redundante com a câmera, mas evita consultas extras e serve de chave no DynamoDB) |
| `camera_id` | `str` | Câmera que enxerga a vaga |
| `rotulo` | `str` | Rótulo exibido no mapa (ex.: "A-12") |
| `poligono` | `list[Point]` | Quatro cantos da vaga na imagem, marcados pelo administrador |
| `status` | `SpotStatus` | Estado atual (inicia como `DESCONHECIDA`) |
| `atualizado_em` | `datetime` | Momento da última mudança de status |

| Método | Retorno | Descrição |
|---|---|---|
| `area()` | `float` | Área do polígono em pixels² |
| `fracao_coberta(poligono: list[Point])` | `float` | Fração da área da vaga coberta por outro polígono (0 a 1). É a medida usada para decidir a ocupação |
| `atualizar_status(novo: SpotStatus)` | `bool` | Atualiza o status e o `atualizado_em`; retorna `True` **somente se o status mudou** |

### 2.7 `Detection`

Resultado de uma detecção de veículo, independente do formato do Ultralytics.

| Atributo | Tipo | Descrição |
|---|---|---|
| `poligono` | `list[Point]` | Caixa orientada (OBB) com 4 cantos |
| `confianca` | `float` | Confiança do modelo (0 a 1) |
| `classe` | `str` | Classe detectada (atualmente sempre `"car"`) |

### 2.8 `SpotChange`

Evento gerado quando uma vaga muda de estado. É o que o front-end recebe via SSE.

| Atributo | Tipo | Descrição |
|---|---|---|
| `vaga_id` | `str` | Vaga afetada |
| `estacionamento_id` | `str` | Estacionamento da vaga (permite ao front filtrar eventos) |
| `status_anterior` | `SpotStatus` | Estado antes da mudança |
| `status_novo` | `SpotStatus` | Estado depois da mudança |
| `timestamp` | `datetime` | Momento da mudança |

### 2.9 `OccupancySnapshot`

Fotografia agregada da ocupação de um estacionamento em um instante. É a unidade do histórico (RF03, RF04).

| Atributo | Tipo | Descrição |
|---|---|---|
| `timestamp` | `datetime` | Momento do registro |
| `estacionamento_id` | `str` | Estacionamento do registro |
| `total` | `int` | Total de vagas monitoradas |
| `livres` | `int` | Vagas livres |
| `ocupadas` | `int` | Vagas ocupadas |

| Método | Retorno | Descrição |
|---|---|---|
| `taxa_ocupacao` (propriedade) | `float` | `ocupadas / total` (0 se `total == 0`) |
| `de_vagas(estacionamento_id, vagas)` (classmethod) | `OccupancySnapshot` | Cria o snapshot contando os status de uma lista de vagas |

### 2.10 `User`

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `nome` | `str` | Nome do usuário |
| `email` | `str` | Login |
| `senha_hash` | `str` | Hash da senha (nunca a senha em texto puro) |
| `papel` | `Role` | Perfil de acesso |
| `placa` | `str \| None` | Placa informada pelo próprio usuário (opcional) |
| `estacionamentos` | `list[str]` | Ids dos estacionamentos sob responsabilidade do usuário (somente admin; vazia para motorista) |

| Método | Retorno | Descrição |
|---|---|---|
| `e_admin()` | `bool` | Se o papel é `ADMIN` |
| `pode_gerenciar(estacionamento_id)` | `bool` | `True` se for admin **e** responsável pelo estacionamento |

> **LGPD:** como a placa é informada pelo próprio usuário, com o seu consentimento, e o sistema não captura placas de terceiros nas imagens, o tratamento de dados pessoais fica restrito ao cadastro da conta.

### 2.11 `ParkingSession`

Registro de onde o motorista estacionou (UC03). É criado pelo próprio motorista ao indicar a vaga no mapa.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `usuario_id` | `str` | Motorista |
| `vaga_id` | `str` | Vaga indicada |
| `estacionamento_id` | `str` | Estacionamento da vaga |
| `inicio` | `datetime` | Momento do registro |
| `fim` | `datetime \| None` | Momento em que a sessão foi encerrada (`None` enquanto ativa) |

| Método | Retorno | Descrição |
|---|---|---|
| `ativa` (propriedade) | `bool` | `True` enquanto `fim` for `None` |
| `encerrar(momento)` | `None` | Preenche `fim` |
| `duracao(agora)` | `timedelta` | Tempo decorrido desde o `inicio` (até `fim`, se encerrada) |

### 2.12 Exceções de domínio

Lançadas pelos serviços e traduzidas em códigos HTTP pela camada `api/`.

| Classe | Uso | HTTP |
|---|---|---|
| `NaoEncontrado` | Entidade inexistente | 404 |
| `AcessoNegado` | Usuário sem permissão (ex.: admin de outro estacionamento) | 403 |
| `DadosInvalidos` | Regra de validação violada (polígono inválido, placa em formato errado) | 422 |

---

## 3. `core/ports.py` — Interfaces (portas)

Todas são classes abstratas (`abc.ABC`) com métodos `@abstractmethod`. Não possuem atributos; definem apenas **o que** o núcleo precisa, não **como** é feito.

### 3.1 `VehicleDetector`

| Método | Retorno | Descrição |
|---|---|---|
| `detectar(frame)` | `list[Detection]` | Detecta veículos em um frame |

### 3.2 `OccupancyStrategy`

Recebe **todas as vagas de uma câmera de uma vez**. Isso é necessário para que cada detecção seja atribuída a uma única vaga (ver 5.2).

| Método | Retorno | Descrição |
|---|---|---|
| `classificar(vagas: list[ParkingSpot], deteccoes: list[Detection])` | `dict[str, SpotStatus]` | Status calculado para cada vaga, indexado pelo id |

> Mesmo com uma única implementação, a interface é mantida como ponto de extensão: um classificador de ocupação por recorte de vaga pode ser adicionado no futuro sem alterar os serviços.

### 3.3 `FrameSource`

| Método | Retorno | Descrição |
|---|---|---|
| `abrir()` | `None` | Abre a conexão com a fonte |
| `ler_frame()` | `ndarray \| None` | Próximo frame; `None` se a fonte terminou ou falhou |
| `fechar()` | `None` | Libera a conexão |
| `esta_aberta()` | `bool` | Se a fonte está disponível |

### 3.4 `ParkingLotRepository`

| Método | Retorno | Descrição |
|---|---|---|
| `listar(apenas_ativos=False)` | `list[ParkingLot]` | Estacionamentos |
| `listar_por_ids(ids)` | `list[ParkingLot]` | Estacionamentos de um admin |
| `buscar(estacionamento_id)` | `ParkingLot \| None` | Estacionamento pelo id |
| `salvar(estacionamento)` | `None` | Cria ou atualiza |
| `remover(estacionamento_id)` | `None` | Remove |

### 3.5 `CameraRepository`

| Método | Retorno | Descrição |
|---|---|---|
| `listar_por_estacionamento(estacionamento_id)` | `list[Camera]` | Câmeras de um estacionamento |
| `listar_ativas()` | `list[Camera]` | Câmeras em monitoramento (usado na subida da aplicação) |
| `buscar(camera_id)` | `Camera \| None` | Câmera pelo id |
| `salvar(camera)` | `None` | Cria ou atualiza |
| `remover(camera_id)` | `None` | Remove a câmera |

### 3.6 `SpotRepository`

Estado atual das vagas (em produção: DynamoDB).

| Método | Retorno | Descrição |
|---|---|---|
| `listar_por_camera(camera_id)` | `list[ParkingSpot]` | Vagas de uma câmera |
| `listar_por_estacionamento(estacionamento_id)` | `list[ParkingSpot]` | Vagas de um estacionamento |
| `buscar(vaga_id)` | `ParkingSpot \| None` | Vaga pelo id |
| `salvar(vaga)` | `None` | Cria ou atualiza uma vaga |
| `salvar_varias(vagas)` | `None` | Salva em lote (usado na demarcação) |
| `remover(vaga_id)` | `None` | Remove uma vaga |
| `remover_por_camera(camera_id)` | `None` | Remove todas as vagas de uma câmera |

### 3.7 `HistoryRepository`

Histórico de ocupação (em produção: PostgreSQL/RDS).

| Método | Retorno | Descrição |
|---|---|---|
| `salvar_snapshot(snapshot)` | `None` | Registra um snapshot de ocupação |
| `consultar_snapshots(estacionamento_id, inicio, fim)` | `list[OccupancySnapshot]` | Snapshots em um intervalo |
| `expurgar_anteriores_a(data)` | `int` | Remove registros antigos; retorna a quantidade removida |

### 3.8 `SessionRepository`

Registros de "onde estacionei" (em produção: PostgreSQL/RDS).

| Método | Retorno | Descrição |
|---|---|---|
| `salvar(sessao)` | `None` | Cria ou atualiza |
| `sessao_ativa(usuario_id)` | `ParkingSession \| None` | Sessão em aberto do usuário |
| `sessoes_ativas_na_vaga(vaga_id)` | `list[ParkingSession]` | Sessões em aberto em uma vaga (para encerramento automático) |
| `expurgar_anteriores_a(data)` | `int` | Remove sessões antigas |

### 3.9 `UserRepository`

| Método | Retorno | Descrição |
|---|---|---|
| `buscar(usuario_id)` | `User \| None` | Usuário pelo id |
| `buscar_por_email(email)` | `User \| None` | Usuário pelo login |
| `salvar(usuario)` | `None` | Cria ou atualiza |

### 3.10 `PasswordHasher`

| Método | Retorno | Descrição |
|---|---|---|
| `gerar_hash(senha)` | `str` | Gera o hash da senha |
| `verificar(senha, hash)` | `bool` | Confere a senha com o hash |

### 3.11 `EventPublisher`

| Método | Retorno | Descrição |
|---|---|---|
| `publicar(mudanca: SpotChange)` | `None` | Envia um evento a todos os inscritos |
| `inscrever(estacionamento_id)` | `AsyncIterator[SpotChange]` | Fluxo assíncrono de eventos de um estacionamento (consumido pela rota SSE) |

---

## 4. `core/services.py` — Regras de negócio

Cada serviço recebe suas dependências no construtor (injeção de dependência). Os atributos listados são essas dependências, mais o estado interno quando houver.

Os métodos que recebem `usuario: User` verificam a permissão antes de agir e lançam `AcessoNegado` quando o administrador não é responsável pelo estacionamento envolvido.

### 4.1 `ParkingLotService`

Cadastro de estacionamentos e responsabilidade dos administradores.

| Atributo | Tipo | Descrição |
|---|---|---|
| `estacionamentos` | `ParkingLotRepository` | Persistência dos estacionamentos |
| `usuarios` | `UserRepository` | Para vincular administradores |

| Método | Retorno | Descrição |
|---|---|---|
| `criar(usuario, nome, endereco=None)` | `ParkingLot` | Cria o estacionamento e torna o criador responsável por ele |
| `editar(usuario, estacionamento_id, **campos)` | `ParkingLot` | Altera nome, endereço ou status ativo |
| `remover(usuario, estacionamento_id)` | `None` | Remove o estacionamento (somente sem câmeras cadastradas) |
| `listar_para(usuario)` | `list[ParkingLot]` | Admin: os estacionamentos sob sua responsabilidade. Motorista: todos os ativos |
| `atribuir_admin(usuario, estacionamento_id, admin_id)` | `None` | Torna outro administrador responsável pelo estacionamento |
| `remover_admin(usuario, estacionamento_id, admin_id)` | `None` | Retira a responsabilidade (impede remover o último responsável) |

> **Primeiro acesso:** o primeiro administrador é criado por um script de inicialização (*seed*). A partir dele, os demais estacionamentos e administradores são cadastrados pelo sistema.

### 4.2 `CameraManager`

Cadastro de câmeras e controle dos laços de captura (UC08).

| Atributo | Tipo | Descrição |
|---|---|---|
| `cameras` | `CameraRepository` | Persistência das câmeras |
| `vagas` | `SpotRepository` | Para remover vagas junto com a câmera e marcá-las como `DESCONHECIDA` |
| `processador` | `FrameProcessor` | Processa os frames capturados |
| `fabrica_fonte` | `Callable[[Camera], FrameSource]` | Cria a fonte adequada ao `tipo_fonte` da câmera |
| `intervalo_s` | `float` | Intervalo entre frames processados (ex.: 1 s) |
| `_tarefas` | `dict[str, Task]` | Laços de captura em execução, por câmera |

| Método | Retorno | Descrição |
|---|---|---|
| `cadastrar(usuario, estacionamento_id, nome, stream_url, tipo_fonte)` | `Camera` | Cria a câmera (inativa) no estacionamento |
| `atualizar(usuario, camera_id, **campos)` | `Camera` | Edita parâmetros (UC08, fluxo A1) |
| `remover(usuario, camera_id)` | `None` | Para o monitoramento e remove a câmera e suas vagas (UC08, fluxo A2) |
| `capturar_frame(usuario, camera_id)` | `ndarray` | Captura um frame de referência para o admin demarcar as vagas; lança erro se a conexão falhar (UC08, fluxo E1) |
| `iniciar_monitoramento(usuario, camera_id)` | `None` | Ativa a câmera e inicia seu laço de captura |
| `parar_monitoramento(usuario, camera_id)` | `None` | Encerra o laço, desativa a câmera e marca suas vagas como `DESCONHECIDA` |
| `iniciar_todas()` | `None` | Inicia as câmeras ativas (chamado na subida da aplicação, sem verificação de usuário) |
| `_laco_captura(camera)` | `None` | Lê frames em loop e chama `processador.processar`; marca as vagas como `DESCONHECIDA` se a fonte cair |

### 4.3 `SpotCalibrationService`

Demarcação manual das vagas sobre o frame de referência da câmera (UC08, passo 4).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Persistência das vagas |
| `cameras` | `CameraRepository` | Para obter o estacionamento da câmera e validar a permissão |

| Método | Retorno | Descrição |
|---|---|---|
| `listar_vagas(usuario, camera_id)` | `list[ParkingSpot]` | Vagas já demarcadas na câmera (para edição) |
| `definir_vagas(usuario, camera_id, demarcacoes: list[tuple[str, list[Point]]])` | `list[ParkingSpot]` | Substitui as vagas da câmera pelas demarcações (rótulo + polígono) |
| `editar_vaga(usuario, vaga_id, rotulo=None, poligono=None)` | `ParkingSpot` | Altera uma vaga existente |
| `remover_vaga(usuario, vaga_id)` | `None` | Remove uma vaga |
| `_validar_poligono(poligono)` | `None` | Exige 4 pontos e área positiva; lança `DadosInvalidos` caso contrário |
| `_validar_rotulos(demarcacoes, estacionamento_id)` | `None` | Impede rótulos repetidos dentro do mesmo estacionamento |

### 4.4 `FrameProcessor`

Coração do sistema: processa um frame e atualiza o estado das vagas da câmera.

| Atributo | Tipo | Descrição |
|---|---|---|
| `detector` | `VehicleDetector` | Detector de veículos |
| `estrategia` | `OccupancyStrategy` | Cálculo de ocupação por interseção |
| `vagas` | `SpotRepository` | Estado das vagas |
| `eventos` | `EventPublisher` | Notificação em tempo real |
| `sessoes` | `ParkingSessionService \| None` | Opcional; se informado, encerra sessões de vagas que ficaram livres |

| Método | Retorno | Descrição |
|---|---|---|
| `processar(camera, frame)` | `list[SpotChange]` | Detecta veículos, calcula o status de todas as vagas da câmera, salva e publica **apenas as vagas que mudaram** |

Fluxo de `processar`:

1. `deteccoes = detector.detectar(frame)`
2. `vagas = vagas.listar_por_camera(camera.id)`
3. `novos = estrategia.classificar(vagas, deteccoes)`
4. Para cada vaga cujo `atualizar_status(novos[vaga.id])` retornar `True`: salvar, publicar o `SpotChange` e, se a vaga ficou `LIVRE`, avisar o `ParkingSessionService`.

### 4.5 `OccupancyService`

Consulta do estado atual (RF03, RF05, UC01, UC02, UC05).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Estado das vagas |
| `estacionamentos` | `ParkingLotRepository` | Para validar se o estacionamento existe e está ativo |

| Método | Retorno | Descrição |
|---|---|---|
| `estado_atual(estacionamento_id)` | `list[ParkingSpot]` | Vagas com o status atual (para o mapa) |
| `totais(estacionamento_id)` | `OccupancySnapshot` | Totais de vagas livres/ocupadas e taxa de ocupação |

### 4.6 `HistoryService`

Histórico e relatórios (RF04, UC06, UC07).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Para gerar os snapshots |
| `estacionamentos` | `ParkingLotRepository` | Estacionamentos a registrar |
| `historico` | `HistoryRepository` | Persistência histórica |

| Método | Retorno | Descrição |
|---|---|---|
| `registrar_snapshots()` | `list[OccupancySnapshot]` | Grava a ocupação atual de cada estacionamento ativo; chamado periodicamente (ex.: a cada 5 min) |
| `consultar(usuario, estacionamento_id, inicio, fim, granularidade)` | `list[OccupancySnapshot]` | Histórico agregado por `"hora"`, `"dia"` ou `"semana"` |
| `gerar_relatorio(usuario, estacionamento_id, inicio, fim)` | `dict` | Indicadores consolidados: média, pico e horário de pico da ocupação |
| `_agregar(snapshots, granularidade)` | `list[OccupancySnapshot]` | Agrupa e calcula a média por período |

### 4.7 `ParkingSessionService`

Registro e consulta de onde o motorista estacionou (UC03, reformulado).

| Atributo | Tipo | Descrição |
|---|---|---|
| `sessoes` | `SessionRepository` | Persistência das sessões |
| `vagas` | `SpotRepository` | Validação da vaga indicada |

| Método | Retorno | Descrição |
|---|---|---|
| `registrar(usuario, vaga_id)` | `ParkingSession` | Encerra a sessão ativa anterior (se houver) e cria uma nova na vaga indicada. Só aceita vaga com status `OCUPADA`, usando a detecção do modelo como confirmação |
| `atual(usuario)` | `tuple[ParkingSession, ParkingSpot] \| None` | Sessão ativa e dados da vaga (rótulo e estacionamento), para exibir no mapa com o tempo decorrido |
| `encerrar(usuario)` | `None` | Encerra manualmente a sessão ativa ("saí do estacionamento") |
| `vaga_liberada(vaga_id, momento)` | `None` | Chamado pelo `FrameProcessor` quando a vaga fica `LIVRE`; encerra as sessões ativas nela |

### 4.8 `AuthService`

Autenticação e cadastro de usuários (UC04). A emissão do token JWT fica na camada `api/`, por ser um detalhe de HTTP.

| Atributo | Tipo | Descrição |
|---|---|---|
| `usuarios` | `UserRepository` | Persistência dos usuários |
| `hasher` | `PasswordHasher` | Hash de senhas |

| Método | Retorno | Descrição |
|---|---|---|
| `autenticar(email, senha)` | `User \| None` | Retorna o usuário se as credenciais forem válidas; `None` caso contrário, sem indicar qual campo errou (UC04, fluxo A1) |
| `cadastrar(nome, email, senha, papel=Role.MOTORISTA)` | `User` | Cria um usuário com a senha já em hash |
| `atualizar_placa(usuario_id, placa)` | `User` | Normaliza (maiúsculas, sem hífen) e valida os formatos Mercosul (`AAA1A23`) e antigo (`AAA1234`); lança `DadosInvalidos` se inválida |

---

## 5. `adapters/` — Implementações concretas

Cada classe implementa uma porta de `core/ports.py`. Métodos herdados da porta não são repetidos na descrição; apenas os atributos e os métodos auxiliares são detalhados.

### 5.1 `adapters/yolo.py`

#### `YoloObbDetector` (implementa `VehicleDetector`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `caminho_pesos` | `str` | Caminho do `car_only_pklot_carpk_acpds.pt` (`model/`) |
| `conf` | `float` | Confiança mínima (padrão 0,25; ajustável por causa da oclusão parcial) |
| `imgsz` | `int` | Tamanho de entrada (1024, o mesmo do treino) |
| `device` | `str \| int` | `0` (GPU) ou `"cpu"` |
| `_modelo` | `ultralytics.YOLO` | Modelo carregado uma única vez no construtor |

| Método | Retorno | Descrição |
|---|---|---|
| `detectar(frame)` | `list[Detection]` | Executa a inferência |
| `_converter(resultado)` | `list[Detection]` | Converte `result.obb.xyxyxyxy` do Ultralytics em objetos `Detection` |

### 5.2 `adapters/occupancy.py`

#### `IntersectionOccupancyStrategy` (implementa `OccupancyStrategy`)

Decide a ocupação pela interseção entre a vaga demarcada e as caixas de detecção.

| Atributo | Tipo | Descrição |
|---|---|---|
| `limiar` | `float` | Fração mínima da área da vaga que precisa estar coberta para considerá-la ocupada (ex.: 0,4) |

| Método | Retorno | Descrição |
|---|---|---|
| `classificar(vagas, deteccoes)` | `dict[str, SpotStatus]` | Status de cada vaga |
| `_atribuir(vagas, deteccoes)` | `dict[str, Detection]` | Associa cada detecção à **única** vaga com maior fração coberta, desde que acima do `limiar` |

Algoritmo:

1. Para cada detecção, calcular `vaga.fracao_coberta(deteccao.poligono)` em todas as vagas da câmera.
2. Atribuir a detecção somente à vaga de maior fração, se ela for maior ou igual ao `limiar`.
3. Vagas que receberam alguma detecção ficam `OCUPADA`; as demais, `LIVRE`.

> **Por que fração coberta e não IoU:** a caixa do carro e o polígono da vaga têm tamanhos diferentes, e a IoU fica baixa mesmo com o carro bem estacionado. **Por que uma vaga por detecção:** em câmeras oblíquas, a caixa de um carro invade a vaga vizinha; sem essa regra, um único carro marcaria duas vagas como ocupadas.

### 5.3 `adapters/database.py`

#### Repositórios em memória (testes e desenvolvimento local)

`InMemoryParkingLotRepository`, `InMemoryCameraRepository`, `InMemorySpotRepository`, `InMemoryHistoryRepository`, `InMemorySessionRepository` e `InMemoryUserRepository` implementam as respectivas portas.

| Atributo | Tipo | Descrição |
|---|---|---|
| `_dados` | `dict[str, objeto]` | Armazenamento por id (no histórico: lista de snapshots) |

Não possuem métodos além dos definidos na porta.

#### `DynamoSpotRepository` (implementa `SpotRepository`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `nome_tabela` | `str` | Tabela do DynamoDB (chave de partição: `estacionamento_id`; chave de ordenação: `vaga_id`) |
| `_tabela` | `boto3 Table` | Recurso da tabela |

| Método | Retorno | Descrição |
|---|---|---|
| `_para_item(vaga)` | `dict` | Converte `ParkingSpot` em item do DynamoDB |
| `_de_item(item)` | `ParkingSpot` | Converte item do DynamoDB em `ParkingSpot` |

#### Repositórios PostgreSQL

`PostgresParkingLotRepository`, `PostgresCameraRepository`, `PostgresHistoryRepository`, `PostgresSessionRepository` e `PostgresUserRepository` seguem o mesmo padrão. A responsabilidade dos administradores (`User.estacionamentos`) é armazenada em uma tabela de associação `usuario_estacionamento`.

| Atributo | Tipo | Descrição |
|---|---|---|
| `_engine` | `sqlalchemy.Engine` | Conexão compartilhada com o RDS |

| Método | Retorno | Descrição |
|---|---|---|
| `_de_linha(linha)` | entidade | Conversão de linha do banco em entidade de domínio |

Função auxiliar `criar_tabelas(engine)`: cria as tabelas se não existirem.

### 5.4 `adapters/camera.py`

Todas implementam `FrameSource`.

#### `VideoFileSource`

| Atributo | Tipo | Descrição |
|---|---|---|
| `caminho` | `str` | Arquivo de vídeo ou imagem |
| `repetir` | `bool` | Reinicia ao fim do vídeo (útil para demonstração) |
| `_captura` | `cv2.VideoCapture` | Leitor do OpenCV |

#### `RtspSource`

| Atributo | Tipo | Descrição |
|---|---|---|
| `url` | `str` | Endereço RTSP da câmera IP |
| `tentativas_reconexao` | `int` | Quantas vezes tenta reconectar antes de desistir |
| `_captura` | `cv2.VideoCapture` | Leitor do OpenCV |

#### `KinesisSource`

| Atributo | Tipo | Descrição |
|---|---|---|
| `nome_stream` | `str` | Stream do Kinesis Video Streams |
| `regiao` | `str` | Região da AWS |
| `_captura` | `cv2.VideoCapture` | Leitor aberto sobre a URL HLS |

| Método | Retorno | Descrição |
|---|---|---|
| `_obter_url_hls()` | `str` | Obtém uma URL HLS temporária (`get_hls_streaming_session_url`) para leitura com o OpenCV |

#### Função `criar_fonte(camera: Camera) -> FrameSource`

Fábrica usada pelo `CameraManager`: escolhe a classe conforme `camera.tipo_fonte`.

### 5.5 `adapters/events.py`

#### `InMemoryEventPublisher` (implementa `EventPublisher`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `_inscritos` | `dict[str, set[asyncio.Queue]]` | Filas dos clientes SSE conectados, agrupadas por estacionamento |
| `_loop` | `asyncio.AbstractEventLoop` | Loop da aplicação, usado para publicar a partir das threads de captura |

| Método | Retorno | Descrição |
|---|---|---|
| `publicar(mudanca)` | `None` | Coloca o evento nas filas do estacionamento correspondente (via `call_soon_threadsafe`, pois a captura roda fora do loop assíncrono) |
| `inscrever(estacionamento_id)` | `AsyncIterator[SpotChange]` | Cria uma fila, entrega os eventos e a remove quando o cliente desconecta |

### 5.6 `adapters/security.py`

#### `BcryptPasswordHasher` (implementa `PasswordHasher`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `rodadas` | `int` | Custo do bcrypt (ex.: 12) |

---

## 6. `api/` — Camada HTTP

### 6.1 `api/schemas.py` — Modelos Pydantic

Todos herdam de `pydantic.BaseModel` e não têm métodos próprios. Os modelos `...Out` possuem um `classmethod` `de_entidade(...)` para converter a entidade de domínio.

| Classe | Campos | Uso |
|---|---|---|
| `LoginIn` | `email`, `senha` | Corpo do login |
| `CadastroIn` | `nome`, `email`, `senha`, `placa` (opcional) | Cadastro de motorista |
| `UsuarioOut` | `id`, `nome`, `email`, `papel`, `placa` | Dados do usuário logado (sem senha) |
| `PlacaIn` | `placa` | Atualização da placa |
| `EstacionamentoIn` | `nome`, `endereco` | Cadastro/edição de estacionamento |
| `EstacionamentoOut` | `id`, `nome`, `endereco`, `ativo` | Resposta |
| `AdminIn` | `usuario_id` | Atribuição de administrador |
| `CameraIn` | `nome`, `stream_url`, `tipo_fonte` | Cadastro/edição de câmera |
| `CameraOut` | `id`, `estacionamento_id`, `nome`, `tipo_fonte`, `ativa` | Resposta (não expõe `stream_url` ao motorista) |
| `PontoIn` | `x`, `y` | Ponto de um polígono |
| `VagaIn` | `rotulo`, `poligono: list[PontoIn]` | Demarcação de vaga |
| `VagaOut` | `id`, `camera_id`, `rotulo`, `poligono`, `status`, `atualizado_em` | Estado da vaga para o mapa |
| `TotaisOut` | `total`, `livres`, `ocupadas`, `taxa_ocupacao` | Contadores |
| `SnapshotOut` | `timestamp`, `livres`, `ocupadas`, `taxa_ocupacao` | Ponto do histórico |
| `RelatorioOut` | `inicio`, `fim`, `media`, `pico`, `horario_pico` | Relatório consolidado |
| `RegistroVagaIn` | `vaga_id` | Registro de "estacionei aqui" |
| `SessaoOut` | `vaga_id`, `vaga_rotulo`, `estacionamento_id`, `estacionamento_nome`, `inicio`, `tempo_estacionado_min` | Resposta do UC03 |

### 6.2 `api/routes.py` — Endpoints

Arquivo de funções (rotas FastAPI). Não define classes; usa os serviços por meio de `Depends`. As exceções de domínio são convertidas em respostas HTTP por *exception handlers*.

**Dependências de segurança:**

| Função | Descrição |
|---|---|
| `criar_token(usuario)` | Gera o JWT e o grava em cookie `HttpOnly` |
| `usuario_atual(request)` | Lê e valida o JWT do cookie; retorna o `User` |
| `exigir_admin(usuario)` | Retorna erro 403 se o usuário não for `ADMIN` |

> A verificação de **qual** estacionamento o admin gerencia fica nos serviços (`pode_gerenciar`), e não nas rotas, para que a regra de negócio seja testável sem HTTP.

**Endpoints:**

| Método | Rota | Perfil | Serviço | Rastreabilidade |
|---|---|---|---|---|
| POST | `/auth/cadastro` | público | `AuthService.cadastrar` | UC04 |
| POST | `/auth/login` | público | `AuthService.autenticar` | UC04 |
| POST | `/auth/logout` | autenticado | — | UC04 |
| GET | `/me` | autenticado | — | UC04 |
| PUT | `/me/placa` | motorista | `AuthService.atualizar_placa` | — |
| GET | `/me/sessao` | motorista | `ParkingSessionService.atual` | UC03 |
| POST | `/me/sessao` | motorista | `ParkingSessionService.registrar` | UC03 |
| DELETE | `/me/sessao` | motorista | `ParkingSessionService.encerrar` | UC03 |
| GET | `/estacionamentos` | autenticado | `ParkingLotService.listar_para` | UC01 |
| POST | `/estacionamentos` | admin | `ParkingLotService.criar` | UC09 |
| PUT/DELETE | `/estacionamentos/{id}` | admin | `ParkingLotService.editar` / `remover` | UC09 |
| POST/DELETE | `/estacionamentos/{id}/admins` | admin | `ParkingLotService.atribuir_admin` / `remover_admin` | UC09 |
| GET | `/estacionamentos/{id}/vagas` | autenticado | `OccupancyService.estado_atual` | RF05, UC02 |
| GET | `/estacionamentos/{id}/totais` | autenticado | `OccupancyService.totais` | RF03, UC01, UC05 |
| GET | `/estacionamentos/{id}/eventos` | autenticado | `EventPublisher.inscrever` (SSE) | RF05, UC01, UC05 |
| GET | `/estacionamentos/{id}/historico` | admin | `HistoryService.consultar` | RF04, UC06 |
| GET | `/estacionamentos/{id}/relatorios` | admin | `HistoryService.gerar_relatorio` | RF04, UC07 |
| GET/POST | `/estacionamentos/{id}/cameras` | admin | `CameraManager` | RF01, UC08 |
| PUT/DELETE | `/cameras/{id}` | admin | `CameraManager.atualizar` / `remover` | UC08 |
| GET | `/cameras/{id}/frame` | admin | `CameraManager.capturar_frame` | UC08 |
| POST | `/cameras/{id}/iniciar` e `/parar` | admin | `CameraManager` | RF01, UC08 |
| GET/PUT | `/cameras/{id}/vagas` | admin | `SpotCalibrationService.listar_vagas` / `definir_vagas` | RF02, UC08 |

---

## 7. `main.py` — Montagem do sistema

### 7.1 `Container`

Único ponto que conhece todas as implementações concretas. Instancia os adaptadores, injeta-os nos serviços e expõe tudo para as rotas.

| Atributo | Tipo | Descrição |
|---|---|---|
| `config` | `Settings` | Configurações lidas de variáveis de ambiente (`pydantic-settings`): caminho dos pesos, `conf`, `limiar` de ocupação, DSN do banco, tabela DynamoDB, segredo JWT, modo (`"local"` ou `"aws"`) |
| `detector` | `VehicleDetector` | `YoloObbDetector` |
| `estrategia` | `OccupancyStrategy` | `IntersectionOccupancyStrategy` |
| `repositorios` | — | `estacionamentos`, `cameras`, `vagas`, `historico`, `sessoes`, `usuarios` (em memória no modo local; DynamoDB/PostgreSQL no modo AWS) |
| `eventos` | `EventPublisher` | `InMemoryEventPublisher` |
| `servicos` | — | `estacionamento_service`, `camera_manager`, `calibracao`, `frame_processor`, `ocupacao`, `historico_service`, `sessao_service`, `auth` |

| Método | Retorno | Descrição |
|---|---|---|
| `__init__(config)` | — | Cria todos os objetos na ordem adaptadores → serviços |
| `_criar_repositorios()` | `None` | Escolhe a implementação conforme `config.modo` |

### 7.2 Função `criar_app() -> FastAPI`

Cria o `Container`, registra as rotas e os *exception handlers*, e no evento de inicialização (`lifespan`) inicia as câmeras ativas e o agendamento do `HistoryService.registrar_snapshots`. Ao desligar, encerra os laços de captura.

---

## 8. Testes (`tests/`)

| Tipo | O que testar | Adaptadores usados |
|---|---|---|
| Unitário | Métodos de `models.py` (`fracao_coberta`, `atualizar_status`, `pode_gerenciar`, `ParkingSession.duracao`) | Nenhum |
| Unitário | `IntersectionOccupancyStrategy` com polígonos conhecidos, incluindo o caso do carro que invade a vaga vizinha | Nenhum |
| Unitário | `FrameProcessor`, `OccupancyService`, `HistoryService`, `ParkingSessionService`, `AuthService` | Repositórios em memória + um `FakeDetector` que devolve detecções fixas |
| Unitário | Permissões: admin tentando gerenciar estacionamento de outro admin deve receber `AcessoNegado` | Repositórios em memória |
| Integração | Pipeline completo com vídeo real e vagas demarcadas | `YoloObbDetector` + `VideoFileSource` |
| Integração | Rotas da API (login, permissões, vagas, SSE) | `TestClient` do FastAPI + repositórios em memória |

O `FakeDetector` é um exemplo direto do benefício da arquitetura: como `FrameProcessor` depende apenas de `VehicleDetector`, a regra de negócio é testada sem GPU e sem o modelo YOLO.

---

## 9. Rastreabilidade: classes × requisitos

| Requisito / caso de uso | Classes principais |
|---|---|
| RF01 — captura em tempo real | `FrameSource` e implementações, `CameraManager` |
| RF02 — classificar vagas | `SpotCalibrationService` (demarcação manual), `VehicleDetector`, `IntersectionOccupancyStrategy`, `FrameProcessor` |
| RF03 — totais de vagas | `OccupancyService`, `OccupancySnapshot` |
| RF04 — histórico diário | `HistoryService`, `HistoryRepository` |
| RF05 — mapa em tempo real | `OccupancyService`, `EventPublisher`, `SpotChange` |
| RNF01 — precisão ≥ 90% | `YoloObbDetector` + `IntersectionOccupancyStrategy` (avaliados no conjunto de teste) |
| RNF03 — segurança | `AuthService`, `PasswordHasher`, `User.pode_gerenciar`, `exigir_admin` |
| RNF04 — HTTPS | Infraestrutura (ALB + ACM); fora das classes Python |
| UC03 — registrar e consultar onde estacionei | `ParkingSession`, `ParkingSessionService`, `SessionRepository` |
| UC08 — gerenciar câmeras e vagas | `CameraManager`, `SpotCalibrationService` |
| UC09 — gerenciar estacionamentos (novo) | `ParkingLot`, `ParkingLotService`, `ParkingLotRepository` |

---

## 10. Trabalhos futuros previstos pela arquitetura

| Evolução | Onde entra |
|---|---|
| Classificador de ocupação por recorte de vaga | Nova implementação de `OccupancyStrategy` |
| Detecção automática das vagas (agrupamento de detecções) | Novo serviço que gera demarcações para o `SpotCalibrationService` |
| Leitura automática de placas (OCR) | Nova porta `PlateReader` e preenchimento automático da `ParkingSession` |
| Separar inferência e API em serviços distintos | Novo adaptador de `VehicleDetector` que chama o serviço de inferência por HTTP |
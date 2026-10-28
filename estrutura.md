# Especificação de classes do back-end

**Projeto:** Desenvolvimento de sistema de controle de estacionamento utilizando visão computacional (TCC — UFABC)
**Arquitetura:** Ports and Adapters (hexagonal), versão simplificada
**Linguagem/framework:** Python 3 + FastAPI

---

## 1. Visão geral

```
backend/
├── app/
│   ├── core/
│   │   ├── models.py      # entidades de domínio
│   │   ├── ports.py       # interfaces abstratas (portas)
│   │   └── services.py    # regras de negócio
│   ├── adapters/
│   │   ├── vision.py      # detector YOLO + leitor de placa
│   │   ├── occupancy.py   # estratégias de ocupação (Rota 1 e Rota 2)
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

> Em relação à estrutura combinada anteriormente, `yolo.py` passou a se chamar `vision.py` (passa a abrigar também o leitor de placa) e foram incluídos `events.py` e `security.py`, para que nenhuma tecnologia externa precise ser importada dentro de `core/`.

### Regra de dependência

```
api/  ──►  core/  ◄──  adapters/
             ▲
          main.py  (único lugar que conhece todos)
```

- `core/` **não importa nada** de `adapters/` nem de `api/`.
- `core/services.py` recebe as dependências **pelo construtor**, sempre tipadas pelas interfaces de `core/ports.py`.
- `main.py` decide quais adaptadores concretos são usados (ex.: repositório em memória nos testes, DynamoDB em produção).

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
| `DESCONHECIDA` | Sem informação suficiente (câmera inativa, oclusão, vaga recém-cadastrada) |

### 2.2 `Role` (Enum)

Perfil de acesso do usuário.

| Valor | Significado |
|---|---|
| `ADMIN` | Administrador do estacionamento |
| `MOTORISTA` | Usuário motorista |

### 2.3 `Point` (dataclass, imutável)

| Atributo | Tipo | Descrição |
|---|---|---|
| `x` | `float` | Coordenada horizontal em pixels |
| `y` | `float` | Coordenada vertical em pixels |

### 2.4 `Camera`

Câmera cadastrada no sistema.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `nome` | `str` | Nome amigável (ex.: "Bloco A — entrada") |
| `stream_url` | `str` | Endereço da transmissão (arquivo, RTSP ou nome do stream Kinesis) |
| `tipo_fonte` | `str` | `"arquivo"`, `"rtsp"` ou `"kinesis"` |
| `ativa` | `bool` | Se está sendo monitorada |
| `criada_em` | `datetime` | Data de cadastro |

| Método | Retorno | Descrição |
|---|---|---|
| `ativar()` | `None` | Marca a câmera como ativa |
| `desativar()` | `None` | Marca a câmera como inativa |

### 2.5 `ParkingSpot`

Vaga monitorada, demarcada sobre a imagem de uma câmera.

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `camera_id` | `str` | Câmera que enxerga a vaga |
| `rotulo` | `str` | Rótulo exibido no mapa (ex.: "A-12") |
| `poligono` | `list[Point]` | Quatro cantos da vaga na imagem |
| `status` | `SpotStatus` | Estado atual (inicia como `DESCONHECIDA`) |
| `atualizado_em` | `datetime` | Momento da última mudança de status |

| Método | Retorno | Descrição |
|---|---|---|
| `area()` | `float` | Área do polígono em pixels² |
| `contem(ponto: Point)` | `bool` | Se o ponto está dentro da vaga |
| `fracao_coberta(poligono: list[Point])` | `float` | Fração da área da vaga coberta por outro polígono (0 a 1); usada pela Rota 1 |
| `atualizar_status(novo: SpotStatus)` | `bool` | Atualiza o status e o `atualizado_em`; retorna `True` **somente se o status mudou** |

### 2.6 `Detection`

Resultado de uma detecção de veículo, independente do formato do Ultralytics.

| Atributo | Tipo | Descrição |
|---|---|---|
| `poligono` | `list[Point]` | Caixa orientada (OBB) com 4 cantos |
| `confianca` | `float` | Confiança do modelo (0 a 1) |
| `classe` | `str` | Classe detectada (atualmente sempre `"car"`) |

| Método | Retorno | Descrição |
|---|---|---|
| `centro()` | `Point` | Centro geométrico da caixa |

### 2.7 `SpotChange`

Evento gerado quando uma vaga muda de estado. É o que o front-end recebe via SSE.

| Atributo | Tipo | Descrição |
|---|---|---|
| `vaga_id` | `str` | Vaga afetada |
| `camera_id` | `str` | Câmera de origem |
| `status_anterior` | `SpotStatus` | Estado antes da mudança |
| `status_novo` | `SpotStatus` | Estado depois da mudança |
| `timestamp` | `datetime` | Momento da mudança |

### 2.8 `OccupancySnapshot`

Fotografia agregada da ocupação em um instante. É a unidade do histórico (RF03, RF04).

| Atributo | Tipo | Descrição |
|---|---|---|
| `timestamp` | `datetime` | Momento do registro |
| `camera_id` | `str \| None` | Câmera específica, ou `None` para o estacionamento inteiro |
| `total` | `int` | Total de vagas monitoradas |
| `livres` | `int` | Vagas livres |
| `ocupadas` | `int` | Vagas ocupadas |

| Método | Retorno | Descrição |
|---|---|---|
| `taxa_ocupacao` (propriedade) | `float` | `ocupadas / total` (0 se `total == 0`) |
| `de_vagas(vagas, camera_id=None)` (classmethod) | `OccupancySnapshot` | Cria o snapshot contando os status de uma lista de vagas |

### 2.9 `User`

| Atributo | Tipo | Descrição |
|---|---|---|
| `id` | `str` | Identificador único |
| `nome` | `str` | Nome do usuário |
| `email` | `str` | Login |
| `senha_hash` | `str` | Hash da senha (nunca a senha em texto puro) |
| `papel` | `Role` | Perfil de acesso |
| `placa` | `str \| None` | Placa vinculada à conta (UC03) |

| Método | Retorno | Descrição |
|---|---|---|
| `e_admin()` | `bool` | Se o papel é `ADMIN` |

### 2.10 `PlateReading`

Registro de uma placa identificada em uma vaga (UC03).

| Atributo | Tipo | Descrição |
|---|---|---|
| `placa` | `str` | Placa normalizada (ex.: `"ABC1D23"`) |
| `vaga_id` | `str` | Vaga em que o veículo foi visto |
| `camera_id` | `str` | Câmera de origem |
| `timestamp` | `datetime` | Momento da leitura |

> **LGPD:** leituras de placa devem ter prazo de retenção definido (expurgo pela função Lambda) e só podem ser consultadas pelo dono da placa.

---

## 3. `core/ports.py` — Interfaces (portas)

Todas são classes abstratas (`abc.ABC`) com métodos `@abstractmethod`. Não possuem atributos; definem apenas **o que** o núcleo precisa, não **como** é feito.

### 3.1 `VehicleDetector`

| Método | Retorno | Descrição |
|---|---|---|
| `detectar(frame)` | `list[Detection]` | Detecta veículos em um frame |

### 3.2 `OccupancyStrategy`

| Método | Retorno | Descrição |
|---|---|---|
| `classificar(frame, vaga: ParkingSpot, deteccoes: list[Detection])` | `SpotStatus` | Decide se a vaga está livre ou ocupada |

### 3.3 `PlateReader`

| Método | Retorno | Descrição |
|---|---|---|
| `ler(frame, deteccao: Detection)` | `str \| None` | Lê a placa do veículo detectado; `None` se não conseguir |

### 3.4 `FrameSource`

| Método | Retorno | Descrição |
|---|---|---|
| `abrir()` | `None` | Abre a conexão com a fonte |
| `ler_frame()` | `ndarray \| None` | Próximo frame; `None` se a fonte terminou ou falhou |
| `fechar()` | `None` | Libera a conexão |
| `esta_aberta()` | `bool` | Se a fonte está disponível |

### 3.5 `CameraRepository`

| Método | Retorno | Descrição |
|---|---|---|
| `listar()` | `list[Camera]` | Todas as câmeras |
| `buscar(camera_id)` | `Camera \| None` | Câmera pelo id |
| `salvar(camera)` | `None` | Cria ou atualiza |
| `remover(camera_id)` | `None` | Remove a câmera |

### 3.6 `SpotRepository`

Estado atual das vagas (em produção: DynamoDB).

| Método | Retorno | Descrição |
|---|---|---|
| `listar_por_camera(camera_id)` | `list[ParkingSpot]` | Vagas de uma câmera |
| `listar_todas()` | `list[ParkingSpot]` | Todas as vagas |
| `buscar(vaga_id)` | `ParkingSpot \| None` | Vaga pelo id |
| `salvar(vaga)` | `None` | Cria ou atualiza uma vaga |
| `salvar_varias(vagas)` | `None` | Salva em lote (usado na calibração) |
| `remover(vaga_id)` | `None` | Remove uma vaga |
| `remover_por_camera(camera_id)` | `None` | Remove todas as vagas de uma câmera |

### 3.7 `HistoryRepository`

Dados históricos (em produção: PostgreSQL/RDS).

| Método | Retorno | Descrição |
|---|---|---|
| `salvar_snapshot(snapshot)` | `None` | Registra um snapshot de ocupação |
| `consultar_snapshots(inicio, fim, camera_id=None)` | `list[OccupancySnapshot]` | Snapshots em um intervalo |
| `salvar_leitura_placa(leitura)` | `None` | Registra uma leitura de placa |
| `ultima_leitura(placa)` | `PlateReading \| None` | Leitura mais recente de uma placa |
| `expurgar_anteriores_a(data)` | `int` | Remove registros antigos; retorna a quantidade removida |

### 3.8 `UserRepository`

| Método | Retorno | Descrição |
|---|---|---|
| `buscar(usuario_id)` | `User \| None` | Usuário pelo id |
| `buscar_por_email(email)` | `User \| None` | Usuário pelo login |
| `salvar(usuario)` | `None` | Cria ou atualiza |

### 3.9 `PasswordHasher`

| Método | Retorno | Descrição |
|---|---|---|
| `gerar_hash(senha)` | `str` | Gera o hash da senha |
| `verificar(senha, hash)` | `bool` | Confere a senha com o hash |

### 3.10 `EventPublisher`

| Método | Retorno | Descrição |
|---|---|---|
| `publicar(mudanca: SpotChange)` | `None` | Envia um evento a todos os inscritos |
| `inscrever()` | `AsyncIterator[SpotChange]` | Fluxo assíncrono de eventos (consumido pela rota SSE) |

---

## 4. `core/services.py` — Regras de negócio

Cada serviço recebe suas dependências no construtor (injeção de dependência). Os atributos listados são exatamente essas dependências, mais o estado interno quando houver.

### 4.1 `FrameProcessor`

Coração do sistema: processa um frame e atualiza o estado das vagas.

| Atributo | Tipo | Descrição |
|---|---|---|
| `detector` | `VehicleDetector` | Detector de veículos |
| `estrategia` | `OccupancyStrategy` | Rota 1 ou Rota 2 |
| `vagas` | `SpotRepository` | Estado das vagas |
| `eventos` | `EventPublisher` | Notificação em tempo real |
| `leitor_placa` | `PlateReader \| None` | Opcional; se `None`, não lê placas |
| `historico` | `HistoryRepository \| None` | Necessário só se houver leitura de placa |

| Método | Retorno | Descrição |
|---|---|---|
| `processar(camera, frame)` | `list[SpotChange]` | Detecta veículos, classifica cada vaga da câmera, salva e publica **apenas as vagas que mudaram** |
| `_registrar_placas(camera, frame, deteccoes, vagas)` | `None` | Para cada vaga que passou a `OCUPADA`, tenta ler a placa e grava um `PlateReading` |

### 4.2 `CameraManager`

Cadastro de câmeras e controle dos laços de captura (UC08).

| Atributo | Tipo | Descrição |
|---|---|---|
| `cameras` | `CameraRepository` | Persistência das câmeras |
| `vagas` | `SpotRepository` | Para remover vagas junto com a câmera |
| `processador` | `FrameProcessor` | Processa os frames capturados |
| `fabrica_fonte` | `Callable[[Camera], FrameSource]` | Cria a fonte adequada ao `tipo_fonte` da câmera |
| `intervalo_s` | `float` | Intervalo entre frames processados (ex.: 1 s) |
| `_tarefas` | `dict[str, Task]` | Laços de captura em execução, por câmera |

| Método | Retorno | Descrição |
|---|---|---|
| `cadastrar(nome, stream_url, tipo_fonte)` | `Camera` | Cria a câmera (inativa) |
| `atualizar(camera_id, **campos)` | `Camera` | Edita parâmetros (UC08, fluxo A1) |
| `remover(camera_id)` | `None` | Para o monitoramento e remove câmera e vagas (UC08, fluxo A2) |
| `testar_conexao(camera_id)` | `bool` | Abre a fonte e tenta ler um frame (UC08, fluxo E1) |
| `iniciar_monitoramento(camera_id)` | `None` | Ativa a câmera e inicia seu laço de captura |
| `parar_monitoramento(camera_id)` | `None` | Encerra o laço e desativa a câmera |
| `iniciar_todas()` | `None` | Inicia as câmeras ativas (chamado na subida da aplicação) |
| `_laco_captura(camera)` | `None` | Lê frames em loop e chama `processador.processar`; marca as vagas como `DESCONHECIDA` se a fonte cair |

### 4.3 `SpotCalibrationService`

Demarcação das vagas sobre a imagem da câmera (UC08, passo 4).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Persistência das vagas |
| `cameras` | `CameraRepository` | Validação da câmera |

| Método | Retorno | Descrição |
|---|---|---|
| `definir_vagas(camera_id, demarcacoes: list[tuple[str, list[Point]]])` | `list[ParkingSpot]` | Substitui as vagas da câmera pelas demarcações (rótulo + polígono) |
| `editar_vaga(vaga_id, rotulo=None, poligono=None)` | `ParkingSpot` | Altera uma vaga existente |
| `remover_vaga(vaga_id)` | `None` | Remove uma vaga |
| `_validar_poligono(poligono)` | `None` | Exige 4 pontos e área positiva; lança `ValueError` caso contrário |

### 4.4 `OccupancyService`

Consulta do estado atual (RF03, RF05, UC01, UC02, UC05).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Estado das vagas |

| Método | Retorno | Descrição |
|---|---|---|
| `estado_atual(camera_id=None)` | `list[ParkingSpot]` | Vagas com o status atual (para o mapa) |
| `totais(camera_id=None)` | `OccupancySnapshot` | Totais de vagas livres/ocupadas e taxa de ocupação |

### 4.5 `HistoryService`

Histórico e relatórios (RF04, UC06, UC07).

| Atributo | Tipo | Descrição |
|---|---|---|
| `vagas` | `SpotRepository` | Para gerar os snapshots |
| `historico` | `HistoryRepository` | Persistência histórica |

| Método | Retorno | Descrição |
|---|---|---|
| `registrar_snapshot()` | `OccupancySnapshot` | Grava a ocupação atual; chamado periodicamente (ex.: a cada 5 min) |
| `consultar(inicio, fim, granularidade, camera_id=None)` | `list[OccupancySnapshot]` | Histórico agregado por `"hora"`, `"dia"` ou `"semana"` |
| `gerar_relatorio(inicio, fim)` | `dict` | Indicadores consolidados: média, pico e horário de pico da ocupação |
| `_agregar(snapshots, granularidade)` | `list[OccupancySnapshot]` | Agrupa e calcula a média por período |

### 4.6 `VehicleLocatorService`

Localização do próprio veículo (UC03).

| Atributo | Tipo | Descrição |
|---|---|---|
| `historico` | `HistoryRepository` | Leituras de placa |
| `vagas` | `SpotRepository` | Dados da vaga encontrada |

| Método | Retorno | Descrição |
|---|---|---|
| `localizar(usuario: User)` | `tuple[ParkingSpot, timedelta] \| None` | Vaga e tempo decorrido desde a leitura. Usa **somente** `usuario.placa`, nunca uma placa informada livremente (garantia de privacidade/LGPD). Lança erro se não houver placa vinculada (UC03, fluxo A2) |

### 4.7 `AuthService`

Autenticação e cadastro de usuários (UC04). A emissão do token JWT fica na camada `api/`, por ser um detalhe de HTTP.

| Atributo | Tipo | Descrição |
|---|---|---|
| `usuarios` | `UserRepository` | Persistência dos usuários |
| `hasher` | `PasswordHasher` | Hash de senhas |

| Método | Retorno | Descrição |
|---|---|---|
| `autenticar(email, senha)` | `User \| None` | Retorna o usuário se as credenciais forem válidas; `None` caso contrário, sem indicar qual campo errou (UC04, fluxo A1) |
| `cadastrar(nome, email, senha, papel)` | `User` | Cria um usuário com a senha já em hash |
| `vincular_placa(usuario_id, placa)` | `User` | Normaliza e vincula a placa à conta |

---

## 5. `adapters/` — Implementações concretas

Cada classe implementa uma porta de `core/ports.py`. Métodos herdados da porta não são repetidos na descrição; apenas os atributos e os métodos auxiliares são detalhados.

### 5.1 `adapters/vision.py`

#### `YoloObbDetector` (implementa `VehicleDetector`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `caminho_pesos` | `str` | Caminho do `best.pt` (`car_only_pklot_carpk_acpds`) |
| `conf` | `float` | Confiança mínima (padrão 0,25; ajustável por causa da oclusão parcial) |
| `imgsz` | `int` | Tamanho de entrada (1024, o mesmo do treino) |
| `device` | `str \| int` | `0` (GPU) ou `"cpu"` |
| `_modelo` | `ultralytics.YOLO` | Modelo carregado uma única vez no construtor |

| Método | Retorno | Descrição |
|---|---|---|
| `detectar(frame)` | `list[Detection]` | Executa a inferência |
| `_converter(resultado)` | `list[Detection]` | Converte `result.obb.xyxyxyxy` do Ultralytics em objetos `Detection` |

#### `EasyOcrPlateReader` (implementa `PlateReader`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `idiomas` | `list[str]` | Idiomas do OCR (ex.: `["pt", "en"]`) |
| `conf_min` | `float` | Confiança mínima de leitura |
| `_leitor` | `easyocr.Reader` | Leitor carregado uma vez |

| Método | Retorno | Descrição |
|---|---|---|
| `ler(frame, deteccao)` | `str \| None` | Recorta o veículo e tenta ler a placa |
| `_recortar(frame, deteccao)` | `ndarray` | Recorte da região do veículo |
| `_normalizar(texto)` | `str \| None` | Remove caracteres inválidos e valida os formatos Mercosul (`AAA1A23`) e antigo (`AAA1234`) |

### 5.2 `adapters/occupancy.py`

#### `SpotCropper`

Classe auxiliar (não é porta): recorta e retifica a região de uma vaga.

| Atributo | Tipo | Descrição |
|---|---|---|
| `largura` | `int` | Largura do recorte de saída (ex.: 150) |
| `altura` | `int` | Altura do recorte de saída (ex.: 150) |

| Método | Retorno | Descrição |
|---|---|---|
| `recortar(frame, poligono)` | `ndarray` | Aplica transformação de perspectiva (`cv2.getPerspectiveTransform` + `warpPerspective`) e devolve a vaga "de frente" |

> A mesma classe pode ser reutilizada no script que gera o dataset de treino da Rota 2 a partir do `samples.json` do PKLot, garantindo que treino e inferência recortem as vagas da mesma forma.

#### `OverlapOccupancyStrategy` (implementa `OccupancyStrategy`) — Rota 1

| Atributo | Tipo | Descrição |
|---|---|---|
| `limiar` | `float` | Fração mínima da vaga coberta por uma detecção para considerá-la ocupada (ex.: 0,4) |

| Método | Retorno | Descrição |
|---|---|---|
| `classificar(frame, vaga, deteccoes)` | `SpotStatus` | `OCUPADA` se alguma detecção cobre a vaga acima do `limiar` (via `vaga.fracao_coberta`); caso contrário, `LIVRE` |

#### `ClassifierOccupancyStrategy` (implementa `OccupancyStrategy`) — Rota 2

| Atributo | Tipo | Descrição |
|---|---|---|
| `caminho_pesos` | `str` | Pesos do classificador (ex.: ResNet) |
| `recortador` | `SpotCropper` | Recorte das vagas |
| `limiar` | `float` | Probabilidade mínima para `OCUPADA` (ex.: 0,5) |
| `device` | `str` | `"cuda"` ou `"cpu"` |
| `_modelo` | `torch.nn.Module` | Classificador carregado |

| Método | Retorno | Descrição |
|---|---|---|
| `classificar(frame, vaga, deteccoes)` | `SpotStatus` | Recorta a vaga e classifica; ignora `deteccoes` |
| `_preprocessar(recorte)` | `torch.Tensor` | Redimensiona e normaliza como no treino |

### 5.3 `adapters/database.py`

#### Repositórios em memória (testes e desenvolvimento local)

`InMemoryCameraRepository`, `InMemorySpotRepository`, `InMemoryHistoryRepository` e `InMemoryUserRepository` implementam as respectivas portas.

| Atributo | Tipo | Descrição |
|---|---|---|
| `_dados` | `dict[str, objeto]` | Armazenamento por id (no histórico: listas de snapshots e de leituras) |

Não possuem métodos além dos definidos na porta.

#### `DynamoSpotRepository` (implementa `SpotRepository`)

| Atributo | Tipo | Descrição |
|---|---|---|
| `nome_tabela` | `str` | Tabela do DynamoDB |
| `_tabela` | `boto3 Table` | Recurso da tabela |

| Método | Retorno | Descrição |
|---|---|---|
| `_para_item(vaga)` | `dict` | Converte `ParkingSpot` em item do DynamoDB |
| `_de_item(item)` | `ParkingSpot` | Converte item do DynamoDB em `ParkingSpot` |

#### `PostgresHistoryRepository` (implementa `HistoryRepository`)

`PostgresCameraRepository` e `PostgresUserRepository` seguem o mesmo padrão.

| Atributo | Tipo | Descrição |
|---|---|---|
| `dsn` | `str` | String de conexão com o RDS |
| `_engine` | `sqlalchemy.Engine` | Conexão (ou pool do `psycopg`) |

| Método | Retorno | Descrição |
|---|---|---|
| `criar_tabelas()` | `None` | Cria as tabelas se não existirem |
| `_linha_para_snapshot(linha)` | `OccupancySnapshot` | Conversão de linha do banco em entidade |

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
| `_inscritos` | `set[asyncio.Queue]` | Uma fila por cliente SSE conectado |
| `_loop` | `asyncio.AbstractEventLoop` | Loop da aplicação, usado para publicar a partir das threads de captura |

| Método | Retorno | Descrição |
|---|---|---|
| `publicar(mudanca)` | `None` | Coloca o evento em todas as filas (via `call_soon_threadsafe`, pois a captura roda fora do loop assíncrono) |
| `inscrever()` | `AsyncIterator[SpotChange]` | Cria uma fila, entrega os eventos e a remove quando o cliente desconecta |

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
| `UsuarioOut` | `id`, `nome`, `email`, `papel`, `placa` | Dados do usuário logado (sem senha) |
| `VincularPlacaIn` | `placa` | Cadastro de placa |
| `CameraIn` | `nome`, `stream_url`, `tipo_fonte` | Cadastro/edição de câmera |
| `CameraOut` | `id`, `nome`, `tipo_fonte`, `ativa` | Resposta (não expõe `stream_url` ao motorista) |
| `PontoIn` | `x`, `y` | Ponto de um polígono |
| `VagaIn` | `rotulo`, `poligono: list[PontoIn]` | Demarcação de vaga |
| `VagaOut` | `id`, `camera_id`, `rotulo`, `poligono`, `status`, `atualizado_em` | Estado da vaga para o mapa |
| `TotaisOut` | `total`, `livres`, `ocupadas`, `taxa_ocupacao` | Contadores |
| `SnapshotOut` | `timestamp`, `livres`, `ocupadas`, `taxa_ocupacao` | Ponto do histórico |
| `RelatorioOut` | `inicio`, `fim`, `media`, `pico`, `horario_pico` | Relatório consolidado |
| `LocalizacaoOut` | `vaga_rotulo`, `camera_id`, `tempo_estacionado_min` | Resposta do UC03 |

### 6.2 `api/routes.py` — Endpoints

Arquivo de funções (rotas FastAPI). Não define classes; usa os serviços por meio de `Depends`.

**Dependências de segurança:**

| Função | Descrição |
|---|---|
| `criar_token(usuario)` | Gera o JWT e o grava em cookie `HttpOnly` |
| `usuario_atual(request)` | Lê e valida o JWT do cookie; retorna o `User` |
| `exigir_admin(usuario)` | Retorna erro 403 se o usuário não for `ADMIN` |

**Endpoints:**

| Método | Rota | Perfil | Serviço | Rastreabilidade |
|---|---|---|---|---|
| POST | `/auth/login` | público | `AuthService.autenticar` | UC04 |
| POST | `/auth/logout` | autenticado | — | UC04 |
| GET | `/me` | autenticado | — | UC04 |
| PUT | `/me/placa` | motorista | `AuthService.vincular_placa` | UC03 |
| GET | `/me/veiculo` | motorista | `VehicleLocatorService.localizar` | UC03 |
| GET | `/vagas` | autenticado | `OccupancyService.estado_atual` | RF05, UC02 |
| GET | `/vagas/totais` | autenticado | `OccupancyService.totais` | RF03, UC01, UC05 |
| GET | `/eventos` | autenticado | `EventPublisher.inscrever` (SSE) | RF05, UC01, UC05 |
| GET | `/historico` | admin | `HistoryService.consultar` | RF04, UC06 |
| GET | `/relatorios` | admin | `HistoryService.gerar_relatorio` | RF04, UC07 |
| GET/POST | `/cameras` | admin | `CameraManager` | RF01, UC08 |
| PUT/DELETE | `/cameras/{id}` | admin | `CameraManager.atualizar` / `remover` | UC08 |
| POST | `/cameras/{id}/testar` | admin | `CameraManager.testar_conexao` | UC08 |
| POST | `/cameras/{id}/iniciar` e `/parar` | admin | `CameraManager` | RF01, UC08 |
| PUT | `/cameras/{id}/vagas` | admin | `SpotCalibrationService.definir_vagas` | RF02, UC08 |

---

## 7. `main.py` — Montagem do sistema

### 7.1 `Container`

Único ponto que conhece todas as implementações concretas. Instancia os adaptadores, injeta-os nos serviços e expõe tudo para as rotas.

| Atributo | Tipo | Descrição |
|---|---|---|
| `config` | `Settings` | Configurações lidas de variáveis de ambiente (`pydantic-settings`): caminhos dos pesos, `conf`, DSN do banco, tabela DynamoDB, segredo JWT, modo (`"local"` ou `"aws"`) |
| `detector` | `VehicleDetector` | `YoloObbDetector` |
| `estrategia` | `OccupancyStrategy` | `OverlapOccupancyStrategy` ou `ClassifierOccupancyStrategy` |
| `repositorios` | — | `cameras`, `vagas`, `historico`, `usuarios` (em memória no modo local; DynamoDB/PostgreSQL no modo AWS) |
| `eventos` | `EventPublisher` | `InMemoryEventPublisher` |
| `servicos` | — | `frame_processor`, `camera_manager`, `calibracao`, `ocupacao`, `historico_service`, `localizador`, `auth` |

| Método | Retorno | Descrição |
|---|---|---|
| `__init__(config)` | — | Cria todos os objetos na ordem adaptadores → serviços |
| `_criar_repositorios()` | `None` | Escolhe a implementação conforme `config.modo` |
| `_criar_estrategia()` | `OccupancyStrategy` | Escolhe Rota 1 ou Rota 2 conforme a configuração |

### 7.2 Função `criar_app() -> FastAPI`

Cria o `Container`, registra as rotas, inicia as câmeras ativas e o agendamento do `HistoryService.registrar_snapshot` no evento de inicialização (`lifespan`), e encerra os laços de captura ao desligar.

---

## 8. Testes (`tests/`)

| Tipo | O que testar | Adaptadores usados |
|---|---|---|
| Unitário | Métodos de `models.py` (`fracao_coberta`, `atualizar_status`, `de_vagas`) | Nenhum |
| Unitário | `FrameProcessor`, `OccupancyService`, `HistoryService`, `VehicleLocatorService`, `AuthService` | Repositórios em memória + um `FakeDetector` que devolve detecções fixas |
| Unitário | `OverlapOccupancyStrategy` com polígonos conhecidos | Nenhum |
| Integração | Pipeline completo com vídeo real | `YoloObbDetector` + `VideoFileSource` |
| Integração | Rotas da API (login, permissões, vagas, SSE) | `TestClient` do FastAPI + repositórios em memória |

O `FakeDetector` é um exemplo direto do benefício da arquitetura: como `FrameProcessor` depende apenas de `VehicleDetector`, a regra de negócio é testada sem GPU e sem o modelo YOLO.

---

## 9. Rastreabilidade: classes × requisitos

| Requisito / caso de uso | Classes principais |
|---|---|
| RF01 — captura em tempo real | `FrameSource` e implementações, `CameraManager` |
| RF02 — classificar vagas | `VehicleDetector`, `OccupancyStrategy`, `FrameProcessor`, `SpotCalibrationService` |
| RF03 — totais de vagas | `OccupancyService`, `OccupancySnapshot` |
| RF04 — histórico diário | `HistoryService`, `HistoryRepository` |
| RF05 — mapa em tempo real | `OccupancyService`, `EventPublisher`, `SpotChange` |
| RNF01 — precisão ≥ 90% | `YoloObbDetector`, `ClassifierOccupancyStrategy` (avaliados no conjunto de teste) |
| RNF03 — segurança | `AuthService`, `PasswordHasher`, `exigir_admin`, `VehicleLocatorService` (acesso restrito à própria placa) |
| RNF04 — HTTPS | Infraestrutura (ALB + ACM); fora das classes Python |
| UC03 — localizar veículo | `PlateReader`, `PlateReading`, `VehicleLocatorService` |
| UC08 — gerenciar câmeras e vagas | `CameraManager`, `SpotCalibrationService` |
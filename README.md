# nas-subtitles

**Legendas em português, geradas localmente a partir do áudio dos seus vídeos.**

O `nas-subtitles` combina um CLI e um worker único para selecionar a faixa de áudio, transcrever com faster-whisper, traduzir com Argos Translate e gerar arquivos SRT. O processamento usa modelos locais, sem provedores de legendas, APIs de tradução ou telemetria.

```text
Vídeo → FFmpeg → Transcrição → Tradução → Validação → Legenda .pt-BR.srt
```

A mídia original permanece intacta. A publicação nunca sobrescreve um arquivo existente.

## Visão geral

| Característica | Comportamento |
|---|---|
| Interface | CLI `nas-subs` |
| Processamento | Worker único, com execução serial |
| Transcrição | faster-whisper, com modelo local |
| Tradução | Argos Translate, com par direto local |
| Saída | Arquivo SRT |
| Publicação padrão | Staging, para revisão |
| Execução atual | CPU |
| Rede durante inferência | Não é necessária |

## Estado do projeto

Os itens **000–005** do [roadmap](ROADMAP.md) estão concluídos. O item **006** (dublagem pt-BR) tem as fases 1 a 3 implementadas, mas está **pausado e desligado por padrão** (`dubbing.enabled: false`); veja [Dublagem](#dublagem-pausada). A implementação de legendas foi exercitada em um Mac Apple Silicon (`arm64`).

| Área | Situação |
|---|---|
| Geração de legendas | Implementada |
| Dashboard opcional | Disponível |
| Webhooks Sonarr/Radarr | Implementados, com testes unitários |
| Implantação em NAS | Ainda não realizada |
| Piloto em episódio real da biblioteca | Ainda não realizado |
| Validação em amd64 ou GPU | Ainda não realizada |
| Dublagem | Fluxo de ponta a ponta com voz fixa implementado e testado só com um preview de 150 s; desligado por padrão, sem avaliação auditiva |

Os webhooks também não foram exercitados contra instâncias reais de Sonarr/Radarr ou um NAS. Consulte [os resultados de benchmark](docs/benchmark.md) para conhecer as medições disponíveis.

## Português brasileiro: alcance e limitações

O destino público é `pt-BR`. Internamente, o pacote Argos utiliza o par direto `en → pb`, em que `pb` representa português do Brasil. Esse código interno não aparece nos nomes de arquivos, em `languages.targets` ou nos jobs.

O sufixo `.pt-BR.srt` expressa o idioma desejado, mas **não garante adaptação linguística ao português brasileiro**. O resultado pode conter vocabulário ou construções de português europeu. Não há glossário nem regras de adaptação regional.

A qualidade da tradução, da transcrição e da sincronização deve ser revisada. Por esse motivo, `staging` é o modo padrão: o resultado fica no diretório de saída antes de qualquer publicação ao lado do vídeo.

## Requisitos

Escolha uma das formas de execução:

- **Local:** Python 3.11 (`>=3.11,<3.12`), [uv](https://docs.astral.sh/uv/) e FFmpeg/ffprobe.
- **Container:** Docker e Compose v2.

Referência de recursos: aproximadamente **4 GiB de RAM** e **3 GiB livres** para intermediários. A implementação atual utiliza CPU e não possui caminho de execução GPU.

### Dependências por plataforma

| Ambiente | Distribuição do Torch |
|---|---|
| Linux e imagem Docker | `torch==2.14.1+cpu`, pelo índice CPU do PyTorch |
| macOS nativo | `torch==2.14.1`, pela distribuição padrão |

Os ambientes têm dependências distintas. Um `uv sync` no Mac não instala o wheel Linux `+cpu`.

### Modelos e acesso à rede

O único comando da aplicação autorizado a acessar a rede é `nas-subs models install`. Importação de módulos, diagnóstico e inicialização do worker não baixam modelos automaticamente.

Se um modelo estiver ausente, a aplicação retorna `model_missing`. Não há fallback para serviços remotos.

## Instalação com Docker

### 1. Preparar a configuração

Na raiz do repositório:

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
mkdir -p data/state data/work data/models data/output
```

Ajuste os arquivos:

| Arquivo | Configuração |
|---|---|
| `config/config.yaml` | `media_roots` com caminhos absolutos vistos pelo container |
| `.env` | `APP_UID`, `APP_GID` e `MEDIA_HOST_PATH` |

Use `id -u` e `id -g` para descobrir UID e GID. `MEDIA_HOST_PATH` deve conter o caminho absoluto da biblioteca no host. Não versione o `.env`.

A biblioteca é montada em `/media`. Exemplos de roots no container: `/media/library/series` e `/media/library/movies`. O arquivo de configuração padrão do CLI é `/config/config.yaml`.

Mantenha `data/` em disco local, especialmente o estado SQLite; não utilize SMB/NFS para o banco da fila.

```bash
docker compose build
docker compose config
```

Confira os volumes na configuração expandida. Um `MEDIA_HOST_PATH` inexistente causa falha no bind, em vez de criar um diretório vazio (`create_host_path: false`).

### 2. Instalar os modelos

Esta é a etapa da aplicação que utiliza rede:

```bash
docker compose run --rm -e HF_HUB_OFFLINE=0 subtitles \
  nas-subs models install --config /config/config.yaml
```

O comando instala o Whisper configurado — `small` por padrão —, o par Argos direto do destino primário (para `pt-BR`, o par interno é `en → pb`) e os modelos de dublagem: a voz Piper `pt_BR-faber-medium` e o separador Demucs `htdemucs` (cerca de 150 MB a mais). Os modelos de dublagem são baixados mesmo com a dublagem desligada.

Destinos adicionais em `languages.targets` não são instalados por esse comando. Se o par direto não estiver disponível no índice Argos, a instalação falha com orientação; não há tradução por idioma-pivô ou API remota.

### 3. Validar o ambiente offline

O override `compose.offline.yaml` configura `network_mode: none` no worker.

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
  nas-subs models verify --offline --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
  nas-subs doctor --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
  nas-subs inspect /media/library/series/EPISODIO.mkv \
  --config /config/config.yaml
```

O `doctor` verifica ambiente, recursos, diretórios, espaço livre, FFmpeg/ffprobe, banco, dashboard, configuração de webhook e carregamento offline dos modelos.

- Modelo ausente: falha `model_missing`.
- Token de webhook ausente: aviso; o listener não inicia, mas o worker pode funcionar.

### 4. Gerar uma prévia

Comece com cinco minutos:

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
  nas-subs process /media/library/series/EPISODIO.mkv \
  --preview-seconds 300 --config /config/config.yaml
```

O resultado aparece em `data/output`, com `.preview` no nome. A prévia permanece em staging, não é publicada como sidecar e não conta como processamento completo da biblioteca.

Revise o SRT antes de processar o arquivo inteiro.

### 5. Iniciar o worker

Para executar scan periódico e processamento serial em staging:

```bash
docker compose -f compose.yaml -f compose.offline.yaml up -d
```

Consulte o [runbook](docs/runbook.md) para dry-run do scanner, piloto e recuperação.

## Execução nativa no macOS

Na execução local, todos os caminhos precisam apontar para diretórios do host. Os caminhos `/media`, `/state`, `/work`, `/models` e `/output` do exemplo Docker devem ser substituídos.

```bash
uv sync --frozen --group dev
cp config/config.example.yaml config/config.yaml
mkdir -p data/state data/work data/models data/output
```

Exemplo de configuração — ajuste os caminhos para sua máquina:

```yaml
media_roots: [/Users/voce/Videos]
state_dir: /Users/voce/nas-subtitles/data/state
work_dir: /Users/voce/nas-subtitles/data/work
models_dir: /Users/voce/nas-subtitles/data/models
output_dir: /Users/voce/nas-subtitles/data/output
publish_mode: staging
```

Passe sempre o caminho absoluto de `--config`; o padrão do CLI continua sendo `/config/config.yaml`.

```bash
uv run nas-subs models install --config /caminho/absoluto/config/config.yaml
uv run nas-subs models verify --offline --config /caminho/absoluto/config/config.yaml
uv run nas-subs doctor --config /caminho/absoluto/config/config.yaml

uv run nas-subs inspect /Users/voce/Videos/EPISODIO.mkv \
  --config /caminho/absoluto/config/config.yaml

uv run nas-subs process /Users/voce/Videos/EPISODIO.mkv \
  --preview-seconds 300 --config /caminho/absoluto/config/config.yaml
```

Para execução contínua:

```bash
uv run nas-subs daemon --config /caminho/absoluto/config/config.yaml
```

`daemon` e `worker` são aliases do mesmo processo.

## Publicação: staging e sidecar

| Modo | Destino | Uso |
|---|---|---|
| `staging` | `output_dir` | Revisão antes da publicação; padrão |
| `sidecar` | Ao lado do vídeo, como `<stem>.pt-BR.srt` | Publicação na biblioteca |

### Sidecar com Docker

São necessárias as duas configurações:

1. `publish_mode: sidecar` no YAML.
2. Override `compose.sidecar.yaml`, que permite escrita no volume de mídia.

Para publicar um job aprovado sem retranscrever, mantendo o worker sem rede:

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.sidecar.yaml \
  run --rm subtitles nas-subs publish JOB_ID --config /config/config.yaml
```

Para execução contínua em sidecar:

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.sidecar.yaml up -d
```

### Sidecar na execução local

Configure `publish_mode: sidecar` e garanta permissão de escrita nos diretórios de mídia. O override Compose não se aplica à execução nativa.

### Proteção contra sobrescrita

A publicação utiliza um arquivo temporário e hard-link exclusivo. Se o nome final já existir, o resultado é `output_conflict`, com código de saída `5`. O arquivo existente é preservado.

O vídeo nunca é remuxado, recodificado, renomeado ou apagado, e seus metadados não são alterados.

## Comandos disponíveis

| Comando | Função |
|---|---|
| `nas-subs doctor` | Diagnóstico de ambiente, recursos, publicação e modelos |
| `nas-subs models install` | Instalação de Whisper, do par Argos direto e dos modelos de dublagem (Piper e Demucs) |
| `nas-subs models verify` | Verificação dos modelos offline |
| `nas-subs inspect PATH` | Streams, duração e seleção da faixa de áudio |
| `nas-subs process PATH` | Processamento imediato sob o mesmo lock do worker |
| `nas-subs enqueue PATH` | Enfileiramento com verificação de estabilidade |
| `nas-subs scan` | Uma passagem pelos roots; `--dry-run` não enfileira |
| `nas-subs worker` / `daemon` | Scan periódico e processamento serial |
| `nas-subs jobs list\|show\|retry\|cancel\|approve` | Consulta e gerenciamento de jobs |
| `nas-subs publish JOB_ID` | Publicação de job aprovado sem retranscrição |
| `nas-subs benchmark PATH` | Medição em uma janela curta na máquina atual |
| `nas-subs health` | Heartbeat e banco; utilizado pelo healthcheck Docker |
| `nas-subs cleanup` | Limpeza restrita a `work_dir`; dry-run até `--apply` |
| `nas-subs backup` | Backup consistente de configuração, manifesto e SQLite |
| `nas-subs dub enqueue\|process\|plan` | Dublagem (pausada; exige `dubbing.enabled: true`) |
| `nas-subs dashboard` | Interface opcional; não processa jobs |
| `nas-subs webhooks` | Listener Sonarr/Radarr; apenas enfileira |

Todos os comandos aceitam `--json` e não apresentam prompts interativos.

### Códigos de saída

| Código | Significado |
|---|---|
| `0` | Sucesso |
| `2` | Argumentos ou configuração inválidos |
| `3` | Falha de preflight |
| `4` | Falha de processamento |
| `5` | Revisão necessária ou conflito |
| `6` | Lock ocupado por outro worker |

`scan` executa uma passagem; `--once` é aceito. O loop periódico pertence ao `daemon`.

## Configuração

O arquivo [config/config.example.yaml](config/config.example.yaml) documenta as opções. Todos os caminhos devem ser absolutos.

| Opção | Padrão |
|---|---|
| Publicação | `staging` |
| Modelo Whisper | `small` |
| Dispositivo / precisão | `cpu` / `int8` |
| Chunks / contexto | 300 s / 2 s de overlap |
| Formatação | Até 2 linhas de 42 caracteres |
| Idioma de origem | `auto` |
| Destinos | `[pt-BR]` |
| Pares permitidos | `[en:pt]`; `pb` é interno ao Argos |
| Intervalo de scan | 600 s |
| Janela de estabilidade | 600 s |
| Idade mínima do arquivo | 600 s |
| Concorrência | 1 worker |
| Webhooks | `127.0.0.1:8788`, sem mapas de caminho |
| Dublagem | `dubbing.enabled: false` (pausada) |

Vários destinos geram um job por idioma e um arquivo `.<tag>.srt`. A instalação automática de modelos continua limitada ao par do destino primário.

## Componentes opcionais

### Dashboard

O dashboard executa em outro container. Interrompê-lo não interrompe o worker. A porta é publicada apenas no loopback do host, em `127.0.0.1:8787`.

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.dashboard.yaml up -d
```

Na execução nativa: `uv run nas-subs dashboard --config /caminho/absoluto/config/config.yaml`.

O que ele permite fazer:

- Ver a fila e o histórico, com busca, filtros, ordenação e paginação, e o detalhe de cada job.
- Tentar de novo, cancelar e reprocessar jobs; rodar uma varredura da biblioteca.
- **Excluir jobs finalizados** (concluídos, ignorados, falhos ou cancelados), um a um ou em lote. Só as linhas do job, o manifesto e a pasta de trabalho dele são removidos; vídeos, legendas publicadas e saídas em `output_dir` ficam. Uma nova varredura pode recriar o job se o arquivo ainda não tiver legenda.
- **Trocar as bibliotecas de mídia** em Configurações. O valor novo é salvo em `state_dir/runtime-settings.json` e sobrepõe `media_roots`; o `config.yaml` não é reescrito. **Reinicie o worker** depois, pois ele só vê o novo valor no início. Remover uma biblioteca com jobs não finalizados é recusado, e a edição só existe com bind em loopback ou token configurado.

Os demais valores de Configurações são somente leitura. Detalhes em [docs/dashboard.md](docs/dashboard.md).

### Webhooks Sonarr/Radarr

O listener é um processo separado, iniciado por `nas-subs webhooks`. Ele recebe POSTs JSON e **apenas enfileira**; a transcrição continua no worker. O scan periódico recupera importações que não chegaram pelo webhook.

O contrato implementado é Connect → Webhook. Não há script oficial para Connect → Custom Script.

Essa integração possui testes unitários, mas ainda não foi validada contra instâncias reais. Consulte o [contrato completo](docs/roadmap/004-sonarr-radarr-webhooks.md).

#### Iniciar o listener

Defina `NAS_SUBS_WEBHOOK_TOKEN` no `.env`. A variável de ambiente tem precedência sobre `webhooks.token` no YAML. O listener recusa iniciar sem token.

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.webhooks.yaml up -d
```

A porta no host é `127.0.0.1:8788`. O bind interno do container é `0.0.0.0:8788` para permitir a publicação pelo Compose. Não exponha o listener à internet.

Na execução local:

```bash
NAS_SUBS_WEBHOOK_TOKEN=um-segredo-longo \
  uv run nas-subs webhooks --config /caminho/absoluto/config/config.yaml
```

O padrão local é `127.0.0.1:8788`. O endpoint `GET /health` não exige autenticação. O worker precisa estar em execução separadamente.

#### Configurar o Connect

Em Settings → Connect → Webhook, configure POST e a URL correspondente:

| Origem | Endpoint |
|---|---|
| Sonarr | `http://127.0.0.1:8788/hooks/sonarr` |
| Radarr | `http://127.0.0.1:8788/hooks/radarr` |
| Genérico | `http://127.0.0.1:8788/hooks` |

Utilize o mesmo segredo por `Authorization: Bearer <token>`, `X-Api-Key` ou `X-Webhook-Token`. A query `?token=` também é aceita; prefira cabeçalhos.

- Importação/upgrade: processado conforme o payload.
- Evento `Test`: reconhecido, sem criar job.
- Outros eventos: ignorados, com HTTP 200 e `action: ignored`.
- Token ausente ou incorreto: HTTP 401, sem enfileiramento.
- Payload inválido ou caminho fora dos roots: rejeitado, sem enfileiramento.

Se Sonarr/Radarr estiver em outro container, `127.0.0.1` aponta para esse container. Configure uma rede compartilhada com o serviço `webhooks` ou um endereço acessível adequado à implantação.

#### Mapear caminhos

O caminho recebido em `episodeFile.path`, `movieFile.path` ou suas listas deve corresponder à visão de disco do listener. Configure prefixos quando os caminhos forem diferentes:

```yaml
webhooks:
  bind: 127.0.0.1
  port: 8788
  path_maps:
    - host_prefix: /nas/media
      container_prefix: /media
```

O caminho resultante deve permanecer dentro de `media_roots`. Barras Windows são normalizadas; conversões Windows → POSIX exigem mapas explícitos.

Importações concluídas recebidas por webhook não repetem a janela de estabilidade. O scanner continua verificando tamanho e `mtime` em duas observações.

Webhooks repetidos para o mesmo fingerprint retornam o job existente (`already_queued`). Alterar bind, porta, token ou mapas não invalida transcrições.

### Dublagem (pausada)

Há um fluxo de dublagem em pt-BR com voz fixa: separa o diálogo (Demucs), transcreve, traduz, sintetiza com Piper, ajusta o tempo, mixa com o fundo e grava um pacote em staging (`output_dir/<root>/dubbing/<job_id>/`), nunca ao lado do vídeo. Ele está **desligado por padrão**: `nas-subs dub enqueue` e `dub process` recusam com `config_invalid` até que `dubbing.enabled: true` seja definido no `config.yaml`. A geração de legendas não depende dele.

Limitações medidas: um preview real de 150 s terminou em `needs_review`, com a maior parte dos trechos acima do limite de aceleração, porque a adaptação de texto ao tempo da fala ainda não existe; uma só voz fala por todos os personagens; ninguém avaliou o áudio de forma auditiva. Ver [docs/benchmark.md](docs/benchmark.md) e o [plano](docs/roadmap/006-dubbing-completion-plan.md).

## Uso com Bazarr e Jellyfin

Defina um produtor de legendas por título. O Bazarr pode indexar o SRT gerado e posteriormente substituí-lo; ajuste os perfis ou upgrades dos títulos após o piloto, conforme sua versão.

No Jellyfin, confirme a existência e a validade do SRT antes de atualizar a biblioteca manualmente, se necessário.

O projeto não altera essas aplicações e não utiliza suas chaves de API.

## Proteção da biblioteca

- Mídia somente leitura por padrão.
- Nenhuma alteração de vídeo ou metadados do container.
- Nenhuma sobrescrita de arquivos existentes.
- Vídeos com legenda portuguesa completa, externa ou embutida, são ignorados; legenda `forced` não conta como completa.
- Arquivos em cópia não são processados pelo scanner: tamanho e `mtime` precisam permanecer estáveis em duas observações.
- `cleanup` atua somente em `work_dir`, com job ID validado, sem exclusão por glob ou na biblioteca.

## Documentação

| Documento | Conteúdo |
|---|---|
| [Arquitetura](docs/architecture.md) | Componentes e fluxo de processamento |
| [Runbook](docs/runbook.md) | Instalação, piloto e recuperação |
| [Benchmark](docs/benchmark.md) | Medições realizadas e validações pendentes |
| [Dashboard](docs/dashboard.md) | Telas, API e o que pode ser alterado por ele |
| [Decisões técnicas](docs/decisions.md) | Justificativas de arquitetura |
| [Dublagem](docs/roadmap/006-dubbing-completion-plan.md) | Estado verificado e plano do fluxo de dublagem |
| [Webhooks](docs/roadmap/004-sonarr-radarr-webhooks.md) | Contrato de integração Sonarr/Radarr |
| [Guia para contribuidores](AGENTS.md) | Regras e responsabilidade dos módulos |
| [Roadmap](ROADMAP.md) | Itens concluídos e evolução planejada |

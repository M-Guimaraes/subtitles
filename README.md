# nas-subtitles

CLI e um único worker que geram legendas em português a partir do áudio de
vídeos armazenados localmente. Só modelos locais: Whisper (faster-whisper) e
Argos Translate. Sem provedores de legenda, sem API de tradução, sem
telemetria.

Um vídeo entra; um `.pt-BR.srt` sai. O áudio é escolhido e extraído com
FFmpeg, transcrito localmente, traduzido localmente, renderizado em SRT e
publicado sem nunca sobrescrever um arquivo existente e sem nunca alterar o
vídeo.

Dublagem (`nas-subs dub`, item 006 do roadmap) **não existe** neste código.

## O que `pt-BR` faz e o que não faz

**O sufixo `pt-BR` é o destino desejado, não uma garantia linguística.** O
pacote Argos instalado é o par direto `en -> pb` (código interno do Argos
para português do Brasil). Na configuração pública o destino continua
`pt-BR`; `pb` nunca aparece em nome de arquivo, em `languages.targets` nem
nos jobs. O texto produzido é português e pode usar vocabulário e
construção europeus. Não há glossário nem regras de adaptação para
português brasileiro.

O projeto também não promete tradução profissional, ausência de
alucinação nem sincronia perfeita. Por isso o modo de publicação padrão é
`staging` e existem portas de revisão: você lê o resultado antes de
qualquer arquivo ser escrito ao lado dos vídeos.

## Estado atual

O software está implementado e foi exercitado num Mac Apple Silicon
(arm64). **Não foi implantado num NAS.** Não há medição nesse servidor, nem
piloto num episódio real da biblioteca, nem afirmação sobre amd64, GPU ou
outro hardware. O que foi medido está em
[docs/benchmark.md](docs/benchmark.md).

Os itens 000–005 do [ROADMAP.md](ROADMAP.md) estão feitos. Webhooks
Sonarr/Radarr (004) existem no código e nos testes unitários; **não foram
exercitados contra um Sonarr/Radarr de verdade nem contra um NAS.**

## Requisitos

- Python **3.11** (a restrição do projeto é `>=3.11,<3.12`) com
  [uv](https://docs.astral.sh/uv/) e FFmpeg, **ou** Docker com Compose v2.
- Cerca de 4 GiB de RAM e 3 GiB livres em disco para intermediários.
- CPU apenas. Não há caminho GPU.

**Torch:** no Linux (incluindo a imagem Docker) a dependência é
`torch==2.14.1+cpu` no índice CPU do PyTorch, para não puxar CUDA/NVIDIA.
No macOS nativo (`sys_platform == 'darwin'`) é `torch==2.14.1` da
distribuição padrão. São ambientes distintos; um `uv sync` no Mac não
instala o wheel `+cpu` do Linux.

O único comando autorizado a usar a rede é `nas-subs models install`.
Import, `doctor`, `daemon` e o restante falham com `model_missing` se o
modelo não estiver no disco. Não há fallback para API remota.

## Passo a passo: Docker (Mac ou Linux)

Caminhos no YAML são **caminhos dentro do contêiner** e precisam ser
absolutos. A biblioteca do host é montada em `/media`. O padrão de
`--config` do CLI é `/config/config.yaml`, que é o que o Compose monta.

```bash
cp config/config.example.yaml config/config.yaml   # edite media_roots
cp .env.example .env                               # APP_UID, APP_GID, MEDIA_HOST_PATH
mkdir -p data/state data/work data/models data/output
```

Em `config/config.yaml` deixe `media_roots` como o contêiner vê a
biblioteca (no exemplo: `/media/library/series` e `/media/library/movies`).
Em `.env` use `id -u` / `id -g` para `APP_UID`/`APP_GID` e o caminho
absoluto da biblioteca no host em `MEDIA_HOST_PATH`. Não commite `.env`.
`data/` deve ficar em disco local (SQLite não vai bem em SMB/NFS).

```bash
docker compose build
docker compose config          # confira se os binds expandiram como você espera
```

Um `MEDIA_HOST_PATH` inexistente falha o bind (`create_host_path: false`) em
vez de criar um diretório vazio.

### Instalar modelos (única etapa com rede)

```bash
docker compose run --rm -e HF_HUB_OFFLINE=0 subtitles \
    nas-subs models install --config /config/config.yaml
```

Isso baixa o Whisper configurado (padrão `small`) e o par Argos direto
(`en -> pb` quando o destino público é `pt-BR`). Pares extras em
`languages.targets` **não** são instalados por este comando. Se o par
direto não existir no índice do Argos, a instalação falha com instrução;
não use idioma-pivô nem API remota.

### Verificar, diagnosticar, inspecionar

A partir daqui a forma normal de rodar é **sem rede** (`compose.offline.yaml`
coloca `network_mode: none` no worker):

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs models verify --offline --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs doctor --config /config/config.yaml

docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs inspect /media/library/series/EPISODIO.mkv --config /config/config.yaml
```

`doctor` checa Python, CPU, memória, `media_roots`, diretórios, espaço
livre, ffmpeg/ffprobe, banco da fila, dashboard, token de webhook e se os
modelos carregam offline. Falha de modelo é `fail` (`model_missing`);
token de webhook ausente é `warn` (o listener recusa subir, o worker não).

### Preview (cinco minutos, só staging)

O preview cai em `data/output` (montado em `/output`) com `.preview` no
nome. **Nunca** vira sidecar e **nunca** satisfaz a biblioteca.

```bash
docker compose -f compose.yaml -f compose.offline.yaml run --rm subtitles \
    nas-subs process /media/library/series/EPISODIO.mkv \
    --preview-seconds 300 --config /config/config.yaml
```

Leia o SRT em `data/output` antes de processar o arquivo inteiro.

### Staging versus sidecar

`publish_mode: staging` é o padrão em `config/config.example.yaml`. O SRT
vai para `output_dir` (`data/output` no host). O bind de `/media` no
`compose.yaml` é **somente leitura**.

Para escrever ao lado do vídeo (`<stem>.pt-BR.srt`) **duas** coisas
precisam ser verdade ao mesmo tempo:

1. `publish_mode: sidecar` em `config/config.yaml`
2. o override `compose.sidecar.yaml` (troca o bind de `/media` para
   leitura-escrita)

Uma das duas sozinha não publica sidecar. O processo ainda **nunca**
remuxa, recodifica, renomeia nem apaga o vídeo. Publicação usa hard-link
exclusivo: se o nome-alvo já existir, o resultado é `output_conflict`
(código de saída 5), nunca overwrite.

Publicar um job já aprovado, sem retranscrever:

```bash
docker compose -f compose.yaml -f compose.sidecar.yaml run --rm subtitles \
    nas-subs publish JOB_ID --config /config/config.yaml
```

Daemon contínuo em staging (padrão, mídia somente leitura):

```bash
docker compose -f compose.yaml -f compose.offline.yaml up -d
```

Daemon contínuo com sidecar (zero-touch, mídia gravável):

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.sidecar.yaml up -d
```

Dashboard opcional (outro contêiner; pará-lo não para o worker). Porta só
em loopback do host (`127.0.0.1:8787`):

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.dashboard.yaml up -d
```

A sequência completa (dry-run do scanner, piloto, recuperação) está em
[docs/runbook.md](docs/runbook.md).

## Passo a passo: local no Mac (sem Docker)

Mesmo código, caminhos de host. O padrão `--config` continua
`/config/config.yaml`; **passe sempre** um YAML absoluto. Copie o exemplo
e reescreva **todos** os caminhos para o Mac (`media_roots`, `state_dir`,
`work_dir`, `models_dir`, `output_dir`). Os valores `/media`, `/state`,
`/work`, `/models` e `/output` são do contêiner e não existem no host.

```bash
uv sync --frozen --group dev
cp config/config.example.yaml config/config.yaml
mkdir -p data/state data/work data/models data/output
```

Exemplo de trecho local (ajuste para a sua máquina):

```yaml
media_roots: [/Users/voce/Videos]
state_dir: /Users/voce/nas-subtitles/data/state
work_dir: /Users/voce/nas-subtitles/data/work
models_dir: /Users/voce/nas-subtitles/data/models
output_dir: /Users/voce/nas-subtitles/data/output
publish_mode: staging
```

```bash
uv run nas-subs models install --config /caminho/absoluto/config/config.yaml
uv run nas-subs models verify --offline --config /caminho/absoluto/config/config.yaml
uv run nas-subs doctor --config /caminho/absoluto/config/config.yaml
uv run nas-subs inspect /Users/voce/Videos/EPISODIO.mkv \
    --config /caminho/absoluto/config/config.yaml
uv run nas-subs process /Users/voce/Videos/EPISODIO.mkv \
    --preview-seconds 300 --config /caminho/absoluto/config/config.yaml
```

O worker contínuo (scan periódico + processamento serial):

```bash
uv run nas-subs daemon --config /caminho/absoluto/config/config.yaml
```

`daemon` e `worker` são o mesmo processo. Sidecar local não usa
`compose.sidecar.yaml`: basta `publish_mode: sidecar` e permissão de
escrita nos diretórios de mídia. O vídeo em si continua intocado.

## Integração com Sonarr e Radarr

O contrato implementado é o item **004**: um processo HTTP separado,
`nas-subs webhooks`, e o Compose `compose.webhooks.yaml`. Não são rotas do
dashboard. Parar o listener **não** para o worker nem o dashboard.

O webhook **só enfileira**. Quem transcreve é o `daemon`. O scan periódico
continua sendo a fonte de verdade: um POST perdido ou rejeitado é
recuperado na próxima reconciliação.

**Não há integração por Custom Script do Sonarr/Radarr.** Não existe
script oficial neste repositório para Connect → Custom Script. O caminho
implementado é Connect → Webhook (POST JSON). Chamar `nas-subs enqueue` à
mão é o CLI genérico, não o contrato 004.

**Isto não foi testado contra um Sonarr/Radarr ao vivo nem num NAS.** Os
testes cobrem payload, token, mapeamento de caminho e idempotência com
dobras sintéticas.

### Subir o listener

Defina o segredo **antes**. O processo recusa iniciar sem token
(`NAS_SUBS_WEBHOOK_TOKEN` no ambiente, ou `webhooks.token` no YAML; a
variável de ambiente ganha). Prefira o `.env`, não o YAML versionado.

```bash
# em .env, descomente e preencha:
# NAS_SUBS_WEBHOOK_TOKEN=um-segredo-longo
```

Docker (porta publicada só em loopback do host: `127.0.0.1:8788`):

```bash
docker compose -f compose.yaml -f compose.offline.yaml -f compose.webhooks.yaml up -d
```

Dentro do contêiner o bind é `0.0.0.0:8788` para o publish do Compose
funcionar. No host a publicação permanece `127.0.0.1:8788:8788`. Não
abra essa porta na internet.

Local no Mac:

```bash
NAS_SUBS_WEBHOOK_TOKEN=um-segredo-longo \
  uv run nas-subs webhooks --config /caminho/absoluto/config/config.yaml
```

Sem `--host`, o YAML padrão escuta `127.0.0.1:8788`. Health sem
autenticação: `GET /health`.

O worker precisa estar rodando em paralelo (`daemon` / serviço
`subtitles`). O serviço `webhooks` monta a mídia **somente leitura** e o
`state_dir`; ele não processa jobs.

### Configurar o Connect no Sonarr / Radarr

1. Settings → Connect → `+` → **Webhook**.
2. URL:
   - Sonarr: `http://127.0.0.1:8788/hooks/sonarr`
   - Radarr: `http://127.0.0.1:8788/hooks/radarr`
   - genérico: `http://127.0.0.1:8788/hooks`
3. Método: POST. Eventos de importação/upgrade (`On Import` / `On
   Upgrade` / `Download`). Outros eventos são ignorados (HTTP 200,
   `action: ignored`) e não enfileiram nada.
4. Autenticação — um destes, com o mesmo segredo:
   - `Authorization: Bearer <token>`
   - cabeçalho `X-Api-Key`
   - cabeçalho `X-Webhook-Token`
   - query `?token=` (funciona, mas cabeçalho é preferível)
5. Evento `Test` é reconhecido (`action: acknowledged`) e não cria job.
6. Sem token, ou token errado: HTTP 401, nada entra na fila. Payload
   malformado ou caminho fora de `media_roots`: falha fechada, nada
   enfileirado.

Se o Sonarr/Radarr roda **noutro contêiner**, `127.0.0.1` lá dentro não é
este host. Use uma rede Docker compartilhada e o nome de serviço
`webhooks`, ou outro endereço LAN documentado. Não publique a porta além
do loopback sem entender a exposição.

### Caminhos: host versus contêiner

O JSON do *arr traz o caminho do arquivo importado
(`episodeFile.path`, `movieFile.path`, ou as listas `episodeFiles` /
`movieFiles`). Esse caminho precisa, depois do mapeamento, cair dentro de
um `media_roots` **como o processo de webhooks vê o disco**.

Se o *arr reporta caminhos de host diferentes do mount do contêiner,
configure `webhooks.path_maps` (prefixo; o resultado ainda tem de ficar
dentro de `media_roots`):

```yaml
webhooks:
  bind: 127.0.0.1
  port: 8788
  path_maps:
    - host_prefix: /nas/media
      container_prefix: /media
```

Barras invertidas estilo Windows são normalizadas para `/`. Mapas
Windows → POSIX dependem de `path_maps` explícitos; o código não inventa
drive letters.

Importações já concluídas pelo *arr **não** passam de novo pela janela de
estabilidade (`require_stability=False`). O scan periódico, esse sim,
exige tamanho e `mtime` estáveis em duas observações.

Um segundo webhook para o mesmo fingerprint devolve o job existente
(`already_queued`). Cada idioma em `languages.targets` é um job
independente.

`bind`, `port`, `token` e `path_maps` ficam fora de `pipeline_config_hash`:
mudar o listener não invalida transcrições.

Não é preciso — e este projeto **não usa** — chave de API do Jellyfin nem
do Bazarr.

## Conviver com Bazarr e Jellyfin

**Um produtor por título.** O Bazarr pode indexar o `.pt-BR.srt` que esta
ferramenta cria e depois substituí-lo por uma legenda baixada. Depois do
piloto, tire o perfil do Bazarr dos títulos daqui, ou desative upgrades
nessa abrangência, nas opções da **sua** versão do Bazarr. Este projeto
não altera o Bazarr e não precisa de API key.

No Jellyfin, confirme primeiro que o SRT existe e parseia (ler o arquivo,
`ffprobe`). Só se ainda não aparecer, atualize a biblioteca pelo
dashboard. Também sem API key.

## Como a biblioteca é protegida

- Bind de mídia **somente leitura** por padrão. Sidecar exige
  `publish_mode: sidecar` **e** `compose.sidecar.yaml` (no Docker).
- Vídeos nunca são remuxados, recodificados, renomeados nem apagados.
  Metadados do container de vídeo não são escritos.
- Arquivo já existente no nome-alvo **nunca** é sobrescrito
  (`output_conflict`).
- Vídeo que já tem legenda portuguesa completa (externa ou embutida) é
  ignorado. `forced` não conta como completa.
- Fora do webhook de importação, a fila só aceita arquivo estável em
  duas observações (tamanho + mtime), para não processar cópia em
  andamento.
- `cleanup` só apaga dentro de `work_dir` e só com job ID validado. Nunca
  por glob, nunca na mídia.

## Comandos

Não existe `nas-subs dub`. Os comandos que o CLI realmente expõe:

```text
nas-subs doctor              ambiente, caminhos, recursos, publish atômico, modelos
nas-subs models install      baixa Whisper e o par Argos direto (única etapa com rede)
nas-subs models verify       confirma que os modelos carregam offline
nas-subs inspect PATH        streams, duração e a faixa de áudio escolhida
nas-subs process PATH        um arquivo na frente da fila (mesmo lock e mesmas regras)
nas-subs enqueue PATH        coloca um arquivo na fila (com checagem de estabilidade)
nas-subs scan                uma passada nos roots; --dry-run não enfileira
nas-subs worker              daemon: scan periódico + processamento serial
nas-subs daemon              alias de worker; entrypoint do serviço Compose
nas-subs jobs list|show|retry|cancel|approve
nas-subs publish JOB_ID      publica job aprovado sem retranscrever
nas-subs benchmark PATH      vazão e memória numa janela curta (desta máquina, não do NAS)
nas-subs health              heartbeat + banco; healthcheck do Docker
nas-subs cleanup             intermediários em work_dir; dry-run até --apply
nas-subs backup              backup consistente de config, manifesto e SQLite
nas-subs dashboard           UI opcional; não processa jobs nem toma o lock
nas-subs webhooks            listener Sonarr/Radarr; não processa jobs nem toma o lock
```

Todo comando aceita `--json` e nenhum pergunta nada. Códigos de saída
(`ExitCode`): `0` sucesso, `2` argumento/config inválidos, `3` preflight,
`4` falha de processamento, `5` revisão ou conflito, `6` lock do worker
já preso.

`--preview-seconds` em `process` escreve em staging com `.preview` no
nome. `scan` no CLI já é uma passada (`--once` é aceito; o loop periódico
é o `daemon`).

## Configuração

Copie `config/config.example.yaml`; cada opção está documentada lá.
Caminhos absolutos. Padrões relevantes:

- `publish_mode: staging`
- Whisper `small`, `device: cpu`, `compute_type: int8`
- chunks de 300 s com 2 s de overlap
- legendas de até 2 linhas × 42 caracteres
- `languages.source: auto`, `languages.targets: [pt-BR]`
- `translation.allowed_pairs: [en:pt]` (família pública; Argos `pb` é
  interno)
- `scan_interval_seconds` / `stability_window_seconds` /
  `minimum_file_age_seconds`: 600
- `worker.concurrency: 1`
- `webhooks.bind: 127.0.0.1`, `webhooks.port: 8788`, `path_maps: []`

Vários destinos (`pt-BR`, `en`, `es`, …) geram um job por idioma e um
arquivo `.<tag>.srt`. `models install` ainda só instala o par do destino
primário.

## Documentação

- [docs/architecture.md](docs/architecture.md) — como as peças se encaixam
- [docs/runbook.md](docs/runbook.md) — instalação, piloto e recuperação
- [docs/benchmark.md](docs/benchmark.md) — o que foi medido e o que está bloqueado
- [docs/decisions.md](docs/decisions.md) — por que as coisas são assim
- [docs/roadmap/004-sonarr-radarr-webhooks.md](docs/roadmap/004-sonarr-radarr-webhooks.md)
  — contrato 004 (webhooks)
- [AGENTS.md](AGENTS.md) — regras e posse de módulos para quem contribui
- [ROADMAP.md](ROADMAP.md) — 000–005 feitos; 006 dublagem planejada, não implementada

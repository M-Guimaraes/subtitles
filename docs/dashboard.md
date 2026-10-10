# Dashboard — o que é e o que cada tela mostra

Este documento detalha a UI web opcional do `nas-subs` (`nas-subs dashboard`):
de onde vêm os dados, o que cada tela exibe e quais ações ela permite. É uma
leitura, não uma mudança de código — serve para desembaraçar a interface
HTML/CSS/JS que fica em [`src/nas_subtitles/static/`](../src/nas_subtitles/static/).

## 1. O que o dashboard é (e o que não é)

- É um **processo HTTP separado** do worker (`nas-subs dashboard`), implementado
  com `http.server` puro (nenhum framework web) em
  [`dashboard.py`](../src/nas_subtitles/dashboard.py). Ele nunca pega o
  `worker.lock` e nunca roda inferência (Whisper/Argos). Parar o dashboard não
  para o processamento da fila.
- Ele é **somente leitura sobre a fila**, com três ações pontuais possíveis
  por job: `retry`, `cancel`, `reprocess` e `delete` (só jobs finalizados). Ele **nunca escreve** em
  `config.yaml` — a tela "Settings" espelha a config carregada, e a única
  coisa editável ali são as **bibliotecas de mídia** (ver seção 8).
- Todo acesso a dados passa por uma camada de serviço,
  [`api.py`](../src/nas_subtitles/api.py) (`DashboardService`), que reusa o
  mesmo `JobRepository`, `discovery.scan` e `health.check_health` que o CLI e o
  worker usam. Não existe lógica de pipeline duplicada na UI.
- O front-end é HTML/CSS/JS estático, servido pelo próprio `dashboard.py`:
  [`index.html`](../src/nas_subtitles/static/index.html),
  [`dashboard.css`](../src/nas_subtitles/static/dashboard.css) e
  [`dashboard.js`](../src/nas_subtitles/static/dashboard.js). Não há build
  step, bundler ou framework de frontend — é uma SPA simples feita à mão, que
  conversa com uma API JSON (`/api/...`) via `fetch`.

### Como iniciar

```bash
nas-subs dashboard --config /config/config.yaml
# opcional: --host / --port para sobrepor dashboard.bind/dashboard.port
```

Bind padrão: `127.0.0.1:8787` (loopback — não exposto na LAN por padrão).
Configurável em `config.yaml` → `dashboard.bind` / `dashboard.port` / `dashboard.token`.
Se `dashboard.token` (ou a env var `NAS_SUBS_DASHBOARD_TOKEN`) estiver
definido, toda rota `/api/*` exige `Authorization: Bearer <token>`; sem
token configurado, a API fica aberta para quem alcançar o bind. **Nunca
exponha isso na internet pública sem um token** — é o próprio aviso que
aparece na tela "Settings".

## 2. Layout geral

A página tem um cabeçalho fixo (`<header class="top">`) com:

- **Marca** — "nas-subs" + "local subtitle queue".
- **Navegação** — três botões: `Queue`, `History`, `Settings`. Trocar de aba
  não recarrega a página; é só JS trocando qual `<section>` fica visível.
- **Pílula de status do worker** (canto direito) — `worker up` (verde) ou
  `worker down` / `api error` (vermelho). Vem de `GET /api/health`, chamado a
  cada troca de tela e a cada 15s de auto-refresh.

O corpo (`<main>`) tem três seções que se alternam (nunca duas visíveis ao
mesmo tempo): **lista** (Queue/History compartilham o mesmo HTML, só mudam o
filtro e o título), **detalhe de um job** e **Settings**.

## 3. Tela "Queue"

Mostra jobs **ativos ou esperando**. Fonte: `GET /api/jobs?view=queue`.

Estados incluídos nessa view (`QUEUE_STATES` em `api.py`):
`queued`, `running`, `retry_wait`, `needs_review`, `ready_to_publish`.

**Barra de ferramentas:**
- Filtro "State" — restringe a um único estado dentro da view atual (os
  `<option>` são preenchidos dinamicamente com os estados da view).
- **Rescan library** — chama `POST /api/scan`, que roda uma varredura
  (`discovery.scan`) e mostra um banner com `examined`/`enqueued`. Não toca o
  `worker.lock`.
- **Refresh** — recarrega a lista manualmente (além do auto-refresh de 15s).

**Colunas da tabela** (uma linha por job, clicável → abre o detalhe):

| Coluna | De onde vem | Observação |
|---|---|---|
| Title | nome do arquivo (`Path(relative_path).name`) + `root_id` / caminho relativo em cinza | |
| State | `job.state` | cor por estado (ver §6) |
| Stage | `job.current_stage` | estágio do pipeline em que o job está agora |
| Source | idioma detectado/origem + probabilidade (`en 93%`) | vem do manifest da legenda ou, se ainda não existe, do último evento `language_decision` |
| Target | `job.target_language` | idioma de destino desse job específico (multi-target = um job por idioma) |
| Updated | `job.updated_at` | formatado, sem milissegundos, `Z` em vez de `+00:00` |
| Error | `job.error_code` | vazio quando não há erro |

## 4. Tela "History"

Mesmo layout da Queue, mas `view=history` e estados
(`HISTORY_STATES`): `completed`, `skipped`, `failed`, `cancelled`.

## 5. Tela de detalhe de um job

Abre ao clicar numa linha (`GET /api/jobs/<id>`). Mostra:

- **Cabeçalho** — título do job, caminho completo (`root_id / relative_path`)
  e botões de ação disponíveis para o estado atual (`payload.actions`):
  - `retry` — só aparece se `state == failed`; reenfileira (`state → queued`).
  - `cancel` — aparece sempre que a transição `→ cancelled` é permitida.
  - `reprocess` — aparece para jobs terminados (`completed`, `skipped`,
    `failed`, `cancelled`); reenfileira **mantendo os checkpoints** (chunks de
    transcrição e cache de tradução já feitos não são refeitos).

- **Card "Status"** — estado, estágio atual, número de tentativas
  (`attempt_count`), última atualização, código/detalhe do erro quando houver.

- **Card "Language"** — tudo o que a decisão de idioma produziu: idioma de
  origem efetivo, idioma detectado + probabilidade, se a decisão foi
  confiante, a fonte da decisão (`override`, `metadata` ou `detection`), o
  idioma de destino, se a tradução foi executada e o motivo (`reason`) quando
  a decisão não foi confiante. Vem do manifest JSON gravado pelo pipeline
  (`write_manifest`) ou, na ausência dele, do evento `language_decision`
  gravado no banco.

- **Card "Audio / output"** — índice da faixa de áudio escolhida, idioma
  marcado no stream, caminho do `.srt` de saída, número de cues geradas e as
  flags de qualidade acumuladas (dedupe de ASR, cues, roundtrip do SRT).

- **Card "Models"** — lista `kind: name` de cada modelo usado (ASR, tradução),
  tirada do manifest.

- **Card "Pipeline"** — chips, um por estágio da sequência daquele tipo de
  job (legenda ou dublagem — ver §7), cada um pintado como `pending`,
  `current` (estágio atual, destacado) ou `done` (já passou).

- **Card "Recent events"** — até 8 eventos recentes (`código (nível)`) do
  histórico de eventos do job (tabela de eventos no SQLite).

## 6. Cores de estado

Definidas em `dashboard.css` via variáveis CSS e a classe `.state.<estado>`:

| Estado | Cor | Significado |
|---|---|---|
| `queued`, `ready_to_publish` | azul (`--queued`) | esperando a vez / esperando aprovação |
| `running`, `completed` | verde (`--ok`) | em andamento / terminado com sucesso |
| `retry_wait`, `needs_review` | amarelo (`--warn`) | aguardando nova tentativa / precisa de revisão humana |
| `failed`, `cancelled` | vermelho (`--bad`) | erro / cancelado |
| `skipped` | cinza (`--muted`) | já havia legenda para esse destino |

O mesmo esquema de cor é usado nos chips de estágio do pipeline (`done` =
verde, `current` = azul de destaque, `pending` = sem cor).

## 7. Estágios do pipeline mostrados no card "Pipeline"

A lista de estágios depende do tipo de job (`job_kind`), definida em
`domain.py`:

- **Legenda** (`subtitles`): `probe → detect_language → extract →
  transcribe → merge → translate → render → validate → publish`.
- **Dublagem** (`dubbing`, roadmap 006 — ainda em fase 1): `probe →
  detect_language → extract → separate → transcribe → merge → translate →
  adapt → synthesize → sync → mix → validate_audio → publish`.

O dashboard não sabe nada de dublagem "por fora": ele só pede
`stages_for(job.job_kind)` e desenha os chips que vierem.

## 8. Tela "Settings"

Leitura (`GET /api/settings`), exceto as bibliotecas de mídia. Mostra, em cards:

- **Automatic processing** — se o heartbeat do worker está recente
  (`health.check_health`) e o motivo quando não está.
- **Media roots** — os `root_id` e caminhos configurados. **Editável**
  (`POST /api/settings/media-roots`, corpo `{"paths": [...]}` ou
  `{"reset": true}`): o valor novo é salvo em `state_dir/runtime-settings.json`
  e sobrepõe o `media_roots` do `config.yaml` (que nunca é reescrito;
  `load_config` aplica o override, então worker e CLI também o veem). Regras:
  caminhos absolutos, existentes, legíveis pelo serviço (dentro do contêiner,
  se Docker), sem repetir/aninhar e sem conter `state_dir`/`work_dir`/
  `models_dir`/`output_dir`; remover uma biblioteca com job não finalizado
  é recusado. **O worker só enxerga o novo valor depois de reiniciar** (a tela
  avisa). A edição só vale com bind em loopback ou com token configurado;
  num bind público sem token a API devolve `writable: false` e recusa. "Restaurar
  do config.yaml" apaga o override.
- **Languages** — idioma de origem configurado (`auto` ou fixo), lista de
  idiomas de destino (`languages.targets`) e a política de baixa confiança.
- **Audio** — preferência de seleção de faixa (`audio.stream`) e idiomas de
  faixa preferidos, em ordem.
- **Processing** — intervalo de varredura, política para legenda já
  existente, modo de publicação (`staging` ou `sidecar`) e o modelo ASR
  configurado.
- **Dashboard** — o próprio bind/porta do dashboard e se um token está
  configurado (nunca mostra o valor do token), com o aviso de não expor isso
  publicamente sem autenticação.

## 9. API consumida pela UI

Toda a UI fala com estas rotas (todas sob `/api/`, todas JSON,
`Content-Type: application/json`):

| Método | Rota | Função em `api.py` | Uso na UI |
|---|---|---|---|
| GET | `/api/health` | `overview()` | pílula de status do worker |
| GET | `/api/overview` | `dashboard_overview()` | **não consumida pela UI atual** — adicionada na Fase 1 do Dashboard v2 (ver `docs/dashboard-v2-audit.md`); devolve `worker`, `stats` (buckets mutuamente exclusivos), `active_jobs`/`attention_jobs` (até 5) e `recent_activity` (até 10, hoje só `language_decision`) |
| GET | `/api/jobs?view=&state=&limit=` | `list_jobs()` | tabelas Queue/History |
| GET | `/api/jobs/<id>` | `get_job()` | tela de detalhe |
| POST | `/api/jobs/<id>/retry` | `retry_job()` | botão "Retry" |
| POST | `/api/jobs/<id>/cancel` | `cancel_job()` | botão "Cancel" |
| POST | `/api/jobs/<id>/reprocess` | `reprocess_job()` | botão "Reprocess" |
| POST | `/api/jobs/delete` | `delete_jobs()` | excluir (linha, seleção em lote ou detalhe) |
| POST | `/api/scan` | `rescan()` | botão "Rescan library" |
| GET | `/api/settings` | `settings()` | tela Settings |
| POST | `/api/settings/media-roots` | `update_media_roots()` | editar bibliotecas |

Erros voltam como `{"ok": false, "error_code": ..., "message": ..., "exit_code": ...}`
com o HTTP status mapeado em `_status_for` (404 para job não encontrado, 401
para token ausente/errado, 409 para transição de estado inválida, 400 para o
resto).

### 9.1 Extensões da Fase 1 do Dashboard v2 (backend, ainda sem UI nova)

`GET /api/jobs` aceita, além de `view`/`state`/`limit` (que continuam
funcionando exatamente como antes):

| Parâmetro | Exemplo | Validação |
|---|---|---|
| `state` | `state=failed,needs_review` | agora aceita uma lista separada por vírgula, além de um único valor |
| `search` | `search=dexter` | substring de `relative_path`, case-insensitive; não é SQL, não há injeção possível |
| `kind` | `kind=dubbing` | `subtitles` ou `dubbing`; valor desconhecido → 400 |
| `target_language` | `target_language=en` | sem validação de enum (vem de config, não do domínio) |
| `sort` | `sort=title_asc` | `updated_desc` (padrão), `updated_asc`, `created_desc`, `created_asc`, `title_asc`, `title_desc`; valor desconhecido → 400 |
| `offset` | `offset=20` | inteiro ≥ 0 |

A resposta ganhou um campo `pagination` (aditivo — `jobs` continua existindo
como antes, nenhum cliente antigo quebra):

```json
{"ok": true, "view": "all", "jobs": [...], "pagination": {"limit": 20, "offset": 0, "total": 142, "has_next": true}}
```

`GET /api/jobs/<id>` ganhou um campo `progress` (também aditivo), o "modo
stage" do contrato de progresso: só a posição no pipeline, nunca um
percentual fabricado — `measured` exigiria o worker persistir o total de
chunks, o que ele não faz hoje.

```json
{"progress": {"mode": "stage", "stage_index": 6, "stage_total": 9, "stage_percent": null, "overall_percent": null}}
```

**Nenhuma dessas extensões mudou `dashboard.js`/`index.html`/`dashboard.css`
ainda** — é só o backend da Fase 1. A UI atual continua exatamente como
descrita nas seções 1 a 8 deste documento até a Fase 2+ do plano trocar o
frontend.

## 10. Autenticação e sessão no navegador

Quando `dashboard.token` está configurado, a primeira chamada sem o header
`Authorization` recebe `401`. O JS (`dashboard.js`, função `api()`) reage
abrindo um `window.prompt("Dashboard token")`, guarda o valor digitado em
`sessionStorage` (`nas-subs-dashboard-token`) e repete a chamada original. O
token nunca é persistido em disco pelo navegador (só dura a aba/sessão) e
nunca é escrito em `config.yaml` pela UI.

## 11. Atualização automática

Fora da tela de detalhe e da tela Settings, a lista (Queue/History) é
recarregada a cada 15 segundos (`setInterval` em `dashboard.js`). Não há
websocket nem server-sent events — é polling simples. A tela de detalhe e a
de Settings só recarregam quando você entra nelas ou aperta "Refresh"/repete
uma ação.

## 12. Limitações conhecidas

- Não edita `config.yaml` — qualquer mudança de configuração exige editar o
  arquivo e reiniciar o worker (e, separadamente, reiniciar o processo do
  dashboard se a mudança afetar bind/porta/token).
- Não mostra logs brutos nem o conteúdo do `.srt` gerado — só caminho, cues e
  flags de qualidade.
- A ação "Reprocess" reenfileira o job mas não força re-transcrição: os
  checkpoints de chunk (ASR) e o cache de tradução continuam sendo
  reaproveitados se ainda baterem fingerprint/hash de configuração.
- Sem HTTPS nativo; é pensado para LAN confiável atrás do bind loopback (ou
  de um proxy reverso, se exposto).

## Excluir jobs

`POST /api/jobs/delete` com `{"ids": [...]}` (até 200). Só jobs finalizados
(`completed`, `skipped`, `failed`, `cancelled`); um job em fila ou rodando vai
para `skipped` na resposta (`not_finished`) e deve ser cancelado antes. Apaga as
linhas do banco (`jobs`, `artifacts`, `events`, `metrics`, `dub_segments`,
`voice_assignments`, `synthesis_artifacts`), o manifesto em `state_dir/manifests/`
e a pasta `work_dir/<job_id>` daquele job (id validado, nunca por glob). **Não**
apaga a biblioteca, uma legenda já publicada nem saídas em `output_dir`. O cache
de tradução é compartilhado e fica. Como o job some, uma nova varredura pode
recriá-lo se o arquivo ainda não tiver legenda.

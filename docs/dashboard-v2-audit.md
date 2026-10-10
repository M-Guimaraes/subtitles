# Dashboard v2 — auditoria (Fase 0)

Levantamento pedido pela Fase 0 do plano "nas-subs — Dashboard v2": inspeção
do código real antes de qualquer implementação. Nenhuma linha de produção foi
alterada para produzir este documento. Veja também
[dashboard.md](dashboard.md) para o detalhamento do dashboard **atual**.

## 1. Contratos JSON atuais (`api.py` / `dashboard.py`)

Confirmados por leitura direta, não por suposição:

- `GET /api/health` → `overview()`: contagens por estado, saúde do worker,
  `target_language`/`target_languages`. **Não existe hoje** um
  `GET /api/overview` separado — o plano propõe criar essa rota nova; é
  aditiva, não quebra nada.
- `GET /api/jobs` → `list_jobs(view, state, limit)`. Aceita só **um** `state`
  (não uma lista), não tem `search`, `kind`, `target_language`, `sort` nem
  `offset`. `limit` vai de 1 a 10000, sem paginação (`total`/`has_next`).
- `GET /api/jobs/<id>` → `get_job()`: job, `stages` (progresso por estágio),
  `language`, `models`, `artifacts`, `metrics`, até 50 `events` (a UI só
  consome os 8 primeiros), `actions`.
- `POST /api/jobs/<id>/retry|cancel|reprocess`, `POST /api/scan`,
  `GET /api/settings`: como documentado em `dashboard.md`.
- Erros: `{"ok": false, "error_code", "message", "exit_code"}` com HTTP
  status já mapeado por tipo de erro (404 não encontrado, 401 sem token, 409
  transição inválida, 400 resto) — **o requisito do plano §11.3 sobre 409 em
  conflito de estado já está implementado.**

## 2. Testes existentes

Único arquivo: [`tests/unit/test_dashboard.py`](../tests/unit/test_dashboard.py)
(9 testes, via `DashboardService` direto e via HTTP com `HTTPConnection`):
separação queue/history, detalhe com idioma e retry, rejeição de retry fora
de `failed`, cancel/reprocess respeitando a máquina de estados, settings
read-only, rescan sem lock do worker, listagem+retry via HTTP, token
obrigatório quando configurado, preenchimento de idioma a partir de evento
quando não há manifest ainda. Não há testes de `search`, `sort`, paginação,
múltiplos estados ou do endpoint `/api/overview` — porque nada disso existe
ainda.

## 3. Todos os estados de job (`JobState`, `domain.py`)

Exatamente 9, sem mais nem menos:
`queued`, `running`, `retry_wait`, `needs_review`, `ready_to_publish`,
`completed`, `skipped`, `failed`, `cancelled`. O mapeamento de cor/label do
plano (§10) cobre os 9 corretamente.

## 4. Todos os códigos de evento persistidos — achado crítico

Busquei todo lugar que grava um `JobEvent` (`repository.append_event`) no
código de produção. **Só existem dois códigos reais hoje:**

- `"language_decision"` — um evento por job, gravado uma vez em
  [`pipeline.py`](../src/nas_subtitles/pipeline.py) quando a detecção de
  idioma termina.
- `"worker_heartbeat"` (`HEARTBEAT_EVENT_CODE`) — evento do **worker**, não
  de um job (`job_id=None`), gravado a cada heartbeat.

Não existe `transcription_started`, `translation_failed`, `job_completed`
nem qualquer evento por mudança de estágio/estado. `repository.transition()`
(chamado em toda mudança de estado/estágio) **só faz um `UPDATE` na linha do
job** — não grava nenhuma linha na tabela `events`. Os exemplos do plano em
§15.2 (`transcription_started`, `translation_failed`) são ilustrativos e **não
existem no banco**, exatamente como o próprio plano avisa em §15.2 para o
agente verificar antes de assumir.

**Consequência para o plano:** as seções 6.3 (`recent_activity`) e 15 (event
timeline legível) descrevem um histórico de atividade que a instrumentação
atual não produz. Dá para listar os eventos que existem (`language_decision`
+ heartbeat), mas uma timeline "14:32 arquivo identificado / 14:34 áudio
extraído / 14:35 transcrição iniciada" exigiria **adicionar novos
`append_event()` em `pipeline.py`/`worker.py`/`discovery.py`** — módulos que
pertencem às stages 7 e 3, não à stage "003. dashboard" (que só pode tocar
`api.py`, `dashboard.py`, `static/`, por `AGENTS.md`). Isso é uma dependência
cross-stage que precisa ser decidida explicitamente antes de prometer a
timeline da seção 15, e não é um trabalho "só de frontend".

## 5. Progresso disponível de fato

`list_artifacts(job_id, stage=...)` dá a contagem real de chunks já
transcritos (uma linha em `artifacts` por chunk concluído em `TRANSCRIBE`).
Isso é uma fonte legítima de progresso **medido**, não fabricado. Mas:

- O total de chunks planejados (`duration / chunk_seconds`) não é persistido
  em lugar nenhum — só existe em memória durante a execução do job. Dar
  "chunk 4 de 9" exigiria calcular o total a partir de `metrics.media_seconds`
  (só gravado **ao final** do job, não durante) ou persistir esse número em
  algum lugar novo.
- Isso confirma que a cautela do plano em §13.1 ("usar progresso por estágio,
  não percentual fabricado") está correta. O modo `measured` do contrato de
  §13.2 **não é trivial de preencher ainda** — precisa de uma decisão sobre
  onde persistir o total de chunks, de novo cruzando para `pipeline.py`.

## 6. Esquema SQLite (`migrations/001_initial.sql` a `004_job_kind.sql`)

- Tabela `jobs`: sem coluna de "título" (é derivado de `relative_path` em
  tempo de leitura); índice único em
  `(root_id, relative_path, fingerprint, pipeline_config_hash, job_kind)`
  só quando `execution_scope = 'full'`; índice de claim em
  `(state, priority DESC, created_at)`. **Não há índice para busca por nome**
  — uma busca por `relative_path LIKE` faz table scan. Aceitável no tamanho
  de biblioteca que este projeto assume (NAS doméstico), mas vale registrar.
- `repository.list_jobs()` só aceita **um** `state` e não tem `offset`,
  `search`, `sort` nem `COUNT(*)` para paginação. Implementar §8 do plano
  (busca, filtros combinados, paginação, ordenação) significa **editar
  `repository.py`**, que pertence à stage "2. state" — de novo, fora do que
  "003. dashboard" pode tocar sozinho segundo a tabela de ownership do
  `AGENTS.md`. Não é bloqueador, mas precisa ser assumido deliberadamente
  (a própria regra do `AGENTS.md`: "Changing a shared contract... do it
  deliberately and say so").
- Tabela `events` tem índice em `(code, created_at)`, não em `job_id` —
  listar eventos de um job filtra por `job_id` sem índice dedicado; não é
  problema no volume atual, mas não escala tão bem quanto parece.

## 7. Diferenças legenda vs. dublagem

- Sequência de estágios (`stages_for(job_kind)`, `domain.py`): 9 estágios
  para legenda, 13 para dublagem — exatamente como o plano lista em §12.2/12.3.
- **Dublagem hoje só implementa o estágio `probe`** (roadmap 006, fase 1;
  confirmado em `dubbing.py`: qualquer estágio além de `probe` levanta
  `not_implemented`). Isso significa que o card "Resultado — Dublagem" (§14.2)
  vai mostrar "Ainda não disponível" para praticamente tudo além da faixa de
  áudio selecionada, até que as fases 2+ do roadmap 006 avancem. Isso está
  alinhado com a Regra 7 do próprio plano ("não inventar dados ausentes") —
  só reforçando que não é um bug da v2, é o estado real do backend.
- `job_summary()` já expõe `job_kind` e `dubbing_profile` por job; a
  listagem unificada (§9, Fase 4) não precisa de campo novo para distinguir
  os dois tipos.

## 8. Coisas que o plano pede e que já existem (sem trabalho adicional)

- SPA fallback para 404: `_serve_static()` em `dashboard.py` já cai para
  `index.html` em qualquer rota GET fora de `/api/*` que não seja um arquivo
  real — o requisito do §5.1 ("refresh em rota interna não pode dar 404")
  **já está satisfeito**, incluindo para as rotas novas (`/jobs`, `/jobs/:id`,
  `/settings`) sem mudar `dashboard.py`.
- Servir arquivos estáticos aninhados (`static/js/pages/overview.js` etc. da
  estrutura do §3): `_serve_static` resolve qualquer caminho relativo sob
  `STATIC_DIR` com proteção de path traversal (`relative_to`) — a estrutura
  de diretórios proposta funciona sem tocar `dashboard.py`.
- CORS: hoje não há `Access-Control-Allow-Origin` nos headers — ou seja, já
  não é permissivo; nada a reforçar aqui além de manter como está.
- 409 em transição de estado inválida: já mapeado (`_status_for`). O plano
  pede exatamente isso em §11.3.

## 9. Ajustes recomendados ao planejamento

1. **Separar explicitamente os itens que cruzam stage.** Pelo menos três
   itens do plano não são "frontend" nem puramente "003. dashboard":
   - extensão de `repository.list_jobs` (busca/filtro/paginação/ordenação) →
     stage 2 (`repository.py`);
   - qualquer evento novo para alimentar "atividade recente"/timeline →
     stages 3/7 (`discovery.py`, `pipeline.py`, `worker.py`);
   - progresso medido (`measured` mode) → stage 7 (`pipeline.py`/`worker.py`).
   Nenhum desses é implementável "só no dashboard" como o título do documento
   sugere; recomendo que a Fase 1 do plano trate isso como duas frentes
   (extensão de `repository.py` + eventuais novos `append_event`), com
   aprovação explícita antes de editar módulos fora da stage 003.
2. **Rebaixar a ambição da Fase 1 em relação a eventos.** Com só dois
   códigos de evento existindo, a "atividade recente" inicial só pode
   mostrar decisões de idioma e heartbeats — não "transcrição iniciada" nem
   "falha na tradução" por job. Ou se aceita essa limitação na v1 do
   Dashboard v2, ou se abre instrumentação nova como pré-requisito explícito
   (fora do escopo atual, por tocar `pipeline.py`).
3. **Registrar isso como item de roadmap.** `ROADMAP.md` trata o item 003
   (dashboard atual) como **DONE**. Este plano de v2 não tem número de
   roadmap ainda; se for seguir adiante, sugiro abrir um item novo (ex.: 007)
   em `ROADMAP.md`/`docs/roadmap/`, já que o projeto trata isso como a fonte
   da verdade para "o que está ativo agora" (ver `AGENTS.md`).
4. **Título/busca por nome**: sem coluna dedicada nem índice, aceitável no
   volume atual; se a biblioteca for grande, considerar índice em
   `relative_path` junto com a extensão do método de busca (não bloqueia a
   Fase 1, só uma nota de performance).

## 10. O que a Fase 0 **não** encontrou de errado

Nenhuma incompatibilidade de contrato, nenhuma dependência oculta e nenhum
teste existente que a Fase 1 quebraria com as extensões aditivas descritas no
próprio plano (novos parâmetros em `/api/jobs`, novo `/api/overview`). A
base do plano está coerente com o código real; os ajustes acima são sobre
**quem** pode implementar o quê dentro das regras de ownership do projeto, e
sobre não prometer uma riqueza de dados (eventos, progresso medido) que a
instrumentação atual não produz — não sobre o desenho da v2 em si.

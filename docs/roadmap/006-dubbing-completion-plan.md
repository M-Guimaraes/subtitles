# Dublagem pt-BR — plano para o fluxo completo

Companion de [`006-dubbing-pt-br.md`](006-dubbing-pt-br.md) (o plano original,
escrito antes da implementação começar). Este documento parte do **código
real hoje** — não do que o plano original previa — e detalha o que falta,
fase por fase, para a dublagem produzir áudio de verdade. Referências de
arquivo/linha foram conferidas na árvore atual; releia-as se o código mudar.

## 1. Estado real verificado

| Peça | Existe? | Onde |
|---|---|---|
| `JobKind.DUBBING`, schema 4, tabelas `dub_segments`/`voice_assignments`/`synthesis_artifacts` | **Sim** | [`migrations/004_job_kind.sql`](../../src/nas_subtitles/migrations/004_job_kind.sql) |
| Contratos de domínio (`DubSegment`, `VoiceAssignment`, `SynthesisArtifact`, `DubbingQualityReport`) | **Sim** | [`domain.py:965-1023`](../../src/nas_subtitles/domain.py) |
| Protocolos de engine (`DialogueSeparator`, `SpeechSynthesizer`, `TimelineRenderer`, `AudioMixer`) | **Sim, só a assinatura** | [`domain.py:1310-1359`](../../src/nas_subtitles/domain.py) — nenhuma implementação |
| `nas-subs dub enqueue` / `dub process` | **Sim** | [`dubbing.py:87`](../../src/nas_subtitles/dubbing.py), [`cli.py:617`](../../src/nas_subtitles/cli.py) |
| `nas-subs dub plan export/apply` (edição manual de falas) | **Sim**, com controle de revisão e rejeição de revisão obsoleta | [`dubbing.py:150-235`](../../src/nas_subtitles/dubbing.py), testado em `test_dubbing.py` |
| Estágio `probe` (seleciona faixa de áudio) | **Sim** | `run_dubbing_job` em [`dubbing.py`](../../src/nas_subtitles/dubbing.py) |
| Estágio `detect_language` | **Sim** | mesmo `run_dubbing_job` — reusa `language.py`'s `decide_source_language`/`effective_source_override`/`is_supported_source` (funções públicas, nunca os helpers privados de `pipeline.py`) mais um `_dub_language_samples` local; grava evento `language_decision`; mesmos desfechos de legenda (confiante → segue, baixa confiança → `needs_review`, idioma não suportado → `failed`). |
| Estágio `extract` | **Sim** | mesmo `run_dubbing_job` — planeja os chunks com `media.plan_chunks` (mesma função pública que a legenda usa) e chama `context.extractor.extract` por chunk. Nenhum checkpoint próprio ainda (mesma convenção da legenda: extração é barata e refeita a cada corrida, só a transcrição é que será cacheada quando existir). |
| Qualquer estágio depois de `extract` (`separate`, `transcribe`, `translate`, `adapt`, `synthesize`, `sync`, `mix`, `validate_audio`, `publish`) | **Não** | mesmo `run_dubbing_job`: levanta `NasSubtitlesError(code=NOT_IMPLEMENTED, detail={"stage": "separate"})` para qualquer estágio a partir daqui |
| Checkpoint/retomada de dublagem (`start_stage` além de `probe`) | **Não** | nada persiste a decisão de idioma nem os chunks extraídos para um dub job ainda (diferente do que já existe para legenda); listado como fase 4 abaixo |
| Instalação de modelos TTS/separação (`nas-subs models install`) | **Sim** | `models.py`'s `_install_piper`/`_install_demucs` baixam a voz Piper `pt_BR-faber-medium` (via `piper.download_voices.download_voice`) e o modelo de separação Demucs `htdemucs` (via `demucs.pretrained.get_model`, cache HF isolado em `separation_models_dir`), gravados no mesmo `models.json` que Whisper/Argos. `verify_models` carrega os dois 100% offline (`PiperVoice.load` / `get_model` com `HF_HUB_OFFLINE=1`) em vez de só checar presença em disco. O checksum do Demucs é calculado sobre os tensores do modelo carregado, não sobre o diretório de cache do HF — esse cache materializa blobs compartilhados (Xet) de forma preguiçosa e instável entre execuções, o que tornava o hash do diretório não-reprodutível |
| `doctor` reconhecendo um perfil de dublagem | **Sim** | `cli.py`'s check `dubbing` agora usa `models.piper_voice_path`/`models.separation_model_path` e falha nomeando qual dos dois está faltando |

**Conclusão:** a fase 1 do plano original (contratos, schema, CLI, plano de
falas editável) está genuinamente completa e testada, e agora os dois
primeiros pedaços de orquestração real (`detect_language`, `extract`)
também estão — construídos com engines falsas, sem nenhuma dependência
nova. Tudo que produz áudio — separação, síntese, sincronização, mixagem —
continua só assinatura de protocolo, zero implementação. A parede de
`not_implemented` (antes logo após `probe`) andou dois estágios: agora é
em `separate`, que é onde a decisão de backend da fase 2 (benchmark) deixa
de ser adiável — `separate` não dá pra escrever sem escolher (ou pelo menos
fingir) um `DialogueSeparator`.

A fase 2 (seção 4 abaixo) começou: a tarefa 1 (`models install` baixando
Piper + Demucs) está feita e testada, com download real validado nesta
máquina (rede autorizada explicitamente pelo usuário). A decisão de backend
de separação ficou registrada aqui mesmo: **Demucs (`htdemucs`)** como
baseline, conforme a spec original já admitia. As tarefas 2-3 estão feitas
(`DemucsSeparator`/`PiperSynthesizer` em `dubbing.py`, testados com modelos
falsos) e a 4 tem uma medição de 3 min em `docs/benchmark.md` (sem avaliação
auditiva humana ainda). Os engines ainda não estão ligados em
`run_dubbing_job`: `separate` continua `not_implemented`.

## 2. A dependência que bloqueia tudo: modelos

Antes de qualquer engine ser escrita, `nas-subs models install` precisa
aprender a instalar os pacotes de dublagem. Hoje ela só sabe baixar Whisper e
Argos. Isso é trabalho em `models.py`/`cli.py` (ownership "006. dubbing" já
cobre a metade de dublagem desses dois arquivos, por `AGENTS.md`), mas **é a
única etapa que toca rede** — por regra do projeto, só esse comando pode, e
só quando o usuário pedir explicitamente. Nenhuma fase abaixo pode ser
validada de ponta a ponta sem isso rodar primeiro.

Modelos que faltam instalar (da spec original, seção 3):

| Papel | Pacote | Observação de licença |
|---|---|---|
| Voz fixa (MVP) | Piper, voz `pt_BR-faber-medium` (já é o default em `DubbingConfig.voice`) | Piper é GPL-3.0 — registrar separadamente de pesos/vozes |
| Separação de diálogo | python-audio-separator (ou Demucs como baseline, repositório original arquivado) | Separação musical não é separação de efeitos cinematográficos — não prometer mais do que entrega |
| Clonagem (evolução, perfil `mac-clone`) | VoxCPM2 | Validar checkpoint específico antes de qualquer distribuição |

## 3. Divisão de responsabilidade (por que isso não é "um PR")

Tudo abaixo cabe dentro de `dubbing.py` e da metade de dublagem de
`models.py`/`cli.py` — a stage "006. dubbing" do `AGENTS.md`. Ela **não pode
tocar** `transcription.py`, `translation.py`, `output.py` (engines de
legenda) nem reescrever `pipeline.py`/`worker.py` além do que já existe (o
dispatch para `run_dubbing_job` já está feito). Isso significa: cada engine
nova implementa um dos Protocols já definidos em `domain.py` (seção 1) e é
injetada via `StageContext`-equivalente de dublagem — sem inventar um novo
mecanismo de orquestração.

## 4. Fases 2 a 6 — tarefas concretas

### Fase 2 — Benchmark no Mac (depende de §2)

**Objetivo:** provar que separação + síntese rodam no hardware alvo (M2 Pro,
CPU/MPS) antes de comprometer uma arquitetura de produção em cima disso.

Tarefas:
1. Estender `models.install_models`/`cli.py models install` para baixar
   Piper (voz `pt_BR-faber-medium`) e o backend de separação homologado.
2. Implementar uma primeira `DialogueSeparator` (Demucs como baseline,
   conforme a spec original admite) — só precisa satisfazer o Protocol:
   `separate(chunk, destination_dir) -> SeparatedAudio`.
3. Implementar uma primeira `SpeechSynthesizer` com Piper —
   `synthesize(segment, voice, destination) -> SynthesisArtifact`.
4. Rodar os dois contra um trecho de 2–5 min real (não um job completo) e
   registrar tempo, pico de memória e uma avaliação de qualidade humana em
   `docs/benchmark.md`.

Critério de aceite (da spec original, §10, mantido): números de tempo e
memória registrados, decisão tomada sobre o backend de separação antes de
seguir.

**Não começar a fase 3 sem essa decisão** — é a mesma regra de "não
implementar fases posteriores com uma anterior ainda incerta" que já vale
para o roadmap inteiro.

### Fase 3 — MVP com voz fixa

**Objetivo:** primeiro fluxo ponta a ponta: vídeo → áudio dublado em
`pt-BR`, voz fixa, em staging.

Tarefas:
1. Estender `run_dubbing_job` (`dubbing.py`) para encadear os estágios já
   nomeados no schema — `SEPARATE → TRANSCRIBE → MERGE → TRANSLATE → ADAPT
   → SYNTHESIZE → SYNC → MIX → VALIDATE_AUDIO → PUBLISH` — reaproveitando
   `transcription.py`/`translation.py` como **serviços chamados**, não como
   módulos editados (a fronteira de ownership do §3 continua valendo: ler,
   nunca escrever, nesses dois arquivos).
2. Implementar `TimelineRenderer.render` (posiciona os `SynthesisArtifact`
   na timeline absoluta do vídeo) e `AudioMixer.mix` (diálogo + fundo, sem
   duplicar o áudio original — regra explícita da spec original, §8).
3. Persistir `DubSegment`/`SynthesisArtifact` via os métodos que já existem
   em `repository.py` (`list_dub_segments`, `replace_dub_plan`) mais os que
   faltarem para artefatos de síntese (hoje só há a tabela
   `synthesis_artifacts`, sem métodos de leitura/escrita no repositório —
   outro gap concreto a fechar aqui, dentro da stage 2 por ownership).
4. Gate de qualidade usando `DubbingQualityReport` (já definido): cobertura
   completa, pico de áudio, falas sem corte.
5. Publicar só em `staging`/preview nesta fase — nunca sidecar automático
   (a spec original não lista publicação automática de dublagem no MVP).

Critério de aceite: `nas-subs dub process --preview-seconds 180` produz
`dialogue.pt-BR.wav`, `dubbed.pt-BR.m4a`, `dubbing-plan.json`,
`manifest.json`, `quality-report.json` em `output/<job_id>/` (layout já
descrito na spec original, §11) — e a suíte de legendas continua passando
sem alteração.

### Fase 4 — Recuperação e revisão

**Objetivo:** o que já existe para legendas (retomada por checkpoint,
reprocessamento preservando trabalho válido) precisa existir para dublagem
também.

Tarefas:
1. Checkpoint por chunk de separação, por fala sintetizada, por timeline —
   mesmo padrão de `transcription.py`'s `chunk_checkpoint_path` (escrever em
   arquivo temporário, `fsync`, renomear; reusar só quando job, fingerprint,
   schema e `stage_config_hash` baterem — já é a convenção documentada em
   `AGENTS.md`).
2. Regra de invalidação específica de dublagem: tradução mudou → ressintetizar
   só as falas afetadas; voz mudou → ressintetizar tudo; separador mudou →
   invalidar os artefatos derivados do áudio separado (regras já escritas na
   spec original, §7 — ainda não implementadas).
3. `nas-subs dub plan apply` já existe e já invalida por revisão (fase 1) —
   essa fase conecta a regeneração seletiva (ressintetizar só o que o plano
   editado mudou) ao resultado do `apply_plan`.

Critério de aceite: interromper um job de dublagem no meio e retomá-lo não
refaz checkpoints válidos (mesmo teste de regressão que legendas já têm, só
que para os novos artefatos).

### Fase 5 — Clonagem e múltiplos locutores

**Objetivo:** evolução condicionada ao benchmark da fase 2 — perfil
`mac-clone`, já modelado em `DubbingProfile`/`VoiceKind.CLONE` no domínio,
mas sem engine nenhuma por trás ainda.

Tarefas: diarização (introduzir pyannote.audio só aqui, como a spec original
já isola), múltiplas `VoiceAssignment` por job, consistência vocal entre
falas do mesmo locutor. Porta de decisão explícita da spec original: se a
clonagem for pesada/instável no M2 Pro, a voz fixa continua sendo o perfil
estável — não é obrigatório chegar até aqui.

### Fase 6 — Mídia longa

**Objetivo:** episódio/filme completo, não só preview de 2–5 min. Antes
disso fazer sentido, as fases 2–4 precisam estar sólidas — processar uma
hora de áudio com uma pipeline não testada em escala é como o risco "tempo
excessivo" da spec original (§13) se materializa.

## 5. O que eu NÃO vou fazer sem você pedir de novo

- Rodar `nas-subs models install` para baixar Piper/separação — toca rede,
  só com pedido explícito seu, cada vez.
- Escrever as implementações de `DialogueSeparator`/`SpeechSynthesizer`
  antes da fase 2 (benchmark) decidir qual backend de separação usar — a
  spec original e a regra 3 do `AGENTS.md` ("Evitar complexidade") pedem
  essa ordem.
- Prometer prazo: a spec original já é explícita que isso é planejamento,
  não medição (`docs/roadmap/006-dubbing-pt-br.md`, linha final).

## 6. Próximo passo sugerido

Fase 2 é o próximo item executável sem ambiguidade: estender
`models install` para Piper + um separador, rodar um benchmark real de 2–5
min no seu Mac, e registrar os números. Isso não produz dublagem completa
ainda, mas é o primeiro passo que **toca rede** — por isso preciso da sua
confirmação explícita antes de rodar `nas-subs models install` com os
pacotes novos.

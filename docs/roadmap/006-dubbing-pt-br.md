# Roadmap final — Dublagem pt-BR no CLI `subtitles`

**Status:** PLANNED

## 1. Objetivo e escopo

Adicionar geração de áudio dublado em português brasileiro ao serviço `subtitles`, usando o CLI existente `nas-subs` e processamento local.

**Ambiente inicial:** MacBook Pro M2 Pro, CPU de 10 núcleos, GPU integrada de 16 núcleos, 16 GB de memória unificada e aproximadamente 127 GiB livres no momento da inspeção.

**Diretrizes:**

- Aproveitar transcrição, tradução, jobs e recuperação existentes.
- Não exigir compra de GPU.
- Executar nativamente no macOS para avaliar aceleração Metal/MPS.
- Manter um worker e carregar modelos pesados sequencialmente.
- Inferência offline após instalação explícita dos modelos.
- Preservar mídia original e nunca sobrescrever outputs.
- Operação pelo CLI, sem nova API ou interface web.
- Sem integrações com Jellyfin, Sonarr ou Radarr.

**MVP:** áudio dublado com voz fixa pt-BR, sincronização, fundo quando a separação for aprovada, revisão e checkpoints.

**Evolução:** múltiplos locutores e clonagem de voz, condicionados aos resultados no M2 Pro.

## 2. Base existente e pontos de atenção

A inspeção do repositório identificou:

| Componente | Implementação | Decisão |
|---|---|---|
| CLI | Typer, `nas-subs` | Estender |
| ASR | faster-whisper | Reutilizar |
| Tradução | Argos Translate local | Reutilizar como baseline |
| Mídia | FFmpeg/ffprobe | Reutilizar inspeção e extração |
| Persistência | SQLite | Estender por migração |
| Execução | Worker único, lock e leases | Manter |
| Recuperação | Retry e checkpoints de transcrição | Estender para dublagem |
| Qualidade | Revisão e aprovação | Adicionar validação de áudio |
| Modelos | Instalação e verificação offline | Ampliar |
| Publicação | Staging e proteção contra sobrescrita | Reutilizar |

Referências: [README](https://github.com/M-Guimaraes/subtitles/blob/main/README.md), [pipeline](https://github.com/M-Guimaraes/subtitles/blob/main/src/nas_subtitles/pipeline.py), [CLI](https://github.com/M-Guimaraes/subtitles/blob/main/src/nas_subtitles/cli.py).

Três ajustes são obrigatórios:

1. **Legenda existente não impede dublagem.** Separar essa regra em descoberta e execução.
2. **Jobs de legenda e dublagem têm identidades distintas.** Adicionar `job_kind` à deduplicação.
3. **Retomada por etapa precisa ser implementada.** No código inspecionado, `run_job` descarta `start_stage`/`stop_after`; a presença desses argumentos não significa que a funcionalidade esteja pronta.

A tradução atual produz português, sem garantir vocabulário brasileiro. A avaliação pt-BR e a revisão fazem parte do aceite.

## 3. Ferramentas selecionadas para avaliação

| Etapa | Ferramenta | Prioridade |
|---|---|---|
| Inspeção, extração e mixagem | [FFmpeg/ffprobe](https://ffmpeg.org/ffmpeg.html) | Base |
| Transcrição | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Base existente, CPU INT8 |
| Tradução | [Argos Translate](https://github.com/argosopentech/argos-translate) | Base existente |
| Separação | [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | Benchmark principal |
| Voz fixa | [Piper](https://github.com/OHF-Voice/piper1-gpl) | MVP CPU |
| Clonagem | [VoxCPM2](https://github.com/OpenBMB/VoxCPM) | Experimento no M2 Pro |
| Alinhamento | [WhisperX](https://github.com/m-bain/whisperX) | Introduzir se necessário |
| Diarização | [pyannote.audio](https://github.com/pyannote/pyannote-audio) | Evolução |
| ASR alternativo | [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Comparação com aceleração Apple |
| Tradução contextual | [Qwen3](https://huggingface.co/Qwen/Qwen3-8B) e [llama.cpp](https://github.com/ggml-org/llama.cpp) | Evolução opcional |

**Licenças:** registrar código, pesos e vozes separadamente. Piper atual é GPL-3.0; VoxCPM2 declara código/pesos Apache-2.0. Validar os checkpoints específicos antes de distribuir.

Demucs permanece como baseline de separação, considerando que seu repositório original está arquivado. Separação musical não garante preservação de efeitos cinematográficos. [Demucs](https://github.com/facebookresearch/demucs).

O [Video Dubbing Translator](https://github.com/kadirb4rut/video-dubbing-translator) serve como referência técnica; sua tradução externa não atende ao requisito offline.

## 4. Arquitetura e fluxo

```text
nas-subs dub process / enqueue
              │
              ▼
       job SQLite: dubbing
              │
    inspeção e seleção de faixa
              │
          extração
              │
   separação de diálogo e fundo
              │
     transcrição e alinhamento
              │
      tradução e adaptação
              │
        plano de falas
              │
      síntese por segmento
              │
        sincronização
              │
           mixagem
              │
     validação e revisão
              │
      output_dir protegido
```

O pipeline de legendas continua independente. Dublagem reutiliza seus serviços, sem depender dos limites de linhas e caracteres da renderização SRT.

Novos contratos: `DialogueSeparator`, `SpeechSynthesizer`, `TimelineRenderer` e `AudioMixer`.

## 5. CLI proposto

Os comandos abaixo representam a interface planejada.

```bash
# Prévia
nas-subs dub process /media/filme.mkv \
  --preview-seconds 180 \
  --source-language en \
  --profile cpu-fixed \
  --config /config/config.yaml

# Execução completa
nas-subs dub process /media/filme.mkv \
  --audio-stream-index 1 \
  --profile cpu-fixed \
  --config /config/config.yaml

# Enfileiramento no worker existente
nas-subs dub enqueue /media/filme.mkv \
  --profile mac-clone \
  --config /config/config.yaml

# Acompanhamento e recuperação
nas-subs jobs show JOB_ID --json
nas-subs jobs cancel JOB_ID
nas-subs jobs retry JOB_ID

# Revisão por arquivo
nas-subs dub plan export JOB_ID --output /output/plan.json
nas-subs dub plan apply JOB_ID --input /output/plan-edited.json

# Aprovação e publicação
nas-subs jobs approve JOB_ID
nas-subs publish JOB_ID
```

Estender `models install`, `models verify` e `doctor` para os perfis de dublagem. Downloads continuam exclusivos de `models install`.

Todos os comandos aceitam `--json`, não solicitam respostas interativas e seguem os códigos de saída existentes.

O scanner continua gerando apenas jobs de legendas. Dublagem automática de diretórios fica fora do MVP.

## 6. Dados e persistência

Adicionar:

| Contrato | Conteúdo |
|---|---|
| `JobKind` | `subtitles` ou `dubbing` |
| `DubbingConfig` | Perfil, modelos, saída e limites |
| `DubSegment` | ID, janela absoluta, original, tradução, adaptação, locutor e revisão |
| `VoiceAssignment` | Voz fixa ou referência por locutor |
| `SynthesisArtifact` | Segmento, duração, modelo, seed e checksum |
| `DubbingQualityReport` | Métricas, flags e necessidade de revisão |

Migração SQLite:

- Jobs antigos recebem `job_kind=subtitles`.
- Deduplicação inclui tipo de job e configuração efetiva.
- Artefatos passam a identificar segmento e revisão.
- Falas e referências têm persistência consultável.
- Preservar IDs, estados e outputs antigos.
- Validar índices e migrações posteriores antes de definir a nova migração.

Reutilizar estados existentes sempre que suficientes: `QUEUED`, `RUNNING`, `NEEDS_REVIEW`, `READY_TO_PUBLISH`, `COMPLETED`, `FAILED` e `CANCELLED`.

## 7. Checkpoints e revisão

Novas etapas: `SEPARATE`, `ADAPT`, `SYNTHESIZE`, `SYNC`, `MIX` e `VALIDATE_AUDIO`.

Definir sequência por tipo de job, preservando a sequência atual de legendas.

Checkpoints:

- Separação por chunk.
- Plano de falas por revisão.
- Síntese por segmento.
- Timeline e mixagem por revisão.

A reutilização exige correspondência de entrada, configuração, modelo, esquema e checksums das dependências.

Escrita: temporário → validação → confirmação do artefato → registro no banco.

**Invalidação:**

- Tradução alterada: ressintetizar a fala e refazer outputs dependentes.
- Voz alterada: ressintetizar todas as falas afetadas.
- Separador alterado: invalidar os dependentes do áudio separado.
- Checkpoint corrompido: refazer apenas a unidade necessária.

`plan apply` valida IDs, versão e base da revisão. Outputs aprovados não mudam silenciosamente.

## 8. Regras de áudio

- Selecionar explicitamente a faixa original e conservar offsets.
- Manter timestamps absolutos na timeline do vídeo.
- Preservar áudio adequado à mixagem; gerar mono apenas para ASR.
- Separar em chunks com contexto e validar bordas.
- Preferir fundo sem diálogo fornecido pelo usuário, quando disponível.
- Não considerar extração do canal central como separação completa.
- Traduzir com contexto e registrar adaptação separadamente.
- Ajustar texto e ressintetizar antes de acelerar áudio.
- Limite inicial de velocidade: `0,90×–1,15×`, sujeito ao benchmark.
- Não cortar palavras para encaixar.
- Sinalizar sobreposição não resolvida.
- Não misturar indiscriminadamente a dublagem com o original, pois duplicaria as falas.

Fundo com vazamento significativo de diálogo ou perda de efeitos exige revisão.

## 9. Perfis para o M2 Pro

### `cpu-fixed` — primeira entrega

- faster-whisper INT8.
- Argos Translate.
- Piper com voz pt-BR validada.
- Separação conforme backend homologado.
- Worker único e processamento em chunks.

### `mac-clone` — experimental

- Transcrição existente inicialmente.
- Separação acelerada quando compatível.
- VoxCPM2 com backend MPS/Metal validado.
- Síntese por fala e modelos carregados sequencialmente.

Os 16 GB são compartilhados com o sistema. Definir limites de buffers, liberar modelos entre etapas e medir pressão de memória/swap.

Não assumir aceleração automática do faster-whisper por Metal. whisper.cpp é uma alternativa a comparar.

A execução nativa é prioritária para aceleração Apple. O perfil Docker CPU continua separado e não deve receber promessa de acesso ao Metal.

## 10. Fases e critérios de aceite

| Fase | Dependência | Entrega e aceite |
|---|---|---|
| **1 — Contratos e migração** | Inspeção concluída | CLI, tipo de job e persistência; regressões de legendas passam |
| **2 — Benchmark no Mac** | Modelos instalados | Trechos de 2–5 minutos; qualidade, tempo e memória registrados |
| **3 — MVP com voz fixa** | Perfil escolhido | Diálogo, sincronização, mixagem e outputs em staging |
| **4 — Recuperação e revisão** | MVP | Cancelamento, retry, plano editável e regeneração seletiva |
| **5 — Clonagem e locutores** | Benchmark favorável | Referências, diarização e consistência vocal avaliadas |
| **6 — Mídia longa** | Piloto aprovado | Episódio e filme completos, retenção e documentação |

**Portas de decisão:**

- Separação reprovada: revisar estratégia antes de habilitar mixagem automática.
- Clonagem pesada ou instável: manter voz fixa como perfil estável.
- Pt-BR inadequado: melhorar adaptação/revisão antes de declarar suporte aprovado.
- Tempo excessivo: medir alternativa de backend antes de considerar novo hardware.

## 11. Outputs e publicação

```text
output/<job_id>/
  dialogue.pt-BR.wav
  dubbed.pt-BR.m4a
  dubbing-plan.json
  manifest.json
  quality-report.json
```

Legenda correspondente pode ser incluída como output opcional.

Publicar somente artefatos validados, seguindo a proteção existente contra sobrescrita. Conflito gera `output_conflict`; não substituir arquivos.

Prévias são explicitamente identificadas e não satisfazem jobs completos.

O MVP entrega áudio separado. Exportação de vídeo adicional fica para uma decisão posterior sobre as regras do projeto.

## 12. Testes e metas iniciais

| Área | Meta |
|---|---|
| Offline | Processamento sem rede; modelo ausente gera `model_missing` |
| Preservação | Original inalterado e outputs sem sobrescrita |
| Cobertura | Todas as falas geradas ou sinalizadas |
| Tradução | Sem alterações críticas de sentido no corpus revisado |
| Sincronização | Meta inicial: ≥95% dos inícios avaliados dentro de ±200 ms da janela aprovada |
| Integridade | Nenhuma palavra truncada |
| Mixagem | Sem clipping; true peak proposto ≤−1 dBTP |
| Qualidade | Avaliação humana de inteligibilidade, pt-BR e fundo |
| Recuperação | Interrupção não refaz checkpoints válidos |
| Compatibilidade | Comandos e jobs de legendas preservados |

Calibrar metas perceptivas após o benchmark.

Testar engines falsos, migrações, invalidação, arquivos com espaços/Unicode, offsets, silêncio, sobreposição, disco cheio, corrupção e interrupção.

Mídia de teste sintética ou com licença registrada. Testes reais com marcador `models`.

Executar Ruff, mypy e suíte sem modelos; smoke tests com modelos e validações de Compose quando aplicáveis.

## 13. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Memória insuficiente | Modelos sequenciais, chunks e buffers limitados |
| Separação perde efeitos | Corpus audiovisual e revisão |
| Voz ou sotaque inadequados | Avaliação pt-BR e perfil fixo |
| Tradução longa | Adaptação e ressíntese |
| Diarização inconsistente | Correção no plano e fallback explícito |
| Dependências incompatíveis | Resolver com `uv`, atualizar lock e testar imports |
| Interrupções longas | Checkpoints por fala/chunk |
| Disco temporário excessivo | Estimativa prévia, retenção e cleanup restrito |

## 14. Definição de pronto

A primeira versão está pronta quando o CLI processa um arquivo com voz fixa pt-BR, entrega áudio validado, permite revisão e retomada, preserva o original e mantém a geração de legendas funcionando.

**Primeiro marco:** `nas-subs dub process --preview-seconds 180` executado no M2 Pro, com áudio, plano de falas e relatório de tempo/memória.

**GPU dedicada não é requisito. Clonagem é uma evolução medida no hardware disponível.**

Este roadmap é planejamento: a implementação e sua homologação ainda precisam ser realizadas.

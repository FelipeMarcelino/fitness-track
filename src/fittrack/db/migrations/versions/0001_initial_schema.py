"""Initial schema.

Transcribed from §5.2 of doc/spec.md, which is the source of truth. The SQL is
kept verbatim rather than translated into the SQLAlchemy DSL so a reviewer can
diff this file against the spec directly; a translation is one more place for
the two to drift apart.
"""

from __future__ import annotations

import re

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = r"""
-- ============================================================
-- IDENTIDADE E TENANCY
-- ============================================================

-- Extensões exigidas pelo schema. Devem vir na primeira migração, antes de
-- qualquer índice trigram — `gin_trgm_ops` não existe sem pg_trgm.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;   -- normalização de alias (§10)

CREATE TYPE plan_tier   AS ENUM ('free', 'pro', 'trial');
CREATE TYPE tenant_state AS ENUM ('onboarding', 'active', 'suspended', 'deleted');

CREATE TABLE tenant (
    id              BIGSERIAL PRIMARY KEY,
    -- BSUID: identificador opaco do usuário no escopo da empresa, entregue
    -- pela Meta no webhook. NÃO é telefone, não é parseável, e não deve ser
    -- exibido ao usuário. É o valor devolvido no campo `to` do envio (§18.4).
    bsuid           TEXT NOT NULL,
    display_name    TEXT,
    locale          TEXT NOT NULL DEFAULT 'pt-BR',
    timezone        TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    state           tenant_state NOT NULL DEFAULT 'onboarding',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
-- Unicidade apenas entre tenants ativos. UNIQUE na coluna impediria alguém de
-- se recadastrar com o mesmo número após exclusão (LGPD, §19.5).
CREATE UNIQUE INDEX ux_tenant_bsuid_active
    ON tenant(bsuid) WHERE deleted_at IS NULL;

-- Consentimentos LGPD granulares. Registro de treino e dado de saúde são separados.
CREATE TYPE consent_kind AS ENUM (
    'terms',            -- termos de uso e política de privacidade
    'workout_data',     -- registro de treino (dado pessoal comum)
    'health_data',      -- métricas corporais, dor, lesão (art. 11 LGPD — sensível)
    'proactive_msg',    -- receber mensagens iniciadas pelo bot
    'model_training'    -- contribuir dados anonimizados para melhoria
);

CREATE TABLE consent (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    kind        consent_kind NOT NULL,
    granted     BOOLEAN NOT NULL,
    text_hash   TEXT NOT NULL,        -- sha256 do texto exato apresentado
    version     TEXT NOT NULL,        -- versão da política, ex: 'privacy-2026-08'
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX ix_consent_tenant_kind ON consent(tenant_id, kind, granted_at DESC);

CREATE TABLE athlete_profile (
    tenant_id           BIGINT PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    goal                TEXT,          -- hipertrofia | forca | emagrecimento | saude | performance
    experience_level    TEXT,          -- iniciante | intermediario | avancado
    training_days_week  SMALLINT,
    session_minutes     SMALLINT,
    equipment_access    TEXT[],        -- ['academia_completa','halteres','peso_corporal']
    injuries            BYTEA,        -- CIFRADA (§22.2); JSON serializado antes de cifrar
    injuries_key_version SMALLINT NOT NULL DEFAULT 1,
    preferences         JSONB DEFAULT '{}'::jsonb,   -- {"disliked_exercises":[...], "verbosity":"short"}
    persona_style       TEXT DEFAULT 'parceiro',     -- parceiro | tecnico | motivacional
    onboarded_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    tier                plan_tier NOT NULL DEFAULT 'free',
    provider            TEXT,          -- 'mercadopago'
    provider_sub_id     TEXT,
    status              TEXT NOT NULL, -- active | pending | past_due | cancelled
    current_period_end  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_subscription_active
    ON subscription(tenant_id) WHERE status = 'active';

-- ============================================================
-- CATÁLOGO DE EXERCÍCIOS
-- ============================================================

CREATE TYPE movement_pattern AS ENUM (
    'empurrar_horizontal','empurrar_vertical','puxar_horizontal','puxar_vertical',
    'agachamento','dobradica_quadril','avanco','core','isolado','locomocao','outro'
);

CREATE TABLE exercise (
    id                  BIGSERIAL PRIMARY KEY,
    slug                TEXT NOT NULL,               -- supino_reto_barra
    name                TEXT NOT NULL,               -- Supino reto com barra
    tenant_id           BIGINT REFERENCES tenant(id) ON DELETE CASCADE,  -- NULL = global
    modality            TEXT NOT NULL,               -- forca | cardio | calistenia | mobilidade
    primary_muscles     TEXT[] NOT NULL DEFAULT '{}',
    secondary_muscles   TEXT[] NOT NULL DEFAULT '{}',
    equipment           TEXT,                        -- barra | halter | maquina | cabo | peso_corporal
    pattern             movement_pattern,
    unilateral          BOOLEAN NOT NULL DEFAULT false,
    default_set_type    TEXT NOT NULL DEFAULT 'strength',
    execution_notes     TEXT,
    substitutes         BIGINT[] DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'active',  -- active | pending_review | merged
    merged_into         BIGINT REFERENCES exercise(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_exercise_slug_global
    ON exercise(slug) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX ux_exercise_slug_tenant
    ON exercise(tenant_id, slug) WHERE tenant_id IS NOT NULL;
CREATE INDEX ix_exercise_name_trgm ON exercise USING gin (name gin_trgm_ops);

CREATE TABLE exercise_alias (
    id          BIGSERIAL PRIMARY KEY,
    exercise_id BIGINT NOT NULL REFERENCES exercise(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    normalized  TEXT NOT NULL,      -- lowercase, sem acento, sem stopwords
    tenant_id   BIGINT REFERENCES tenant(id) ON DELETE CASCADE,  -- alias privado
    source      TEXT NOT NULL DEFAULT 'curated',  -- curated | learned | user
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_alias_normalized ON exercise_alias(normalized);
CREATE INDEX ix_alias_norm_trgm  ON exercise_alias USING gin (normalized gin_trgm_ops);

-- ============================================================
-- SESSÕES E SÉRIES
-- ============================================================

CREATE TYPE session_status AS ENUM ('open', 'closed_auto', 'closed_explicit', 'discarded');

CREATE TABLE workout_session (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    status          session_status NOT NULL DEFAULT 'open',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    local_date      DATE NOT NULL,     -- data no fuso do tenant (chave de agregação diária)
    label           TEXT,              -- "Peito e tríceps" (inferido no fechamento)
    location        TEXT,              -- academia | rua | casa
    notes           TEXT,
    plan_item_id    BIGINT,            -- se seguiu uma ficha
    CONSTRAINT ck_session_dates CHECK (ended_at IS NULL OR ended_at >= started_at)
);
-- No máximo uma sessão aberta por tenant
CREATE UNIQUE INDEX ux_session_one_open
    ON workout_session(tenant_id) WHERE status = 'open';
CREATE INDEX ix_session_tenant_date ON workout_session(tenant_id, local_date DESC);
CREATE INDEX ix_session_open_activity
    ON workout_session(last_activity_at) WHERE status = 'open';

CREATE TYPE set_type   AS ENUM ('strength', 'cardio', 'isometric', 'interval');
CREATE TYPE set_status AS ENUM ('complete', 'incomplete');

CREATE TABLE exercise_set (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    session_id      BIGINT NOT NULL REFERENCES workout_session(id) ON DELETE CASCADE,
    exercise_id     BIGINT NOT NULL REFERENCES exercise(id),
    set_type        set_type NOT NULL DEFAULT 'strength',
    set_index       SMALLINT NOT NULL,          -- 1..N dentro do exercício na sessão
    exercise_order  SMALLINT,                   -- ordem do exercício na sessão

    -- musculação
    load_kg         NUMERIC(6,2),
    reps            SMALLINT,
    rpe             NUMERIC(3,1),               -- 0..10, um decimal
    rir             SMALLINT,                   -- reps in reserve, se informado
    side            TEXT,                       -- left | right | both

    -- cardio
    distance_m      NUMERIC(10,2),
    duration_s      INTEGER,
    elevation_m     NUMERIC(7,2),
    avg_hr          SMALLINT,

    -- isometria / intervalado
    hold_s          INTEGER,
    rounds          SMALLINT,

    -- comum
    rest_s          INTEGER,
    tempo           TEXT,                       -- "3-1-1-0"
    is_warmup       BOOLEAN NOT NULL DEFAULT false,
    is_failure      BOOLEAN NOT NULL DEFAULT false,
    technique       TEXT,                       -- dropset | restpause | cluster | normal

    -- proveniência e auditoria
    status          set_status NOT NULL DEFAULT 'complete',
    -- Copiado de exercise.equipment na gravação. Denormalizado porque um
    -- CHECK não pode consultar outra tabela, e a regra da §9.7 depende dele.
    is_bodyweight   BOOLEAN NOT NULL DEFAULT false,
    inferred        BOOLEAN NOT NULL DEFAULT false,  -- expandido de "3x10", não dito série a série
    confidence      NUMERIC(3,2) NOT NULL DEFAULT 1.00,
    low_confidence  BOOLEAN GENERATED ALWAYS AS (confidence < 0.75) STORED,
    source_text     TEXT,                       -- trecho original que gerou esta linha
    source_message_id TEXT,
    corrected_from  BIGINT REFERENCES exercise_set(id),
    deleted_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- O CHECK só vale para linhas COMPLETAS. Série cujo esclarecimento expirou
    -- (§8.6) entra como 'incomplete' e fica de fora das análises — o dado do
    -- usuário nunca é descartado, mas também nunca contamina cálculo.
    CONSTRAINT ck_set_payload CHECK (
        status = 'incomplete'
     OR (set_type = 'strength'  AND reps IS NOT NULL
                                AND (is_bodyweight OR load_kg IS NOT NULL))
     OR (set_type = 'cardio'    AND duration_s IS NOT NULL)   -- distância é opcional (§9.7)
     OR (set_type = 'isometric' AND hold_s IS NOT NULL)
     OR (set_type = 'interval'  AND rounds IS NOT NULL)
    ),
    CONSTRAINT ck_rpe_range CHECK (rpe IS NULL OR (rpe >= 0 AND rpe <= 10))
);
CREATE INDEX ix_set_tenant_created ON exercise_set(tenant_id, created_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_set_session ON exercise_set(session_id) WHERE deleted_at IS NULL;
CREATE INDEX ix_set_tenant_exercise ON exercise_set(tenant_id, exercise_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- Idempotência de reprocessamento (§17.4). NULLS NOT DISTINCT (PG15+) é
-- obrigatório: sem ele, séries com source_message_id nulo escapariam da
-- unicidade e o retry de um batch duplicaria o volume do treino.
-- Fila de revisão: séries que ficaram incompletas por timeout de esclarecimento
CREATE INDEX ix_set_incomplete ON exercise_set(tenant_id, created_at DESC)
    WHERE status = 'incomplete' AND deleted_at IS NULL;

CREATE UNIQUE INDEX ux_set_idempotency
    ON exercise_set (session_id, exercise_id, set_index, source_message_id)
    NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;

-- Volume por série, materializado para as queries analíticas
CREATE VIEW v_set_volume AS
SELECT s.*,
       (s.load_kg * s.reps) AS volume_kg,
       -- 1RM estimado (Epley); apenas para reps entre 1 e 12
       CASE WHEN s.reps BETWEEN 1 AND 12 AND s.load_kg > 0
            THEN s.load_kg * (1 + s.reps::numeric / 30) END AS e1rm_epley
FROM exercise_set s
WHERE s.deleted_at IS NULL
  AND s.is_warmup = false
  AND s.status = 'complete';   -- incompletas nunca entram em cálculo

CREATE TABLE session_summary (
    session_id      BIGINT PRIMARY KEY REFERENCES workout_session(id) ON DELETE CASCADE,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    narrative       BYTEA NOT NULL,     -- CIFRADA (§22.2); resumo indexado no RAG
    key_version     SMALLINT NOT NULL DEFAULT 1,
    total_volume_kg NUMERIC(10,2),
    total_sets      SMALLINT,
    duration_min    SMALLINT,
    muscle_groups   TEXT[],
    prs             JSONB DEFAULT '[]'::jsonb,
    avg_rpe         NUMERIC(3,1),
    indexed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- SAÚDE E MÉTRICAS CORPORAIS (dado sensível — consentimento próprio)
-- ============================================================

CREATE TABLE body_metric (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    measured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    local_date   DATE NOT NULL,
    kind         TEXT NOT NULL,   -- peso | cintura | braco | sono_h | disposicao | dor
    value        BYTEA NOT NULL,  -- CIFRADA (§22.2); não agregável em SQL
    key_version  SMALLINT NOT NULL DEFAULT 1,
    unit         TEXT NOT NULL,   -- kg | cm | h | escala_0_10
    note         TEXT,
    source_text  TEXT
);
CREATE INDEX ix_body_metric ON body_metric(tenant_id, kind, local_date DESC);

CREATE TABLE health_report (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    reported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    region       TEXT,            -- ombro_direito | lombar | joelho_esquerdo
    severity     TEXT,            -- leve | moderada | intensa
    category     TEXT NOT NULL,   -- dor | lesao | tontura | mal_estar | outro
    verbatim     BYTEA NOT NULL,  -- CIFRADA (§22.2)
    key_version  SMALLINT NOT NULL DEFAULT 1,
    guidance_given TEXT,
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX ix_health_active ON health_report(tenant_id) WHERE resolved_at IS NULL;

-- ============================================================
-- FICHAS DE TREINO
-- ============================================================

-- Um PROGRAMA é o horizonte longo (4 a 16 semanas): template base, fases de
-- periodização e metas. Uma FICHA (`workout_plan`) é a instância semanal que
-- o programa gera. Ver §9.6.
CREATE TYPE program_status AS ENUM ('draft', 'active', 'completed', 'abandoned');

CREATE TABLE training_program (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    goal            TEXT NOT NULL,          -- hipertrofia | forca | emagrecimento | performance
    base_template   TEXT,                   -- ppl | upper_lower | full_body | 5x5 | custom
    template_source TEXT,                   -- id do chunk no RAG que embasou a escolha
    horizon_weeks   SMALLINT NOT NULL,
    rationale       TEXT NOT NULL,          -- por que este programa para este usuário
    status          program_status NOT NULL DEFAULT 'draft',
    started_at      TIMESTAMPTZ,
    ends_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_program_horizon CHECK (horizon_weeks BETWEEN 4 AND 16),
    -- Chave alternativa: permite FK composta nas filhas, garantindo que
    -- referência e referenciado pertençam ao MESMO tenant.
    UNIQUE (id, tenant_id)
);
-- No máximo um programa ativo por tenant
CREATE UNIQUE INDEX ux_program_one_active
    ON training_program(tenant_id) WHERE status = 'active';

-- tenant_id é OBRIGATÓRIO nas filhas. A RLS do PostgreSQL é por tabela e NÃO
-- se propaga por chave estrangeira: sem esta coluna, uma query direta em
-- program_phase leria as fases de todos os tenants.
CREATE TABLE program_phase (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    program_id        BIGINT NOT NULL,
    phase_order       SMALLINT NOT NULL,
    name              TEXT NOT NULL,        -- base | acumulacao | intensificacao | deload | teste
    weeks             SMALLINT NOT NULL,
    weekly_sets_min   SMALLINT,             -- volume alvo por grupo muscular
    weekly_sets_max   SMALLINT,
    rpe_min           NUMERIC(3,1),
    rpe_max           NUMERIC(3,1),
    intensity_note    TEXT,
    is_deload         BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (program_id, phase_order),
    UNIQUE (id, program_id),          -- alvo da FK composta em workout_plan
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE program_milestone (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    program_id    BIGINT NOT NULL,
    description   TEXT NOT NULL,            -- "supino reto 100kg x 1"
    metric        TEXT NOT NULL,            -- e1rm | load | volume | distance | duration
    exercise_id   BIGINT REFERENCES exercise(id),
    target_value  NUMERIC(10,2) NOT NULL,
    target_date   DATE,
    achieved_at   TIMESTAMPTZ,
    achieved_value NUMERIC(10,2),
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX ix_milestone_open ON program_milestone(program_id) WHERE achieved_at IS NULL;

CREATE TABLE workout_plan (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT REFERENCES tenant(id) ON DELETE CASCADE,  -- NULL = template global
    program_id   BIGINT,
    phase_id     BIGINT,
    week_number  SMALLINT,        -- semana do programa que esta ficha materializa
    name         TEXT NOT NULL,
    goal         TEXT,
    level        TEXT,
    days_week    SMALLINT,
    split_type   TEXT,            -- ppl | upper_lower | full_body | abcd
    rationale    TEXT,            -- por que foi recomendada (gerado por LLM)
    source       TEXT NOT NULL DEFAULT 'generated',  -- generated | template | user | program
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FKs compostas: a ficha só referencia programa do PRÓPRIO tenant, e
    -- a fase tem de pertencer AO MESMO programa. Com FKs independentes,
    -- apagar o programa de um tenant apagaria ficha de outro por CASCADE.
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (phase_id, program_id)
        REFERENCES program_phase(id, program_id),
    CONSTRAINT ck_plan_phase_needs_program
        CHECK (phase_id IS NULL OR program_id IS NOT NULL),
    UNIQUE (id, tenant_id)            -- alvo da FK composta em plan_item
);

-- tenant_id obrigatório pelo mesmo motivo de program_phase: RLS é por tabela
-- e não se propaga por FK. Sem ele, uma query direta em plan_item leria os
-- itens de ficha de todos os tenants.
CREATE TABLE plan_item (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    plan_id      BIGINT NOT NULL,
    day_label    TEXT NOT NULL,   -- "A" | "Push" | "Segunda"
    day_order    SMALLINT NOT NULL,
    item_order   SMALLINT NOT NULL,
    exercise_id  BIGINT NOT NULL REFERENCES exercise(id),
    target_sets  SMALLINT,
    target_reps_min SMALLINT,
    target_reps_max SMALLINT,
    target_rpe   NUMERIC(3,1),
    rest_s       INTEGER,
    note         TEXT,
    FOREIGN KEY (plan_id, tenant_id)
        REFERENCES workout_plan(id, tenant_id) ON DELETE CASCADE
);

-- ============================================================
-- MENSAGENS, CUSTO E OPERAÇÃO
-- ============================================================

-- CASCADE, não SET NULL: o payload traz texto do usuário e transcrições de
-- áudio. Com SET NULL a linha sobreviveria à exclusão da conta, sem o
-- tenant_id necessário para localizá-la — violando a erasure da §19.5.
-- Consequência: no primeiro contato o ingress faz UPSERT do tenant (state=
-- 'onboarding') ANTES de inserir raw_message. Não existe mensagem órfã.
CREATE TABLE raw_message (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    wa_message_id TEXT NOT NULL UNIQUE,
    direction     TEXT NOT NULL,       -- inbound | outbound
    msg_type      TEXT NOT NULL,       -- text | audio | image | interactive | reaction | template
    payload       BYTEA NOT NULL,      -- CIFRADA (§22.2); JSON serializado antes de cifrar
    transcript    BYTEA,               -- CIFRADA (§22.2); preenchida se áudio
    key_version   SMALLINT NOT NULL DEFAULT 1,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);
CREATE INDEX ix_raw_tenant_time ON raw_message(tenant_id, received_at DESC);

CREATE TABLE processing_batch (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    message_ids   TEXT[] NOT NULL,
    combined_text TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts      SMALLINT NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE TABLE usage_ledger (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent          TEXT NOT NULL,
    provider       TEXT NOT NULL,      -- xai | anthropic | groq | openai
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    audio_seconds  NUMERIC(8,2),
    cost_usd       NUMERIC(10,6) NOT NULL DEFAULT 0,
    trace_id       TEXT,
    was_fallback   BOOLEAN NOT NULL DEFAULT false
);
-- date_trunc('month', timestamptz) é STABLE, não IMMUTABLE (depende do
-- TimeZone da sessão), e expressão de índice exige IMMUTABLE — o CREATE INDEX
-- falharia. Índice de range resolve as mesmas queries de quota mensal.
CREATE INDEX ix_usage_tenant_time ON usage_ledger(tenant_id, occurred_at DESC);

CREATE TABLE outbound_queue (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,      -- text | reaction | interactive | template | media
    payload       JSONB NOT NULL,

    -- Split em bolhas (§13.6): as bolhas de uma mesma resposta compartilham
    -- group_id e são enviadas em ordem de seq. Sem isso, um restart do worker
    -- não saberia quais já saíram, e o retry reenviaria o prefixo ou perderia
    -- o sufixo.
    group_id      UUID NOT NULL,
    seq           SMALLINT NOT NULL DEFAULT 0,

    -- scheduled_at = quando PODE sair pela primeira vez (agendamento).
    -- next_retry_at = quando pode ser TENTADA de novo após falha (backoff).
    -- Elegível para envio quando ambas já passaram.
    scheduled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    attempts      SMALLINT NOT NULL DEFAULT 0,
    error_code    TEXT,               -- código da Cloud API na última falha
    retryable     BOOLEAN,            -- classificação do erro (§18.5)
    last_error    TEXT,
    dead_at       TIMESTAMPTZ,        -- desistiu; não tenta mais

    UNIQUE (group_id, seq)
);
-- Pendente e elegível: nada agendado para o futuro, nada em backoff, nada morto
CREATE INDEX ix_outbound_pending
    ON outbound_queue(scheduled_at, next_retry_at, group_id, seq)
    WHERE sent_at IS NULL AND dead_at IS NULL;

-- Janela de 24h da Cloud API: última mensagem recebida do usuário.
-- `timestamptz + interval` é STABLE (sensível a fuso/DST) e coluna gerada
-- exige IMMUTABLE — o CREATE TABLE falharia. A expiração é calculada na
-- consulta, que é o único lugar onde importa.
CREATE TABLE conversation_window (
    tenant_id       BIGINT PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    last_inbound_at TIMESTAMPTZ NOT NULL
);

-- Predicado canônico para "a janela de 24h está aberta?" (§14.3, §18.4):
--     WHERE last_inbound_at > now() - INTERVAL '24 hours'
"""

# Row level security, from §19.1. It lives in a separate section of the spec
# but belongs in the same migration: a table created without its policy is
# readable across tenants for however long the gap lasts.
RLS = r"""
-- Aplicar a cada tabela tenant-scoped, sem exceção:
--   athlete_profile, consent, subscription, exercise (privados),
--   exercise_alias, workout_session, exercise_set, session_summary,
--   body_metric, health_report, workout_plan, plan_item,
--   training_program, program_phase, program_milestone, raw_message,
--   processing_batch, usage_ledger, outbound_queue, conversation_window
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'athlete_profile','consent','subscription','exercise','exercise_alias',
    'workout_session','exercise_set','session_summary','body_metric',
    'health_report','workout_plan','plan_item','training_program',
    'program_phase','program_milestone','raw_message','processing_batch',
    'usage_ledger','outbound_queue','conversation_window'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING (tenant_id IS NOT DISTINCT FROM
               NULLIF(current_setting('app.tenant_id', true), '')::bigint)
    $f$, t);
  END LOOP;
END $$;
"""

_CREATED = re.compile(r"^CREATE (?:UNIQUE )?(TABLE|TYPE|VIEW)\s+(?:IF NOT EXISTS\s+)?(\w+)", re.M)


def _drop_statements() -> list[str]:
    """Derived from SCHEMA so the two cannot drift: a table added above is
    dropped here without anyone remembering to.

    Deliberately not `DROP SCHEMA public CASCADE`, which also removes
    alembic_version -- Alembic then cannot record the downgrade and the next
    command fails with "relation alembic_version does not exist".
    """
    tables, types, views = [], [], []
    for kind, name in _CREATED.findall(SCHEMA):
        {"TABLE": tables, "TYPE": types, "VIEW": views}[kind].append(name)

    out = []
    if views:
        out.append("DROP VIEW IF EXISTS " + ", ".join(views) + " CASCADE")
    if tables:
        out.append("DROP TABLE IF EXISTS " + ", ".join(tables) + " CASCADE")
    if types:
        out.append("DROP TYPE IF EXISTS " + ", ".join(types) + " CASCADE")
    return out


# asyncpg refuses multiple commands in one prepared statement, so the script is
# split into statements. Naive splitting on ";" breaks three ways: the RLS DO
# block carries semicolons inside dollar quotes, comments can contain them, and
# so can string literals. The scanner below tracks all three.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def _statements(script: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    tag: str | None = None

    while i < n:
        if tag is not None:
            if script.startswith(tag, i):
                buf.append(tag)
                i += len(tag)
                tag = None
            else:
                buf.append(script[i])
                i += 1
            continue

        match = _DOLLAR_TAG.match(script, i)
        if match:
            tag = match.group(0)
            buf.append(tag)
            i = match.end()
            continue

        if script.startswith("--", i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            buf.append(script[i:end])
            i = end
            continue

        if script[i] == "'":
            end = i + 1
            while end < n:
                if script[end] == "'":
                    if end + 1 < n and script[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            buf.append(script[i:end])
            i = end
            continue

        if script[i] == ";":
            _flush(buf, out)
            buf = []
            i += 1
            continue

        buf.append(script[i])
        i += 1

    _flush(buf, out)
    return out


def _flush(buf: list[str], out: list[str]) -> None:
    """Drop anything that is only comments or whitespace: the schema ends with
    two explanatory comment lines, and feeding those to Postgres raises
    "syntax error at end of input"."""
    raw = "".join(buf)
    executable = "\n".join(
        line for line in raw.splitlines() if line.strip() and not line.strip().startswith("--")
    ).strip()
    if executable:
        out.append(raw.strip())


def upgrade() -> None:
    for statement in _statements(SCHEMA):
        op.execute(statement)
    for statement in _statements(RLS):
        op.execute(statement)


def downgrade() -> None:
    for statement in _drop_statements():
        op.execute(statement)

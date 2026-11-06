# ============================================================
# dashboard/data.py — Carregamento e cache de dados
# ============================================================
# Conecta ao banco em 4 modos:
#   1. FORCE_SQLITE=1 (env var) — força SQLite local, ignora PostgreSQL remoto
#   2. DATABASE_URL (env var)   — Railway / banco remoto
#   3. Streamlit Secrets        — Streamlit Cloud
#   4. SQLite local             — dev / testes (fallback)
# ============================================================

import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine


# ==================== CONEXÃO ====================
def _obter_database_url():
    """
    Tenta obter a URL do banco em ordem de prioridade:
        1. Variável de ambiente FORCE_SQLITE=1 — força SQLite local
        2. Streamlit Secrets (formato [connections.postgresql] url = ...)
        3. Streamlit Secrets (chave direta DATABASE_URL)
        4. Variável de ambiente DATABASE_URL (Railway)
        5. SQLite local (fallback para desenvolvimento)
    """
    # 1. Forçar SQLite local (ignora banco remoto)
    if os.environ.get('FORCE_SQLITE', '').strip() in ('1', 'true', 'yes', 'sim'):
        sqlite_path = Path(__file__).resolve().parent.parent / 'dados' / 'tickets.db'
        if sqlite_path.exists():
            st.info(f'ℹ️ Usando SQLite local (FORCE_SQLITE): {sqlite_path.name}')
            return f'sqlite:///{sqlite_path}'
        st.warning('⚠️ FORCE_SQLITE=1 definido, mas banco local não encontrado.')

    # 2. Streamlit Secrets — formato [connections.postgresql]
    try:
        url = st.secrets["connections"]["postgresql"]["url"]
        if url:
            return url
    except (KeyError, FileNotFoundError, Exception):
        pass

    # 3. Streamlit Secrets — chave direta DATABASE_URL
    try:
        url = st.secrets.get("DATABASE_URL")
        if url:
            return url
    except Exception:
        pass

    # 4. Variável de ambiente (Railway)
    url = os.environ.get('DATABASE_URL')
    if url:
        return url

    # 5. Fallback: SQLite local (só pra DEV)
    sqlite_path = Path(__file__).resolve().parent.parent / 'dados' / 'tickets.db'
    if sqlite_path.exists():
        st.info(f'ℹ️ Usando SQLite local: {sqlite_path.name}')
        return f'sqlite:///{sqlite_path}'

    return None


def _limpar_url(url):
    """Remove parâmetros problemáticos do Neon."""
    if not url:
        return url
    url = url.replace('&channel_binding=require', '')
    url = url.replace('?channel_binding=require&', '?')
    url = url.replace('?channel_binding=require', '')
    return url


@st.cache_resource
def get_engine():
    """Cria engine do banco (cached).

    Tenta conectar ao PostgreSQL se DATABASE_URL estiver definido, mas
    cai automaticamente para SQLite local se a conexão falhar.
    Se FORCE_SQLITE=1, ignora o banco remoto e usa SQLite diretamente.
    """
    # 1. FORCE_SQLITE ignora banco remoto
    force_sqlite = os.environ.get('FORCE_SQLITE', '').strip() in (
        '1', 'true', 'yes', 'sim'
    )

    url = _obter_database_url()
    if not url:
        return None

    # SQLite (local) - usado quando FORCE_SQLITE ou sem DATABASE_URL
    if url.startswith('sqlite:///'):
        return create_engine(
            url,
            connect_args={'check_same_thread': False},
        )

    # Postgres (Neon/Railway) - tenta conectar, cai para SQLite se falhar
    if not force_sqlite:
        url = _limpar_url(url)
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=3600,
                pool_size=5,
                max_overflow=10,
                connect_args={
                    'connect_timeout': 10,
                    'sslmode': 'require',
                },
            )
            # Teste real de conexão
            with engine.connect() as conn:
                conn.execute('SELECT 1')
            return engine
        except Exception as e:
            st.warning(
                f'⚠️ Banco remoto inacessível ({type(e).__name__}). '
                f'Usando SQLite local como fallback.'
            )

    # Fallback: SQLite local
    sqlite_path = Path(__file__).resolve().parent.parent / 'dados' / 'tickets.db'
    if sqlite_path.exists():
        st.info(f'ℹ️ Usando SQLite local: {sqlite_path.name}')
        return create_engine(
            f'sqlite:///{sqlite_path}',
            connect_args={'check_same_thread': False},
        )

    return None

# ==================== CARREGAMENTO ====================
@st.cache_data(ttl=3600)
def carregar_tickets():
    engine = get_engine()
    if engine is None:
        return pd.DataFrame()

    try:
        df = pd.read_sql('SELECT * FROM tickets', engine)
    except Exception as e:
        st.error(f'Erro ao carregar tickets: {e}')
        return pd.DataFrame()

    if df.empty:
        return df

    # ---------- CONVERSÕES DE TIPO ----------
    for col in ['criado_data', 'alterado_data', 'previsao',
                'data_resolvido', 'data_1_resolvido']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')

    if 'id' in df.columns:
        df['id'] = pd.to_numeric(df['id'], errors='coerce').astype('Int64')

    # ---------- COLUNAS DERIVADAS ----------
    hoje = pd.Timestamp.now()

    # dias_aberto: dias desde criado_data (só pra tickets em aberto)
    if 'dias_aberto' not in df.columns:
        df['dias_aberto'] = (hoje - df['criado_data']).dt.days

    # dias_resolucao: dias entre criado_data e data_resolvido
    if 'dias_resolucao' not in df.columns:
        if 'data_resolvido' in df.columns:
            df['dias_resolucao'] = (
                df['data_resolvido'] - df['criado_data']
            ).dt.days

    # dias_1_resolucao: dias até a primeira resolução
    if 'dias_1_resolucao' not in df.columns:
        if 'data_1_resolvido' in df.columns:
            df['dias_1_resolucao'] = (
                df['data_1_resolvido'] - df['criado_data']
            ).dt.days

    # sla_status: cumprido / estourado
    if 'sla_status' not in df.columns:
        if 'previsao' in df.columns and 'data_resolvido' in df.columns:
            df['sla_status'] = None
            mask = df['data_resolvido'].notna() & df['previsao'].notna()
            df.loc[mask & (df['data_resolvido'] <= df['previsao']), 'sla_status'] = 'cumprido'
            df.loc[mask & (df['data_resolvido'] > df['previsao']), 'sla_status'] = 'estourado'

    # status_resolvido: True/False
    if 'status_resolvido' not in df.columns and 'status' in df.columns:
        df['status_resolvido'] = df['status'].isin(['Resolvido', 'Fechado'])

    return df


@st.cache_data(ttl=300)
def carregar_movimentacoes():
    """Carrega a tabela `movimentacoes` do banco."""
    engine = get_engine()
    if engine is None:
        return pd.DataFrame()

    try:
        df = pd.read_sql('SELECT * FROM movimentacoes', engine)
    except Exception as e:
        st.warning(f'⚠️ Erro ao carregar movimentações: {e}')
        return pd.DataFrame()

    if not df.empty:
        if 'data_movimentacao' in df.columns:
            df['data_movimentacao'] = pd.to_datetime(
                df['data_movimentacao'], errors='coerce'
            )
        if 'ticket_id' in df.columns:
            df['ticket_id'] = pd.to_numeric(df['ticket_id'], errors='coerce')

    return df


@st.cache_data(ttl=300)
def carregar_mensagens():
    """Carrega a tabela `mensagens` do banco."""
    engine = get_engine()
    if engine is None:
        return pd.DataFrame()

    try:
        df = pd.read_sql('SELECT * FROM mensagens', engine)
    except Exception as e:
        st.warning(f'⚠️ Erro ao carregar mensagens: {e}')
        return pd.DataFrame()

    if not df.empty:
        if 'data_hora' in df.columns:
            df['data_hora'] = pd.to_datetime(df['data_hora'], errors='coerce')
        if 'ticket_id' in df.columns:
            df['ticket_id'] = pd.to_numeric(df['ticket_id'], errors='coerce')

    return df

@st.cache_data(ttl=300)
def carregar_analistas():
    """
    Carrega a tabela `analistas` do banco.
    Usada pela view de Roteamento.
    """
    engine = get_engine()
    if engine is None:
        return pd.DataFrame()

    try:
        df = pd.read_sql('SELECT * FROM analistas', engine)
    except Exception as e:
        st.warning(f'⚠️ Erro ao carregar analistas: {e}')
        return pd.DataFrame()

    return df
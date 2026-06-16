"""
Banco de dados — PostgreSQL (Railway / Supabase).
Tabelas: clientes (emitentes), produtos, notas.
"""
import os
import json
import contextlib
from dotenv import load_dotenv

load_dotenv()


def _conn():
    """Retorna conexao psycopg2. Substituido por SQLite em run_dev.py."""
    import psycopg2
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not url:
        raise RuntimeError("DATABASE_URL nao definida no .env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


@contextlib.contextmanager
def _get_conn():
    """Context manager de conexao — usa _conn() por padrao."""
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dict_cursor(conn):
    """Cursor que retorna dicts — funciona com psycopg2 e SQLite."""
    try:
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    except TypeError:
        # SQLite nao aceita cursor_factory
        return conn.cursor()


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ── Init ──────────────────────────────────────────────────────────

def init_db():
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clientes (
                    id                  TEXT PRIMARY KEY,
                    razao_social        TEXT DEFAULT '',
                    cnpj                TEXT DEFAULT '',
                    ie                  TEXT DEFAULT '',
                    crt                 INTEGER DEFAULT 1,
                    uf                  TEXT DEFAULT '',
                    cuf                 INTEGER DEFAULT 0,
                    cep                 TEXT DEFAULT '',
                    xLgr                TEXT DEFAULT '',
                    nro                 TEXT DEFAULT '',
                    xCpl                TEXT DEFAULT '',
                    xBairro             TEXT DEFAULT '',
                    cMun                TEXT DEFAULT '',
                    xMun                TEXT DEFAULT '',
                    fone                TEXT DEFAULT '',
                    caminho_certificado TEXT DEFAULT '',
                    senha_certificado   TEXT DEFAULT '',
                    numero_nfe          INTEGER DEFAULT 1,
                    serie               INTEGER DEFAULT 1,
                    id_csc              TEXT DEFAULT '000001',
                    csc                 TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS produtos (
                    id      SERIAL PRIMARY KEY,
                    codigo  TEXT NOT NULL,
                    descricao TEXT NOT NULL,
                    ncm     TEXT DEFAULT '',
                    cfop    TEXT DEFAULT '5102',
                    unidade TEXT DEFAULT 'UN',
                    preco   NUMERIC(14,2) DEFAULT 0,
                    orig    TEXT DEFAULT '0',
                    csosn   TEXT DEFAULT '102',
                    cst_icms TEXT DEFAULT '00',
                    p_icms  NUMERIC(6,2) DEFAULT 12
                );

                CREATE TABLE IF NOT EXISTS notas (
                    id              SERIAL PRIMARY KEY,
                    cliente_id      TEXT REFERENCES clientes(id) ON DELETE SET NULL,
                    chave           TEXT DEFAULT '',
                    n_prot          TEXT DEFAULT '',
                    n_nfe           INTEGER DEFAULT 0,
                    serie           INTEGER DEFAULT 1,
                    nat_op          TEXT DEFAULT 'Venda',
                    tp_nf           TEXT DEFAULT '1',
                    id_dest         TEXT DEFAULT '1',
                    ind_final       TEXT DEFAULT '0',
                    ind_pres        TEXT DEFAULT '1',
                    mod_frete       TEXT DEFAULT '9',
                    tp_pag          TEXT DEFAULT '01',
                    cnpj_dest       TEXT DEFAULT '',
                    cpf_dest        TEXT DEFAULT '',
                    xNome_dest      TEXT DEFAULT '',
                    ie_dest         TEXT DEFAULT '',
                    xLgr_dest       TEXT DEFAULT '',
                    nro_dest        TEXT DEFAULT '',
                    xCpl_dest       TEXT DEFAULT '',
                    xBairro_dest    TEXT DEFAULT '',
                    cMun_dest       TEXT DEFAULT '',
                    xMun_dest       TEXT DEFAULT '',
                    uf_dest         TEXT DEFAULT '',
                    cep_dest        TEXT DEFAULT '',
                    email_dest      TEXT DEFAULT '',
                    itens           JSONB DEFAULT '[]',
                    inf_adic        TEXT DEFAULT '',
                    v_nf            NUMERIC(14,2) DEFAULT 0,
                    status          TEXT DEFAULT 'pendente',
                    observacao      TEXT DEFAULT '',
                    arquivo_xml     TEXT DEFAULT '',
                    modelo          INTEGER DEFAULT 55,
                    criado_em       TIMESTAMP DEFAULT NOW()
                );
            """)

    migracoes = [
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS xFant TEXT DEFAULT ''",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS v_desc NUMERIC(14,2) DEFAULT 0",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS v_frete NUMERIC(14,2) DEFAULT 0",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS modelo INTEGER DEFAULT 55",
    ]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            for sql in migracoes:
                try:
                    cur.execute(sql)
                except Exception:
                    pass


# ── Clientes ──────────────────────────────────────────────────────

def listar_clientes():
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM clientes ORDER BY id")
            return _rows(cur)


def carregar_cliente(nome: str):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM clientes WHERE id = %s", (nome,))
            return _row(cur)


def salvar_cliente(nome: str, dados: dict):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clientes
                    (id, razao_social, cnpj, ie, crt, uf, cuf, cep,
                     xLgr, nro, xCpl, xBairro, cMun, xMun, fone, xFant,
                     caminho_certificado, senha_certificado, serie)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    razao_social        = EXCLUDED.razao_social,
                    cnpj                = EXCLUDED.cnpj,
                    ie                  = EXCLUDED.ie,
                    crt                 = EXCLUDED.crt,
                    uf                  = EXCLUDED.uf,
                    cuf                 = EXCLUDED.cuf,
                    cep                 = EXCLUDED.cep,
                    xLgr                = EXCLUDED.xLgr,
                    nro                 = EXCLUDED.nro,
                    xCpl                = EXCLUDED.xCpl,
                    xBairro             = EXCLUDED.xBairro,
                    cMun                = EXCLUDED.cMun,
                    xMun                = EXCLUDED.xMun,
                    fone                = EXCLUDED.fone,
                    xFant               = EXCLUDED.xFant,
                    caminho_certificado = CASE WHEN COALESCE(EXCLUDED.caminho_certificado,'')=''
                                              THEN clientes.caminho_certificado
                                              ELSE EXCLUDED.caminho_certificado END,
                    senha_certificado   = CASE WHEN COALESCE(EXCLUDED.senha_certificado,'')=''
                                              THEN clientes.senha_certificado
                                              ELSE EXCLUDED.senha_certificado END,
                    serie               = EXCLUDED.serie
            """, (
                nome,
                dados.get("razao_social", ""),
                dados.get("cnpj", ""),
                dados.get("ie", ""),
                int(dados.get("crt", 1)),
                dados.get("uf", ""),
                int(dados.get("cuf", 0)),
                dados.get("cep", ""),
                dados.get("xLgr", ""),
                dados.get("nro", ""),
                dados.get("xCpl", ""),
                dados.get("xBairro", ""),
                dados.get("cMun", ""),
                dados.get("xMun", ""),
                dados.get("fone", ""),
                dados.get("xFant", ""),
                dados.get("caminho_certificado", ""),
                dados.get("senha_certificado", ""),
                int(dados.get("serie", 1)),
            ))


def deletar_cliente(nome: str):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clientes WHERE id = %s", (nome,))


def proximo_numero_nfe(cliente_id: str) -> int:
    """Incrementa e retorna o proximo nNF (atomico)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE clientes SET numero_nfe = numero_nfe + 1
                WHERE id = %s RETURNING numero_nfe - 1
            """, (cliente_id,))
            row = cur.fetchone()
    return row[0] if row else 1


# ── Produtos ──────────────────────────────────────────────────────

def listar_produtos():
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM produtos ORDER BY codigo")
            return _rows(cur)


def salvar_produto(dados: dict, produto_id: int = None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if produto_id:
                cur.execute("""
                    UPDATE produtos SET
                        codigo=%s, descricao=%s, ncm=%s, cfop=%s,
                        unidade=%s, preco=%s, orig=%s, csosn=%s,
                        cst_icms=%s, p_icms=%s
                    WHERE id=%s
                """, (
                    dados["codigo"], dados["descricao"], dados.get("ncm", ""),
                    dados.get("cfop", "5102"), dados.get("unidade", "UN"),
                    float(dados.get("preco", 0)), dados.get("orig", "0"),
                    dados.get("csosn", "102"), dados.get("cst_icms", "00"),
                    float(dados.get("p_icms", 12)), produto_id,
                ))
            else:
                cur.execute("""
                    INSERT INTO produtos (codigo, descricao, ncm, cfop, unidade,
                        preco, orig, csosn, cst_icms, p_icms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    dados["codigo"], dados["descricao"], dados.get("ncm", ""),
                    dados.get("cfop", "5102"), dados.get("unidade", "UN"),
                    float(dados.get("preco", 0)), dados.get("orig", "0"),
                    dados.get("csosn", "102"), dados.get("cst_icms", "00"),
                    float(dados.get("p_icms", 12)),
                ))


def deletar_produto(produto_id: int):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM produtos WHERE id = %s", (produto_id,))


# ── Notas ─────────────────────────────────────────────────────────

def criar_nota(dados: dict) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO notas (
                    cliente_id, n_nfe, serie, nat_op, tp_nf, id_dest,
                    ind_final, ind_pres, mod_frete, tp_pag,
                    cnpj_dest, cpf_dest, xNome_dest, ie_dest,
                    xLgr_dest, nro_dest, xCpl_dest, xBairro_dest,
                    cMun_dest, xMun_dest, uf_dest, cep_dest, email_dest,
                    itens, inf_adic, v_nf, v_desc, v_frete, modelo, status
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,'pendente'
                ) RETURNING id
            """, (
                dados.get("cliente_id"),
                int(dados.get("n_nfe", 0)),
                int(dados.get("serie", 1)),
                dados.get("nat_op", "Venda"),
                dados.get("tp_nf", "1"),
                dados.get("id_dest", "1"),
                dados.get("ind_final", "0"),
                dados.get("ind_pres", "1"),
                dados.get("mod_frete", "9"),
                dados.get("tp_pag", "01"),
                dados.get("cnpj_dest", ""),
                dados.get("cpf_dest", ""),
                dados.get("xNome_dest", ""),
                dados.get("ie_dest", ""),
                dados.get("xLgr_dest", ""),
                dados.get("nro_dest", ""),
                dados.get("xCpl_dest", ""),
                dados.get("xBairro_dest", ""),
                dados.get("cMun_dest", ""),
                dados.get("xMun_dest", ""),
                dados.get("uf_dest", ""),
                dados.get("cep_dest", ""),
                dados.get("email_dest", ""),
                json.dumps(dados.get("itens", [])),
                dados.get("inf_adic", ""),
                float(dados.get("v_nf", 0)),
                float(dados.get("v_desc", 0)),
                float(dados.get("v_frete", 0)),
                int(dados.get("modelo", 55)),
            ))
            return cur.fetchone()[0]


def listar_notas(cliente_id: str = None, status: str = None):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            filters, params = [], []
            if cliente_id:
                filters.append("n.cliente_id = %s")
                params.append(cliente_id)
            if status:
                filters.append("n.status = %s")
                params.append(status)
            where = ("WHERE " + " AND ".join(filters)) if filters else ""
            cur.execute(f"""
                SELECT n.*, c.razao_social
                FROM notas n
                LEFT JOIN clientes c ON c.id = n.cliente_id
                {where}
                ORDER BY n.id DESC
            """, params)
            rows = _rows(cur)
            for r in rows:
                if isinstance(r.get("itens"), str):
                    r["itens"] = json.loads(r["itens"])
            return rows


def get_nota(nota_id: int):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("""
                SELECT n.*, c.razao_social, c.cnpj as cnpj_emit,
                       c.uf, c.ie, c.crt, c.xLgr, c.nro, c.xCpl,
                       c.xBairro, c.cMun, c.xMun, c.cep, c.fone,
                       c.caminho_certificado, c.senha_certificado,
                       c.serie as serie_emit, c.xFant
                FROM notas n
                LEFT JOIN clientes c ON c.id = n.cliente_id
                WHERE n.id = %s
            """, (nota_id,))
            r = _row(cur)
            if r and isinstance(r.get("itens"), str):
                r["itens"] = json.loads(r["itens"])
            return r


def update_nota_status(nota_id: int, status: str, obs: str = None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET status=%s, observacao=%s WHERE id=%s",
                (status, obs, nota_id),
            )


def update_nota_emitida(nota_id: int, chave: str, n_prot: str, xml_path: str):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET chave=%s, n_prot=%s, arquivo_xml=%s, status='emitido' WHERE id=%s",
                (chave, n_prot, xml_path, nota_id),
            )


def excluir_nota(nota_id: int):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notas WHERE id = %s", (nota_id,))


def update_nota_cancelada(nota_id: int):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET status='cancelado' WHERE id=%s", (nota_id,)
            )

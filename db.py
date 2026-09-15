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


# Colunas com maiúscula/minúscula misturada (xLgr, xNome_dest, etc.) — o
# Postgres dobra identificadores não citados pra minúsculas ao devolver o
# resultado (vira "xlgr"), mas o SQLite do run_dev.py preserva o nome
# exatamente como foi declarado ("xLgr"). O resto do código (webapp.py,
# api.py, templates) sempre leu/lê pelo nome em camelCase original — o que
# funciona em dev (SQLite) mas voltava vazio em produção (Postgres). Em vez
# de reescrever todo SELECT com aliases citados, normaliza aqui: se a chave
# camelCase não veio (ou veio vazia) mas a versão em minúsculas tem valor,
# copia pra camelCase.
_CAMPOS_CASE_MISTO = [
    "xLgr", "xCpl", "xBairro", "cMun", "xMun", "xFant",
    "xNome_dest", "xLgr_dest", "xCpl_dest", "xBairro_dest",
    "cMun_dest", "xMun_dest",
]


def _normalizar_case_misto(row):
    if not row:
        return row
    for campo in _CAMPOS_CASE_MISTO:
        if not row.get(campo):
            v = row.get(campo.lower())
            if v:
                row[campo] = v
    return row


def _garantir_xml_disco(row):
    """Re-materializa o procNFe em disco a partir do Postgres se o arquivo
    sumiu (disco efêmero do Railway some a cada redeploy) — sem isso,
    download de XML e geração de PDF (que leem tpAmb/dhRecbto/QR do
    arquivo) quebravam depois do primeiro deploy seguinte à emissão."""
    if not row:
        return row
    caminho = row.get("arquivo_xml")
    conteudo = row.get("xml_conteudo")
    if caminho and conteudo and not os.path.isfile(caminho):
        try:
            os.makedirs(os.path.dirname(caminho), exist_ok=True)
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(conteudo)
        except Exception:
            pass
    return row


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

                CREATE TABLE IF NOT EXISTS api_emissoes (
                    id              SERIAL PRIMARY KEY,
                    emitente_id     TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    modelo          INTEGER NOT NULL,
                    chave           TEXT DEFAULT '',
                    n_prot          TEXT DEFAULT '',
                    nota_id         INTEGER,
                    xml_path        TEXT DEFAULT '',
                    criado_em       TIMESTAMP DEFAULT NOW(),
                    UNIQUE (emitente_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS api_logs (
                    id             SERIAL PRIMARY KEY,
                    request_id     TEXT,
                    metodo         TEXT,
                    endpoint       TEXT,
                    emitente_id    TEXT,
                    status_code    INTEGER,
                    sucesso        BOOLEAN,
                    erro           TEXT,
                    duracao_ms     INTEGER,
                    criado_em      TIMESTAMP DEFAULT NOW()
                );
            """)

    migracoes = [
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS xFant TEXT DEFAULT ''",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS v_desc NUMERIC(14,2) DEFAULT 0",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS v_frete NUMERIC(14,2) DEFAULT 0",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS modelo INTEGER DEFAULT 55",
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS numero_nfce INTEGER DEFAULT 1",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS fin_nfe TEXT DEFAULT '1'",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS ref_nfe TEXT DEFAULT ''",
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS certificado_pfx BYTEA",
        "ALTER TABLE notas ADD COLUMN IF NOT EXISTS xml_conteudo TEXT",
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
            rows = _rows(cur)
            for r in rows:
                r.pop("certificado_pfx", None)
                _normalizar_case_misto(r)
            return rows


def carregar_cliente(nome: str):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM clientes WHERE id = %s", (nome,))
            row = _row(cur)
            if row:
                row.pop("certificado_pfx", None)
            return _normalizar_case_misto(row)


def salvar_cliente(nome: str, dados: dict):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clientes
                    (id, razao_social, cnpj, ie, crt, uf, cuf, cep,
                     xLgr, nro, xCpl, xBairro, cMun, xMun, fone, xFant,
                     caminho_certificado, senha_certificado, serie, id_csc, csc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    razao_social        = CASE WHEN COALESCE(EXCLUDED.razao_social,'')=''
                                              THEN clientes.razao_social
                                              ELSE EXCLUDED.razao_social END,
                    cnpj                = CASE WHEN COALESCE(EXCLUDED.cnpj,'')=''
                                              THEN clientes.cnpj
                                              ELSE EXCLUDED.cnpj END,
                    ie                  = CASE WHEN COALESCE(EXCLUDED.ie,'')=''
                                              THEN clientes.ie
                                              ELSE EXCLUDED.ie END,
                    crt                 = EXCLUDED.crt,
                    uf                  = CASE WHEN COALESCE(EXCLUDED.uf,'')=''
                                              THEN clientes.uf
                                              ELSE EXCLUDED.uf END,
                    cuf                 = EXCLUDED.cuf,
                    cep                 = CASE WHEN COALESCE(EXCLUDED.cep,'')=''
                                              THEN clientes.cep
                                              ELSE EXCLUDED.cep END,
                    xLgr                = CASE WHEN COALESCE(EXCLUDED.xLgr,'')=''
                                              THEN clientes.xLgr
                                              ELSE EXCLUDED.xLgr END,
                    nro                 = CASE WHEN COALESCE(EXCLUDED.nro,'')=''
                                              THEN clientes.nro
                                              ELSE EXCLUDED.nro END,
                    xCpl                = EXCLUDED.xCpl,
                    xBairro             = CASE WHEN COALESCE(EXCLUDED.xBairro,'')=''
                                              THEN clientes.xBairro
                                              ELSE EXCLUDED.xBairro END,
                    cMun                = CASE WHEN COALESCE(EXCLUDED.cMun,'')=''
                                              THEN clientes.cMun
                                              ELSE EXCLUDED.cMun END,
                    xMun                = CASE WHEN COALESCE(EXCLUDED.xMun,'')=''
                                              THEN clientes.xMun
                                              ELSE EXCLUDED.xMun END,
                    fone                = EXCLUDED.fone,
                    xFant               = EXCLUDED.xFant,
                    caminho_certificado = CASE WHEN COALESCE(EXCLUDED.caminho_certificado,'')=''
                                              THEN clientes.caminho_certificado
                                              ELSE EXCLUDED.caminho_certificado END,
                    senha_certificado   = CASE WHEN COALESCE(EXCLUDED.senha_certificado,'')=''
                                              THEN clientes.senha_certificado
                                              ELSE EXCLUDED.senha_certificado END,
                    serie               = EXCLUDED.serie,
                    id_csc              = CASE WHEN COALESCE(EXCLUDED.id_csc,'')=''
                                              THEN clientes.id_csc
                                              ELSE EXCLUDED.id_csc END,
                    csc                 = CASE WHEN COALESCE(EXCLUDED.csc,'')=''
                                              THEN clientes.csc
                                              ELSE EXCLUDED.csc END
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
                dados.get("id_csc", "000001"),
                dados.get("csc", ""),
            ))


def deletar_cliente(nome: str):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clientes WHERE id = %s", (nome,))


def salvar_certificado_bytes(cliente_id: str, dados: bytes):
    """Guarda o .pfx no Postgres (persistente) — o disco do container
    (certs/) é efêmero e some a cada deploy no Railway."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clientes SET certificado_pfx = %s WHERE id = %s",
                (dados, cliente_id),
            )


def get_certificado_bytes(cliente_id: str):
    """Recupera os bytes do .pfx do Postgres, para re-materializar o
    arquivo em disco quando o container perdeu o que tinha (redeploy)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT certificado_pfx FROM clientes WHERE id = %s", (cliente_id,))
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            return bytes(row[0])


def proximo_numero_nfe(cliente_id: str, modelo: int = 55) -> int:
    """Incrementa e retorna o proximo nNF (atomico).

    NF-e (55) e NFC-e (65) sao series numericas INDEPENDENTES na SEFAZ —
    por isso cada modelo tem sua propria coluna de contador.
    """
    coluna = "numero_nfe" if modelo == 55 else "numero_nfce"
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE clientes SET {coluna} = {coluna} + 1
                WHERE id = %s RETURNING {coluna} - 1
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
                    itens, inf_adic, v_nf, v_desc, v_frete, modelo,
                    fin_nfe, ref_nfe, status
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,'pendente'
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
                dados.get("fin_nfe", "1"),
                dados.get("ref_nfe", ""),
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
                _normalizar_case_misto(r)
            return rows


def get_nota(nota_id: int):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("""
                SELECT n.*, c.razao_social, c.cnpj as cnpj_emit,
                       c.uf, c.ie, c.crt, c.xLgr, c.nro, c.xCpl,
                       c.xBairro, c.cMun, c.xMun, c.cep, c.fone,
                       c.caminho_certificado, c.senha_certificado,
                       c.serie as serie_emit, c.xFant, c.id_csc, c.csc
                FROM notas n
                LEFT JOIN clientes c ON c.id = n.cliente_id
                WHERE n.id = %s
            """, (nota_id,))
            r = _row(cur)
            if r and isinstance(r.get("itens"), str):
                r["itens"] = json.loads(r["itens"])
            r = _normalizar_case_misto(r)
            return _garantir_xml_disco(r)


def update_nota_status(nota_id: int, status: str, obs: str = None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET status=%s, observacao=%s WHERE id=%s",
                (status, obs, nota_id),
            )


def update_nota_emitida(nota_id: int, chave: str, n_prot: str, xml_path: str):
    # Guarda o conteúdo do XML também no Postgres — o disco do container
    # (downloads/) é efêmero no Railway e some a cada redeploy, quebrando
    # download do XML e geração de PDF (que lê tpAmb/dhRecbto etc. do
    # arquivo). Lido aqui, na hora, enquanto o arquivo ainda existe.
    xml_conteudo = None
    try:
        if xml_path and os.path.isfile(xml_path):
            with open(xml_path, "r", encoding="utf-8") as f:
                xml_conteudo = f.read()
    except Exception:
        pass
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET chave=%s, n_prot=%s, arquivo_xml=%s, "
                "xml_conteudo=%s, status='emitido' WHERE id=%s",
                (chave, n_prot, xml_path, xml_conteudo, nota_id),
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


def get_nota_por_chave(chave: str):
    """Localiza a nota (emitida pela API ou pelo admin) por chave de acesso —
    usado pela API pra cancelar/gerar DANFE sem o chamador reenviar dados."""
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute("""
                SELECT n.*, c.razao_social, c.cnpj as cnpj_emit,
                       c.uf, c.ie, c.crt, c.xLgr, c.nro, c.xCpl,
                       c.xBairro, c.cMun, c.xMun, c.cep, c.fone,
                       c.caminho_certificado, c.senha_certificado,
                       c.serie as serie_emit, c.xFant, c.id_csc, c.csc
                FROM notas n
                LEFT JOIN clientes c ON c.id = n.cliente_id
                WHERE n.chave = %s
                ORDER BY n.id DESC LIMIT 1
            """, (chave,))
            r = _row(cur)
            if r and isinstance(r.get("itens"), str):
                r["itens"] = json.loads(r["itens"])
            r = _normalizar_case_misto(r)
            return _garantir_xml_disco(r)


# ── Idempotência da API (evita duplicar número em retentativas) ────

def buscar_emissao_idempotente(emitente_id: str, idempotency_key: str):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM api_emissoes WHERE emitente_id=%s AND idempotency_key=%s",
                (emitente_id, idempotency_key),
            )
            return _row(cur)


def registrar_emissao_idempotente(emitente_id: str, idempotency_key: str, modelo: int,
                                   chave: str, n_prot: str, nota_id: int, xml_path: str):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO api_emissoes
                    (emitente_id, idempotency_key, modelo, chave, n_prot, nota_id, xml_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (emitente_id, idempotency_key) DO NOTHING
            """, (emitente_id, idempotency_key, modelo, chave, n_prot, nota_id, xml_path))


# ── Log de chamadas da API (auditoria/diagnóstico) ──────────────────

def registrar_api_log(rec: dict):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO api_logs
                    (request_id, metodo, endpoint, emitente_id, status_code,
                     sucesso, erro, duracao_ms)
                VALUES (%(request_id)s, %(metodo)s, %(endpoint)s, %(emitente_id)s,
                        %(status_code)s, %(sucesso)s, %(erro)s, %(duracao_ms)s)
            """, rec)


def listar_api_logs(limit: int = 200, apenas_erros: bool = False):
    with _get_conn() as conn:
        with _dict_cursor(conn) as cur:
            where = "WHERE sucesso = FALSE" if apenas_erros else ""
            cur.execute(f"SELECT * FROM api_logs {where} ORDER BY id DESC LIMIT %s", (limit,))
            return _rows(cur)

"""
Servidor de desenvolvimento — usa SQLite em arquivo local.
Não requer PostgreSQL nem certificado real.

Uso: python run_dev.py
"""
import os, json, sqlite3, contextlib, re

os.environ.setdefault("NFE_AMBIENTE", "2")
os.environ.setdefault("ADMIN_USER",   "admin")
os.environ.setdefault("ADMIN_PASS",   "admin")
os.environ.setdefault("SECRET_KEY",   "dev-secret-nfe-2025")

DB_PATH = os.path.join(os.path.dirname(__file__), "dev.db")

# ── Patch db._conn para usar SQLite ───────────────────────────────
import db as _db

class _DictCursor:
    """Cursor SQLite que se comporta como RealDictCursor do psycopg2."""
    def __init__(self, conn):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        # converte %s → ? e %%(name)s → :name
        sql = sql.replace("%s", "?")
        import re
        sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
        self._cur.execute(sql, params)

    def executescript(self, sql):
        self._cur.executescript(sql)

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None: return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return [dict(zip(cols, r)) for r in self._cur.fetchall()]

    @property
    def description(self):
        return self._cur.description

    def __enter__(self): return self
    def __exit__(self, *a): pass


class _SQLiteConn:
    """Conexão SQLite que aceita cursor_factory (ignora — usa _DictCursor sempre)."""
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.autocommit = False

    def cursor(self, cursor_factory=None):
        return _DictCursor(self._conn)

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()

    def __enter__(self): return self
    def __exit__(self, *a): self._conn.commit()


_shared_conn = _SQLiteConn(DB_PATH)


@contextlib.contextmanager
def _sqlite_conn():
    yield _shared_conn

# Patcha _get_conn e _dict_cursor (os hooks usados por todas as funcoes de db.py)
_db._get_conn = _sqlite_conn

def _sqlite_dict_cursor(conn):
    return conn.cursor()

_db._dict_cursor = _sqlite_dict_cursor

# Recria init_db com SQLite
def _init_sqlite():
    with _sqlite_conn() as conn:
        conn.cursor().executescript("""
            CREATE TABLE IF NOT EXISTS clientes (
                id TEXT PRIMARY KEY, razao_social TEXT DEFAULT '',
                cnpj TEXT DEFAULT '', ie TEXT DEFAULT '',
                crt INTEGER DEFAULT 1, uf TEXT DEFAULT '',
                cuf INTEGER DEFAULT 0, cep TEXT DEFAULT '',
                xLgr TEXT DEFAULT '', nro TEXT DEFAULT '',
                xCpl TEXT DEFAULT '', xBairro TEXT DEFAULT '',
                cMun TEXT DEFAULT '', xMun TEXT DEFAULT '',
                fone TEXT DEFAULT '', xFant TEXT DEFAULT '',
                caminho_certificado TEXT DEFAULT '',
                senha_certificado TEXT DEFAULT '',
                numero_nfe INTEGER DEFAULT 1, serie INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT NOT NULL, descricao TEXT NOT NULL,
                ncm TEXT DEFAULT '', cfop TEXT DEFAULT '5102',
                unidade TEXT DEFAULT 'UN', preco REAL DEFAULT 0,
                orig TEXT DEFAULT '0', csosn TEXT DEFAULT '102',
                cst_icms TEXT DEFAULT '00', p_icms REAL DEFAULT 12
            );
            CREATE TABLE IF NOT EXISTS notas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id TEXT, chave TEXT DEFAULT '',
                n_prot TEXT DEFAULT '', n_nfe INTEGER DEFAULT 0,
                serie INTEGER DEFAULT 1, nat_op TEXT DEFAULT 'Venda',
                tp_nf TEXT DEFAULT '1', id_dest TEXT DEFAULT '1',
                ind_final TEXT DEFAULT '0', ind_pres TEXT DEFAULT '1',
                mod_frete TEXT DEFAULT '9', tp_pag TEXT DEFAULT '01',
                cnpj_dest TEXT DEFAULT '', cpf_dest TEXT DEFAULT '',
                xNome_dest TEXT DEFAULT '', ie_dest TEXT DEFAULT '',
                xLgr_dest TEXT DEFAULT '', nro_dest TEXT DEFAULT '',
                xCpl_dest TEXT DEFAULT '', xBairro_dest TEXT DEFAULT '',
                cMun_dest TEXT DEFAULT '', xMun_dest TEXT DEFAULT '',
                uf_dest TEXT DEFAULT '', cep_dest TEXT DEFAULT '',
                email_dest TEXT DEFAULT '', itens TEXT DEFAULT '[]',
                inf_adic TEXT DEFAULT '', v_nf REAL DEFAULT 0,
                v_desc REAL DEFAULT 0, v_frete REAL DEFAULT 0,
                status TEXT DEFAULT 'pendente', observacao TEXT DEFAULT '',
                arquivo_xml TEXT DEFAULT '', modelo INTEGER DEFAULT 55,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

    # Seed dados de demo (só na primeira vez)
    # Migracoes SQLite
    with _sqlite_conn() as conn:
        cur = conn.cursor()
        for col_sql in [
            "ALTER TABLE notas ADD COLUMN modelo INTEGER DEFAULT 55",
            "ALTER TABLE clientes ADD COLUMN id_csc TEXT DEFAULT '000001'",
            "ALTER TABLE clientes ADD COLUMN csc TEXT DEFAULT ''",
        ]:
            try:
                cur.execute(col_sql)
                conn.commit()
            except Exception:
                pass

    with _sqlite_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM clientes", ())
        row = cur.fetchone()
        count = list(row.values())[0] if row else 0
        if count == 0:
            cur.execute("""
                INSERT INTO clientes (id, razao_social, cnpj, ie, crt, uf, cuf,
                    xLgr, nro, xBairro, cMun, xMun, cep, serie)
                VALUES ('empresa_demo','EMPRESA DEMO LTDA','12345678000195',
                        '1234567890123',1,'MG',31,
                        'Rua das Flores','100','Centro','3106200',
                        'Belo Horizonte','30130110',1)
            """)
            cur.execute("""
                INSERT INTO produtos (codigo, descricao, ncm, cfop, unidade, preco, orig, csosn)
                VALUES ('PROD001','Produto Exemplo A','84713012','5102','UN',250.00,'0','102')
            """)
            cur.execute("""
                INSERT INTO produtos (codigo, descricao, ncm, cfop, unidade, preco, orig, csosn)
                VALUES ('PROD002','Produto Exemplo B','39269090','5102','KG',85.50,'0','102')
            """)
            cur.execute("""
                INSERT INTO notas (cliente_id, n_nfe, serie, xNome_dest, cnpj_dest,
                    uf_dest, xMun_dest, v_nf, status, chave, n_prot, itens, modelo)
                VALUES ('empresa_demo',1,1,'CLIENTE EXEMPLO SA','98765432000110',
                        'SP','São Paulo',500.00,'emitido',
                        '35250612345678000195550010000000011234567890',
                        '135250612345678',
                        '[{"nItem":1,"cProd":"PROD001","xProd":"Produto Exemplo A","qCom":2,"vUnCom":250.0,"CFOP":"5102","NCM":"84713012","CSOSN":"102","orig":"0"}]',
                        55)
            """)
            cur.execute("""
                INSERT INTO notas (cliente_id, n_nfe, serie, xNome_dest, cpf_dest,
                    uf_dest, xMun_dest, v_nf, status, chave, n_prot, itens, modelo,
                    nat_op, ind_final, ind_pres, tp_pag)
                VALUES ('empresa_demo',4,1,'JOAO DA SILVA','98765432100',
                        'MG','Belo Horizonte',171.00,'emitido',
                        '31260612345678000195650010000000041987654320',
                        '365260612345678',
                        '[{"nItem":1,"cProd":"PROD002","xProd":"Produto Exemplo B","qCom":2,"vUnCom":85.50,"CFOP":"5102","NCM":"39269090","CSOSN":"102","orig":"0"}]',
                        65, 'Venda a Consumidor','1','1','04')
            """)
            cur.execute("""
                INSERT INTO notas (cliente_id, n_nfe, serie, xNome_dest, cnpj_dest,
                    uf_dest, xMun_dest, v_nf, status, itens)
                VALUES ('empresa_demo',2,1,'OUTRO CLIENTE LTDA','11222333000144',
                        'RJ','Rio de Janeiro',1200.00,'pendente','[]')
            """)
            cur.execute("""
                INSERT INTO notas (cliente_id, n_nfe, serie, xNome_dest, cpf_dest,
                    uf_dest, xMun_dest, v_nf, status, itens)
                VALUES ('empresa_demo',3,1,'MARIA DA SILVA','12345678901',
                        'MG','Belo Horizonte',180.00,'erro',
                        '[{"nItem":1,"cProd":"PROD002","xProd":"Produto Exemplo B","qCom":2,"vUnCom":90.0}]')
            """)
            conn.commit()
            print("[dev] Dados de demo inseridos.")

# Patch das funções que usam psycopg2.extras.RealDictCursor
# (SQLite usa Row nativo — já configurado acima)
# Precisamos reescrever _row/_rows para lidar com sqlite3.Row
def _row_sq(cur):
    r = cur.fetchone()
    return dict(r) if r else None

def _rows_sq(cur):
    return [dict(r) for r in cur.fetchall()]

_db._row = _row_sq
_db._rows = _rows_sq

# Patch proximo_numero_nfe para SQLite (sem RETURNING em versões antigas)
def _proximo_nfe(cliente_id):
    with _sqlite_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT numero_nfe FROM clientes WHERE id=%s", (cliente_id,))
        row = cur.fetchone()
        num = row["numero_nfe"] if row else 1
        cur.execute("UPDATE clientes SET numero_nfe=numero_nfe+1 WHERE id=%s", (cliente_id,))
        conn.commit()
    return num

_db.proximo_numero_nfe = _proximo_nfe

# Patch init_db
_db.init_db = _init_sqlite

# ── Modo simulacao — emite sem certificado nem SEFAZ ─────────────
import random, datetime

def _emitir_simulado(dados):
    """Gera chave, monta XML e simula protocolo sem contatar a SEFAZ."""
    modelo  = int(dados.get("modelo", 55))
    uf      = dados.get("uf", "35")
    cnpj    = re.sub(r"\D", "", dados.get("cnpj_emitente", dados.get("cnpj", "12345678000195")))
    serie   = str(dados.get("serie", 1)).zfill(3)
    n_nfe   = str(dados.get("nnf", dados.get("n_nfe", 1))).zfill(9)
    agora   = datetime.datetime.now()
    aamm    = agora.strftime("%y%m")
    cuf     = str(dados.get("cuf", 35)).zfill(2)
    cnf     = str(random.randint(10000000, 99999999))

    # chave sem dv
    chave43 = f"{cuf}{aamm}{cnpj}{modelo:02d}{serie}{n_nfe}1{cnf}"
    dv = _nfe_api._calcular_dv(chave43)
    chave = chave43 + str(dv)

    n_prot  = "3" + agora.strftime("%Y%m%d%H%M%S") + str(random.randint(100, 999))

    # QR Code simulado para NFC-e
    url_qr = ""
    if modelo == 65:
        url_qr = f"https://www.nfce.fazenda.sp.gov.br/consulta?p={chave}|2|||{random.randint(10000,99999)}"

    # XML minimo para visualizacao
    ns = "http://www.portalfiscal.inf.br/nfe"
    supl_xml = (f"<infNFeSupl><qrCode>{url_qr}</qrCode>"
                f"<urlChave>https://www.nfce.fazenda.sp.gov.br/consulta</urlChave></infNFeSupl>"
                if modelo == 65 else "")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc versao="4.00" xmlns="{ns}">
  <NFe xmlns="{ns}">
    <infNFe Id="NFe{chave}" versao="4.00">
      <ide>
        <cUF>{cuf}</cUF><cNF>{cnf}</cNF><natOp>{dados.get('nat_op','Venda')}</natOp>
        <mod>{modelo}</mod><serie>{dados.get('serie',1)}</serie><nNF>{dados.get('nnf',dados.get('n_nfe',1))}</nNF>
        <dhEmi>{agora.strftime('%Y-%m-%dT%H:%M:%S')}-03:00</dhEmi>
        <tpNF>{dados.get('tp_nf','1')}</tpNF><idDest>1</idDest>
        <cMunFG>{dados.get('cMun','3550308')}</cMunFG>
        <tpImp>1</tpImp><tpEmis>1</tpEmis><cDV>{dv}</cDV>
        <tpAmb>2</tpAmb><finNFe>1</finNFe><indFinal>1</indFinal>
        <indPres>1</indPres><procEmi>0</procEmi><verProc>DEV-1.0</verProc>
      </ide>
      <emit>
        <CNPJ>{cnpj}</CNPJ>
        <xNome>{dados.get('razao_social','EMPRESA DEMO')}</xNome>
        <CRT>{dados.get('crt',1)}</CRT>
      </emit>
      <dest>
        <xNome>{dados.get('xNome_destinatario', dados.get('xNome_dest','CONSUMIDOR'))}</xNome>
      </dest>
      <total><ICMSTot>
        <vNF>{float(dados.get('v_nf',0)):.2f}</vNF>
      </ICMSTot></total>
    </infNFe>
  </NFe>
  <protNFe versao="4.00">
    <infProt>
      <tpAmb>2</tpAmb><verAplic>DEV</verAplic>
      <chNFe>{chave}</chNFe>
      <dhRecbto>{agora.strftime('%Y-%m-%dT%H:%M:%S')}-03:00</dhRecbto>
      <nProt>{n_prot}</nProt><digVal>simulado</digVal>
      <cStat>100</cStat><xMotivo>Autorizado o uso da NF-e [SIMULACAO]</xMotivo>
    </infProt>
  </protNFe>
  {supl_xml}
</nfeProc>"""

    # salva XML
    xml_dir = os.path.join(os.path.dirname(__file__), "xmls_dev")
    os.makedirs(xml_dir, exist_ok=True)
    xml_path = os.path.join(xml_dir, f"NFe{chave}.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml)

    return {
        "chave":    chave,
        "n_prot":   n_prot,
        "cStat":    "100",
        "xMotivo":  "Autorizado o uso da NF-e [SIMULACAO DEV]",
        "xml_path": xml_path,
    }

import fluxo_nfe_api as _nfe_api
_nfe_api.emitir_nfe  = _emitir_simulado
_nfe_api._calcular_dv  # garante que a funcao existe

# ── Inicializar e rodar ───────────────────────────────────────────
_init_sqlite()

from webapp import app
print("\n  Emissor NF-e — Servidor de Desenvolvimento")
print("  Acesse: http://localhost:5000")
print("  Login:  admin / admin\n")
port = int(os.environ.get("PORT", 5000))
app.run(debug=True, port=port, use_reloader=False)

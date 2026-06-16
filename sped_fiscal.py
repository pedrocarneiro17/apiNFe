"""
Gerador de EFD-ICMS/IPI (SPED Fiscal) — Leiaute 18 (versão atual).

Gera o arquivo .txt mensal a partir das notas fiscais emitidas no banco.
Suporta Lucro Presumido e Lucro Real (CRT=3).

Blocos gerados:
  0 — Abertura, identificação e referências
  C — Documentos fiscais I (NF-e modelo 55 e NFC-e modelo 65)
  E — Apuração do ICMS
  9 — Controle e encerramento

Uso:
    from sped_fiscal import gerar_efd
    txt = gerar_efd(cliente, notas, ano=2025, mes=6)
    # txt é bytes UTF-8 pronto para salvar como .txt
"""

import json
from datetime import date, datetime
from typing import Any

# ── Helpers ───────────────────────────────────────────────────────────────────

def _n(v, dec=2) -> str:
    """Formata número para SPED (vírgula decimal, sem separador de milhar)."""
    try:
        return f"{float(v or 0):.{dec}f}".replace(".", ",")
    except Exception:
        return "0," + "0" * dec


def _d(v) -> str:
    """Formata data para DDMMAAAA."""
    if isinstance(v, (date, datetime)):
        return v.strftime("%d%m%Y")
    if v:
        # tenta converter string ISO
        try:
            return datetime.fromisoformat(str(v)[:10]).strftime("%d%m%Y")
        except Exception:
            pass
    return ""


def _s(v, max_len: int = 0) -> str:
    """String limpa; trunca se max_len > 0."""
    s = str(v or "").strip()
    return s[:max_len] if max_len else s


def _so_num(v) -> str:
    return "".join(c for c in str(v or "") if c.isdigit())


def _reg(*campos) -> str:
    """Monta uma linha de registro SPED."""
    return "|" + "|".join(str(c) for c in campos) + "|\n"


# ── Controle de totais por bloco ──────────────────────────────────────────────

class _Contador:
    def __init__(self):
        self._total = 0
        self._blocos: dict[str, int] = {}

    def inc(self, reg: str):
        self._total += 1
        bloco = reg[0]
        self._blocos[bloco] = self._blocos.get(bloco, 0) + 1

    @property
    def total(self):
        return self._total

    def qtd_bloco(self, bloco: str) -> int:
        return self._blocos.get(bloco, 0)


# ── Gerador principal ─────────────────────────────────────────────────────────

def gerar_efd(cliente: dict, notas: list[dict], ano: int, mes: int) -> bytes:
    """
    Gera o arquivo EFD-ICMS/IPI completo.

    cliente: dict com campos da tabela clientes (razao_social, cnpj, ie, uf, etc.)
    notas:   lista de dicts da tabela notas com itens já desserializados
    ano/mes: período de apuração
    """
    linhas: list[str] = []
    cnt = _Contador()

    def w(reg: str, *campos):
        linha = _reg(reg, *campos)
        linhas.append(linha)
        cnt.inc(reg)
        return linha

    cnpj     = _so_num(cliente.get("cnpj", ""))
    ie       = _so_num(cliente.get("ie", ""))
    uf       = _s(cliente.get("uf", ""))
    razao    = _s(cliente.get("razao_social", ""), 100)
    cep      = _so_num(cliente.get("cep", ""))
    fone     = _so_num(cliente.get("fone", ""))
    crt      = int(cliente.get("crt", 3))
    ind_perfil = "A"   # Perfil A = completo (Lucro Real/Presumido)
    ind_ativ   = "0"   # 0 = outros

    dt_ini = date(ano, mes, 1)
    # último dia do mês
    if mes == 12:
        dt_fim = date(ano, 12, 31)
    else:
        dt_fim = date(ano, mes + 1, 1).replace(day=1)
        from datetime import timedelta
        dt_fim = dt_fim - timedelta(days=1)

    # ── BLOCO 0 ───────────────────────────────────────────────────────────────
    w("0000",
      "015",           # versão leiaute
      "0",             # tipo de escrita (0=original)
      _d(dt_ini),      # data inicial
      _d(dt_fim),      # data final
      razao,
      cnpj,
      "",              # CPF (PJ = vazio)
      uf,
      ie,
      "",              # cod_mun (IBGE 7 dig — preenchido abaixo)
      "",              # IE_ST
      "",              # IM
      "",              # SUFRAMA
      ind_perfil,
      ind_ativ,
    )

    w("0001", "0")     # abertura bloco 0 (indicador de movimento: 0=com dados)

    # 0005 — Dados complementares do contribuinte
    w("0005",
      _s(cliente.get("xFant", razao), 60),  # fantasia
      fone,
      "",              # fax
      "",              # email
    )

    # 0100 — Dados do contador (opcional — deixamos em branco)
    # w("0100", ...) — omitido

    # 0150 — Tabela de cadastro de participantes (destinatários únicos)
    participantes: dict[str, dict] = {}
    for nota in notas:
        doc = _so_num(nota.get("cnpj_dest", "") or nota.get("cpf_dest", ""))
        if doc and doc not in participantes:
            participantes[doc] = nota

    for cod_part, (doc, nota) in enumerate(participantes.items(), 1):
        cnpj_dest = _so_num(nota.get("cnpj_dest", ""))
        cpf_dest  = _so_num(nota.get("cpf_dest", ""))
        w("0150",
          str(cod_part),                          # cod_part
          _s(nota.get("xNome_dest", ""), 60),     # nome
          _s(nota.get("cod_pais", "1058")),        # cod_pais
          cnpj_dest,                               # CNPJ
          cpf_dest,                                # CPF
          "",                                      # IE
          "",                                      # cod_mun
          "",                                      # suframa
          "",                                      # end
          "",                                      # num
          "",                                      # compl
          "",                                      # bairro
        )

    # Mapa doc → cod_part para usar nos registros C
    doc_to_part = {
        _so_num(n.get("cnpj_dest", "") or n.get("cpf_dest", "")): str(i)
        for i, n in enumerate(participantes.values(), 1)
    }

    # 0190 — Unidades de medida
    unidades: set[str] = set()
    for nota in notas:
        for item in _itens(nota):
            u = _s(item.get("uCom") or item.get("unidade", "UN"), 6).upper()
            unidades.add(u)
    for u in sorted(unidades):
        w("0190", u, "")

    # 0200 — Tabela de identificação de itens
    produtos: dict[str, dict] = {}
    for nota in notas:
        for item in _itens(nota):
            cod = _s(item.get("cProd") or item.get("codigo", ""), 60)
            if cod and cod not in produtos:
                produtos[cod] = item
    for cod, item in produtos.items():
        w("0200",
          cod,                                              # cod_item
          _s(item.get("xProd", ""), 60),                   # descr_item
          _s(item.get("cEAN", ""), 14),                    # cod_barra
          "",                                               # cod_ant_item
          _s(item.get("uCom") or item.get("unidade","UN"), 6).upper(),  # unid_inv
          "00",                                             # tipo_item (00=merc p/revenda)
          _so_num(item.get("NCM") or item.get("ncm","00000000"))[:8],  # cod_ncm
          "",                                               # ex_ipi
          _s(item.get("CFOP") or item.get("cfop","5102")), # cod_gen
          "",                                               # cod_lst
          _n(item.get("vUnCom") or item.get("preco", 0)),  # aliq_icms
          "",                                               # cest
        )

    w("0990", str(cnt.qtd_bloco("0") + 1))  # encerramento bloco 0

    # ── BLOCO C ───────────────────────────────────────────────────────────────
    w("C001", "0")

    # Agrupa apuração de ICMS por CFOP+CST+alíquota
    apuracao: dict[tuple, dict] = {}

    for nota in notas:
        if nota.get("status") != "emitido":
            continue

        chave    = _s(nota.get("chave", ""))
        n_nfe    = str(nota.get("n_nfe", 0)).zfill(9)
        serie    = str(nota.get("serie", 1)).zfill(3)
        modelo   = str(nota.get("modelo", 55))
        nat_op   = _s(nota.get("nat_op", "Venda"), 60)
        ind_oper = "1"   # 1=saída
        ind_emit = "0"   # 0=emissão própria
        doc_dest = _so_num(nota.get("cnpj_dest","") or nota.get("cpf_dest",""))
        cod_part = doc_to_part.get(doc_dest, "")
        v_nf     = float(nota.get("v_nf", 0))
        v_desc   = float(nota.get("v_desc", 0))
        dh_emi   = nota.get("criado_em", "")

        # C100 — Nota fiscal
        w("C100",
          ind_oper,    # IND_OPER
          ind_emit,    # IND_EMIT
          cod_part,    # COD_PART
          modelo,      # COD_MOD
          "N",         # COD_SIT (N=normal)
          serie,       # SER
          n_nfe,       # NUM_DOC
          chave,       # CHV_NFE
          _d(dh_emi),  # DT_DOC
          _d(dh_emi),  # DT_E_S
          _n(v_nf),    # VL_DOC
          "0",         # IND_PGTO
          "",          # VL_DESC
          "0,00",      # VL_ABAT_NT
          _n(v_nf),    # VL_MERC
          "9",         # IND_FRT
          "0,00",      # VL_FRT
          "0,00",      # VL_SEG
          "0,00",      # VL_OUT_DA
          "0,00",      # VL_BC_ICMS
          "0,00",      # VL_ICMS
          "0,00",      # VL_BC_ICMS_ST
          "0,00",      # VL_ICMS_ST
          _n(v_nf),    # VL_IPI
          "0,00",      # VL_PIS
          "0,00",      # VL_COFINS
          "0,00",      # VL_PIS_ST
          "0,00",      # VL_COFINS_ST
        )

        itens = _itens(nota)
        for num, item in enumerate(itens, 1):
            cod   = _s(item.get("cProd") or item.get("codigo",""), 60)
            descr = _s(item.get("xProd",""), 60)
            ncm   = _so_num(item.get("NCM") or item.get("ncm","00000000"))[:8]
            cfop  = _s(item.get("CFOP") or item.get("cfop","5102"))
            unid  = _s(item.get("uCom") or item.get("unidade","UN"),6).upper()
            qtd   = float(item.get("qCom") or item.get("qtd", 1))
            vun   = float(item.get("vUnCom") or item.get("preco", 0))
            vprod = qtd * vun
            csosn = _s(item.get("CSOSN") or item.get("csosn","102"))
            cst   = "400" if crt == 1 else _s(item.get("CST_ICMS","00"))
            aliq  = float(item.get("pICMS", 0)) if crt != 1 else 0.0

            # C170 — Itens da nota
            w("C170",
              str(num),       # NUM_ITEM
              cod,            # COD_ITEM
              descr,          # DESCR_COMPL
              _n(qtd, 3),     # QTD
              unid,           # UNID
              _n(vun),        # VL_ITEM
              "0,00",         # VL_DESC
              ind_oper,       # IND_MOV
              cst,            # CST_ICMS
              cfop,           # CFOP
              "3",            # COD_NAT
              "0,00",         # VL_BC_ICMS
              "0,00",         # ALIQ_ICMS
              "0,00",         # VL_ICMS
              "0,00",         # VL_BC_ICMS_ST
              "0,00",         # ALIQ_ST
              "0,00",         # VL_ICMS_ST
              _n(vprod),      # VL_IPI
              "99",           # CST_IPI
              "0,00",         # VL_BC_IPI
              "0,00",         # ALIQ_IPI
              "0,00",         # VL_IPI2
              _s(item.get("CST_PIS","07")),   # CST_PIS
              "0,00",         # VL_BC_PIS
              "0,00",         # ALIQ_PIS_P
              "0,000",        # QUANT_BC_PIS
              "0,00",         # ALIQ_PIS_R
              "0,00",         # VL_PIS
              _s(item.get("CST_COFINS","07")),# CST_COFINS
              "0,00",         # VL_BC_COFINS
              "0,00",         # ALIQ_COFINS_P
              "0,000",        # QUANT_BC_COFINS
              "0,00",         # ALIQ_COFINS_R
              "0,00",         # VL_COFINS
              "",             # COD_CTA
              "0,00",         # VL_ABAT_NT
            )

            # Acumula apuração C190
            chave_ap = (cfop, cst, f"{aliq:.2f}")
            if chave_ap not in apuracao:
                apuracao[chave_ap] = {"vbc": 0.0, "vicms": 0.0, "vred": 0.0, "vprod": 0.0}
            apuracao[chave_ap]["vprod"] += vprod
            if aliq > 0:
                apuracao[chave_ap]["vbc"]   += vprod
                apuracao[chave_ap]["vicms"] += vprod * aliq / 100

        # C190 — Registro analítico do documento
        for (cfop, cst, aliq_s), v in apuracao.items():
            w("C190",
              cst,
              cfop,
              aliq_s.replace(".", ","),
              _n(v["vbc"]),
              _n(v["vicms"]),
              "0,00",
              "0,00",
              "0,00",
              "0,00",
              _n(v["vprod"]),
              "0,00",
              "0,00",
              "0,00",
              "0,00",
              "0,00",
            )
        apuracao.clear()

    w("C990", str(cnt.qtd_bloco("C") + 1))

    # ── BLOCO E — Apuração do ICMS ────────────────────────────────────────────
    w("E001", "0")
    w("E100", _d(dt_ini), _d(dt_fim))

    # Soma total de ICMS das notas emitidas
    total_vbc   = sum(float(n.get("v_nf", 0)) for n in notas if n.get("status") == "emitido")
    total_vicms = 0.0  # Simples/SN isento; Lucro Presumido calcula separado

    w("E110",
      _n(total_vbc),   # VL_TOT_DEBITOS
      "0,00",          # VL_AJ_DEBITOS
      "0,00",          # VL_TOT_AJ_DEBITOS
      "0,00",          # VL_ESTORNOS_CRED
      "0,00",          # VL_TOT_CREDITOS
      "0,00",          # VL_AJ_CREDITOS
      "0,00",          # VL_TOT_AJ_CREDITOS
      "0,00",          # VL_ESTORNOS_DEB
      "0,00",          # VL_SLD_CREDOR_ANT
      _n(total_vicms), # VL_SLD_APURADO
      "0,00",          # VL_TOT_DED
      _n(total_vicms), # VL_ICMS_RECOLHER
      "0,00",          # VL_SLD_CREDOR_TRANSPORTAR
      "0,00",          # DEB_ESP
    )

    w("E990", str(cnt.qtd_bloco("E") + 1))

    # ── BLOCO 9 — Controle e encerramento ────────────────────────────────────
    w("9001", "0")

    # 9900 — Registros do arquivo digital
    # Precisamos registrar quantidades por tipo de registro
    contagens: dict[str, int] = {}
    for linha in linhas:
        reg_id = linha.split("|")[1] if "|" in linha else ""
        if reg_id:
            contagens[reg_id] = contagens.get(reg_id, 0) + 1

    for reg_id in sorted(contagens):
        w("9900", reg_id, str(contagens[reg_id]))

    # 9990 e 9999 fecham o bloco e o arquivo
    qtd_9900 = cnt.qtd_bloco("9") + 1   # inclui o 9990 que vamos escrever
    w("9990", str(qtd_9900 + 1))
    total_final = cnt.total + 2          # +9999 e ele mesmo
    w("9999", str(total_final))

    return "".join(linhas).encode("utf-8")


# ── Utilitário interno ────────────────────────────────────────────────────────

def _itens(nota: dict) -> list[dict]:
    itens = nota.get("itens", [])
    if isinstance(itens, str):
        try:
            itens = json.loads(itens)
        except Exception:
            itens = []
    return itens if isinstance(itens, list) else []

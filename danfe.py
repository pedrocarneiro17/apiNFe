"""
DANFE simplificado — Documento Auxiliar da NF-e (modelo 55).
Gera PDF em memoria usando reportlab.
"""
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.barcode import code128

W, H = A4  # 210 x 297 mm


def _obter_xml_texto(nota: dict) -> str:
    """Texto do procNFe autorizado — sempre prioriza o Postgres (fonte
    durável); só cai pro arquivo em disco se por algum motivo o banco não
    tiver o conteúdo (nota emitida antes dessa coluna existir). Disco no
    Railway é efêmero (some a cada redeploy), então nunca deve ser a
    única fonte pra nada que precisa sobreviver além da própria request."""
    conteudo = nota.get("xml_conteudo")
    if conteudo:
        return conteudo
    caminho = nota.get("arquivo_xml", "")
    if caminho:
        try:
            import os
            if os.path.isfile(caminho):
                with open(caminho, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
    return ""


def _extrair_ibscbs(xml_texto: str) -> tuple[float, float]:
    """Soma vIBS e vCBS de cada item, lendo do procNFe assinado/autorizado —
    são os valores de teste do ano-teste 2026 da Reforma Tributária, não
    entram no valor total da nota."""
    if not xml_texto:
        return 0.0, 0.0
    try:
        import xml.etree.ElementTree as ET
        ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
        tree = ET.fromstring(xml_texto.encode("utf-8"))
        v_ibs = sum(float(el.text) for el in tree.findall(".//nfe:vIBS", ns))
        v_cbs = sum(float(el.text) for el in tree.findall(".//nfe:vCBS", ns))
        return v_ibs, v_cbs
    except Exception:
        return 0.0, 0.0


def _extrair_tpamb(xml_texto: str) -> str:
    """Lê o ambiente real da autorização (protNFe/infProt/tpAmb) do procNFe
    — fonte confiável de verdade, ao contrário de um campo "ambiente" que
    nunca existiu na tabela notas (por isso o DANFE sempre exibia o selo
    de homologação, mesmo em notas emitidas de verdade em produção)."""
    if not xml_texto:
        return "2"
    try:
        import xml.etree.ElementTree as ET
        ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
        tree = ET.fromstring(xml_texto.encode("utf-8"))
        tp = (tree.findtext(".//nfe:protNFe/nfe:infProt/nfe:tpAmb", namespaces=ns)
              or tree.findtext(".//nfe:infNFe/nfe:ide/nfe:tpAmb", namespaces=ns) or "2")
        return tp
    except Exception:
        return "2"


# ── helpers de estilo ─────────────────────────────────────────────

def _style(size=7, bold=False, align=TA_LEFT, color=colors.black):
    return ParagraphStyle(
        "s",
        fontSize=size,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        alignment=align,
        textColor=color,
        leading=size + 2,
        wordWrap="LTR",
    )


def _p(text, **kw):
    return Paragraph(str(text) if text is not None else "", _style(**kw))


def _fmt_cnpj(v):
    v = "".join(filter(str.isdigit, str(v or "")))
    if len(v) == 14:
        return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
    return v


def _fmt_cpf(v):
    v = "".join(filter(str.isdigit, str(v or "")))
    if len(v) == 11:
        return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"
    return v


def _fmt_cep(v):
    v = "".join(filter(str.isdigit, str(v or "")))
    if len(v) == 8:
        return f"{v[:5]}-{v[5:]}"
    return v


def _fmt_money(v):
    try:
        return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def _fmt_chave(chave):
    """Formata chave 44 digitos em grupos de 4."""
    chave = str(chave or "")
    return " ".join(chave[i:i+4] for i in range(0, len(chave), 4))


def _truncar(texto, fonte, tamanho, largura_max):
    """Corta o texto (com '…' no final) até caber em largura_max pontos,
    medindo a largura real da fonte — mais confiável que cortar por
    quantidade fixa de caracteres, que ora sobra ora estoura a coluna."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    texto = str(texto or "")
    if stringWidth(texto, fonte, tamanho) <= largura_max:
        return texto
    while texto and stringWidth(texto + "…", fonte, tamanho) > largura_max:
        texto = texto[:-1]
    return texto + "…" if texto else ""


# ── canvas helpers ────────────────────────────────────────────────

def _box(c, x, y, w, h, label="", value="", label_size=5, value_size=8):
    """Desenha caixa com label no topo e valor dentro."""
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.3)
    c.rect(x, y, w, h)
    if label:
        c.setFont("Helvetica", label_size)
        c.setFillColor(colors.black)
        c.drawString(x + 1*mm, y + h - label_size*0.4 - 1*mm, label.upper())
    if value:
        c.setFont("Helvetica-Bold", value_size)
        c.drawString(x + 1.5*mm, y + 2*mm, str(value))


def _hline(c, x, y, w):
    c.setLineWidth(0.3)
    c.line(x, y, x + w, y)


def _vline(c, x, y, h):
    c.setLineWidth(0.3)
    c.line(x, y, x, y + h)


# ── gerador principal ─────────────────────────────────────────────

def gerar_danfe(nota: dict) -> bytes:
    """
    Recebe dict com dados da nota (resultado de db.get_nota) e retorna
    bytes do PDF DANFE.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    xml_texto = _obter_xml_texto(nota)

    margem = 10 * mm
    larg   = W - 2 * margem   # largura util
    topo   = H - margem       # y inicial (topo da pagina)

    y = topo  # cursor vertical (decresce)

    # ── Cabecalho ────────────────────────────────────────────────
    cab_h = 32 * mm

    # Borda externa do cabecalho
    c.setLineWidth(0.5)
    c.rect(margem, y - cab_h, larg, cab_h)

    # Divisao vertical: emitente | DANFE central | numero NF-e
    col1 = larg * 0.40
    col2 = larg * 0.22
    col3 = larg * 0.38

    x1 = margem
    x2 = margem + col1
    x3 = x2 + col2

    _vline(c, x2, y - cab_h, cab_h)
    _vline(c, x3, y - cab_h, cab_h)

    # Emitente (esquerda) — MOC 7.0 Anexo II, item 3.7.6: razão social em
    # negrito mín. 12pt; demais dados do emitente em negrito mín. 8pt.
    largura_col1 = col1 - 4*mm  # 2mm de respiro em cada lado
    c.setFont("Helvetica-Bold", 12)
    nome_emit = _truncar(str(nota.get("razao_social", "")).upper(), "Helvetica-Bold", 12, largura_col1)
    c.drawString(x1 + 2*mm, y - 6*mm, nome_emit)
    c.setFont("Helvetica-Bold", 8)
    end_emit = (
        f"{nota.get('xLgr','')}, {nota.get('nro','')} "
        f"{nota.get('xBairro','')} - {nota.get('xMun','')}/{nota.get('uf','')}"
    )
    end_emit = _truncar(end_emit, "Helvetica-Bold", 8, largura_col1)
    c.drawString(x1 + 2*mm, y - 11*mm, end_emit)
    c.drawString(x1 + 2*mm, y - 15*mm, f"CNPJ: {_fmt_cnpj(nota.get('cnpj_emit',''))}")
    c.drawString(x1 + 2*mm, y - 19*mm, f"IE: {nota.get('ie','')}")
    c.drawString(x1 + 2*mm, y - 23*mm, f"Fone: {nota.get('fone','')}")

    # Centro — DANFE — MOC 7.0 Anexo II, item 3.7.4: "DANFE" em negrito
    # mín. 12pt; "Documento Auxiliar..." e tipo de operação em mín. 8pt.
    xc = x2
    wc = col2
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(xc + wc/2, y - 7*mm, "DANFE")
    c.setFont("Helvetica", 8)
    c.drawCentredString(xc + wc/2, y - 11*mm, "Documento Auxiliar da")
    c.drawCentredString(xc + wc/2, y - 14.5*mm,  "Nota Fiscal Eletrônica")

    tp_nf = "ENTRADA" if str(nota.get("tp_nf", "1")) == "0" else "SAÍDA"
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(xc + wc/2, y - 19*mm, f"Tipo: {tp_nf}")

    ambiente = _extrair_tpamb(xml_texto)
    if str(ambiente) == "2":
        c.setFillColor(colors.red)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(xc + wc/2, y - 24*mm, "SEM VALOR FISCAL")
        c.setFillColor(colors.black)

    # Direita — número e série — mín. 10pt negrito (3.7.4)
    xd = x3
    wd = col3
    n_nfe = str(nota.get("n_nfe", 0)).zfill(9)
    serie = str(nota.get("serie", 1)).zfill(3)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(xd + wd/2, y - 7*mm,  f"NF-e  Nº {n_nfe}")
    c.drawCentredString(xd + wd/2, y - 12*mm, f"Série  {serie}")
    c.setFont("Helvetica", 8)
    import datetime
    c.drawCentredString(xd + wd/2, y - 17*mm, f"Emissão: {datetime.datetime.now().strftime('%d/%m/%Y')}")
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(xd + wd/2, y - 22*mm, f"Folha 1/1")

    y -= cab_h

    # ── Chave de acesso ──────────────────────────────────────────
    chave_h = 10 * mm
    c.setLineWidth(0.3)
    c.rect(margem, y - chave_h, larg, chave_h)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem + 1*mm, y - 3*mm, "CHAVE DE ACESSO")
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(margem + larg/2, y - 7*mm, _fmt_chave(nota.get("chave", "")))

    y -= chave_h

    # ── Código de barras CODE-128C da chave de acesso ─────────────
    # MOC 7.0 Anexo II, item 2: CODE-128C, largura mínima 6cm (laser),
    # altura mínima 0,8cm.
    chave_num = "".join(c2 for c2 in str(nota.get("chave", "")) if c2.isdigit())
    if chave_num:
        barcode_h = 12 * mm
        c.setLineWidth(0.3)
        c.rect(margem, y - barcode_h, larg, barcode_h)
        try:
            bc = code128.Code128(chave_num, barWidth=0.5, barHeight=8*mm)
            bc.drawOn(c, margem + (larg - bc.width) / 2, y - barcode_h + 2*mm)
        except Exception:
            pass
        y -= barcode_h

    # ── Natureza da operacao / protocolo ────────────────────────
    nat_h = 9 * mm
    c.rect(margem, y - nat_h, larg * 0.60, nat_h)
    c.rect(margem + larg * 0.60, y - nat_h, larg * 0.40, nat_h)

    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem + 1*mm, y - 3*mm, "NATUREZA DA OPERAÇÃO")
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margem + 1*mm, y - 7*mm, str(nota.get("nat_op", "")))

    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem + larg*0.60 + 1*mm, y - 3*mm, "PROTOCOLO DE AUTORIZAÇÃO")
    c.setFont("Helvetica-Bold", 8)
    n_prot = str(nota.get("n_prot", ""))
    c.drawString(margem + larg*0.60 + 1*mm, y - 7*mm, n_prot)

    y -= nat_h

    # ── Emitente detalhes ────────────────────────────────────────
    emit_h = 9 * mm
    tercos = larg / 3

    for i, (label, val) in enumerate([
        ("CNPJ", _fmt_cnpj(nota.get("cnpj_emit", ""))),
        ("Insc. Estadual", nota.get("ie", "")),
        ("Regime Tributário", {1:"Simples Nacional",2:"Simples Nacional Exc.",3:"Regime Normal"}.get(int(nota.get("crt",1)),""))
    ]):
        c.rect(margem + i*tercos, y - emit_h, tercos, emit_h)
        c.setFont("Helvetica-Bold", 6)
        c.drawString(margem + i*tercos + 1*mm, y - 3*mm, label.upper())
        c.setFont("Helvetica-Bold", 7)
        c.drawString(margem + i*tercos + 1*mm, y - 7*mm, str(val))

    y -= emit_h

    # ── Destinatario ─────────────────────────────────────────────
    _secao(c, margem, y, larg, "DESTINATÁRIO / REMETENTE")
    y -= 5 * mm

    dest_doc = (_fmt_cnpj(nota.get("cnpj_dest","")) if nota.get("cnpj_dest")
                else _fmt_cpf(nota.get("cpf_dest","")))

    dest_h = 9 * mm
    # linha 1: nome | CNPJ/CPF | data emissao
    c.rect(margem, y - dest_h, larg*0.50, dest_h)
    c.rect(margem + larg*0.50, y - dest_h, larg*0.30, dest_h)
    c.rect(margem + larg*0.80, y - dest_h, larg*0.20, dest_h)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem+1*mm, y-3*mm, "NOME / RAZÃO SOCIAL")
    c.drawString(margem+larg*0.50+1*mm, y-3*mm, "CNPJ / CPF")
    c.drawString(margem+larg*0.80+1*mm, y-3*mm, "IE DESTINATÁRIO")
    c.setFont("Helvetica-Bold", 7)
    nome_dest = _truncar(nota.get("xNome_dest",""), "Helvetica-Bold", 7, larg*0.50 - 3*mm)
    c.drawString(margem+1*mm, y-7*mm, nome_dest)
    c.drawString(margem+larg*0.50+1*mm, y-7*mm, dest_doc)
    c.drawString(margem+larg*0.80+1*mm, y-7*mm, str(nota.get("ie_dest","")))
    y -= dest_h

    # linha 2: endereco | municipio | UF | CEP
    c.rect(margem, y-dest_h, larg*0.40, dest_h)
    c.rect(margem+larg*0.40, y-dest_h, larg*0.25, dest_h)
    c.rect(margem+larg*0.65, y-dest_h, larg*0.08, dest_h)
    c.rect(margem+larg*0.73, y-dest_h, larg*0.27, dest_h)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem+1*mm, y-3*mm, "ENDEREÇO")
    c.drawString(margem+larg*0.40+1*mm, y-3*mm, "MUNICÍPIO")
    c.drawString(margem+larg*0.65+1*mm, y-3*mm, "UF")
    c.drawString(margem+larg*0.73+1*mm, y-3*mm, "CEP")
    c.setFont("Helvetica-Bold", 7)
    end_dest = f"{nota.get('xLgr_dest','')}, {nota.get('nro_dest','')} {nota.get('xBairro_dest','')}"
    end_dest = _truncar(end_dest, "Helvetica-Bold", 7, larg*0.40 - 3*mm)
    c.drawString(margem+1*mm, y-7*mm, end_dest)
    c.drawString(margem+larg*0.40+1*mm, y-7*mm, str(nota.get("xMun_dest","")))
    c.drawString(margem+larg*0.65+1*mm, y-7*mm, str(nota.get("uf_dest","")))
    c.drawString(margem+larg*0.73+1*mm, y-7*mm, _fmt_cep(nota.get("cep_dest","")))
    y -= dest_h

    # ── Itens ────────────────────────────────────────────────────
    _secao(c, margem, y, larg, "DADOS DOS PRODUTOS / SERVIÇOS")
    y -= 5 * mm

    itens = nota.get("itens") or []
    if isinstance(itens, str):
        import json
        itens = json.loads(itens)

    # cabecalho da tabela de itens
    # MOC 7.0 Anexo II, item 3.1.7: colunas que não podem ser suprimidas —
    # Código, Descrição, NCM, CST, CFOP, Unidade, Quantidade, Valor Unitário,
    # Valor Total, Base de Cálculo do ICMS, Valor do ICMS e Alíquota do ICMS.
    col_w = [larg*p for p in [0.06, 0.28, 0.06, 0.05, 0.05, 0.05, 0.06, 0.08, 0.08, 0.09, 0.09, 0.05]]
    headers = ["CÓD.", "DESCRIÇÃO", "NCM", "CST/\nCSOSN", "CFOP", "UNID", "QTD",
               "VL UNIT", "VL TOTAL", "B.CÁLC.\nICMS", "VL\nICMS", "ALIQ\nICMS"]
    crt = int(nota.get("crt", 1))

    row_h = 5 * mm
    # cabecalho
    x_cur = margem
    for i, (h, w) in enumerate(zip(headers, col_w)):
        c.setLineWidth(0.3)
        c.rect(x_cur, y - row_h, w, row_h)
        c.setFont("Helvetica-Bold", 5)
        c.drawCentredString(x_cur + w/2, y - row_h + 1.2*mm, h.replace("\n", " "))
        x_cur += w
    y -= row_h

    # linhas de itens
    for item in itens[:20]:  # maximo 20 itens por pagina
        x_cur = margem
        qtd    = float(item.get("qCom", item.get("qtd", 1)) or 0)
        v_un   = float(item.get("vUnCom", item.get("preco", 0)) or 0)
        v_prod = float(item["vProd"]) if item.get("vProd") else qtd * v_un

        # Simples Nacional (CSOSN) não destaca ICMS próprio no DANFE — os
        # campos ficam zerados. Regime Normal (CST) calcula pela alíquota.
        if crt == 1 or str(item.get("CST_ICMS", "")) not in ("00",):
            v_bc_icms, v_icms, aliq_icms = 0.0, 0.0, 0.0
        else:
            v_bc_icms = v_prod
            aliq_icms = float(item.get("pICMS", 0) or 0)
            v_icms    = v_bc_icms * aliq_icms / 100

        vals = [
            item.get("cProd", ""),
            item.get("xProd", ""),
            item.get("NCM", item.get("ncm", "")),
            item.get("CSOSN", item.get("csosn", item.get("CST", ""))),
            item.get("CFOP", item.get("cfop", "")),
            item.get("uCom", item.get("unidade", "UN")),
            f"{qtd:,.2f}",
            f"{v_un:,.2f}",
            f"{v_prod:,.2f}",
            f"{v_bc_icms:,.2f}",
            f"{v_icms:,.2f}",
            f"{aliq_icms:,.2f}",
        ]
        for i, (v, w) in enumerate(zip(vals, col_w)):
            c.rect(x_cur, y - row_h, w, row_h)
            c.setFont("Helvetica", 6)
            align_right = i in (6, 7, 8, 9, 10, 11)
            if align_right:
                c.drawRightString(x_cur + w - 1*mm, y - row_h + 1.5*mm, str(v))
            else:
                txt = str(v)
                if i == 1 and len(txt) > 35:
                    txt = txt[:34] + "…"
                c.drawString(x_cur + 1*mm, y - row_h + 1.5*mm, txt)
            x_cur += w
        y -= row_h

    # ── Totais ───────────────────────────────────────────────────
    y -= 3*mm
    _secao(c, margem, y, larg, "CÁLCULO DO IMPOSTO")
    y -= 5*mm

    v_ibs, v_cbs = _extrair_ibscbs(xml_texto)

    tot_h = 9*mm
    tots = [
        ("BC ICMS", "R$ 0,00"),
        ("VL ICMS", "R$ 0,00"),
        ("BC ICMS ST", "R$ 0,00"),
        ("VL ICMS ST", "R$ 0,00"),
        ("VL IPI", "R$ 0,00"),
        ("VL PRODUTOS", _fmt_money(nota.get("v_nf", 0))),
        ("VL FRETE", _fmt_money(nota.get("v_frete", 0))),
        ("VL DESC.", _fmt_money(nota.get("v_desc", 0))),
        ("VL IBS*", _fmt_money(v_ibs)),
        ("VL CBS*", _fmt_money(v_cbs)),
        ("VL NF-e", _fmt_money(nota.get("v_nf", 0))),
    ]
    w_each = larg / len(tots)
    x_cur = margem
    for label, val in tots:
        c.rect(x_cur, y - tot_h, w_each, tot_h)
        c.setFont("Helvetica-Bold", 6)
        c.drawString(x_cur + 0.5*mm, y - 3*mm, label)
        c.setFont("Helvetica-Bold", 7)
        c.drawRightString(x_cur + w_each - 0.5*mm, y - 7*mm, val)
        x_cur += w_each
    y -= tot_h
    if v_ibs or v_cbs:
        c.setFont("Helvetica", 6)
        c.drawString(margem, y - 3*mm, "* IBS/CBS: valores de teste (ano-teste 2026), não somam no valor total da nota")
        y -= 4*mm

    # ── Transportadora ───────────────────────────────────────────
    _secao(c, margem, y, larg, "TRANSPORTADOR / VOLUMES TRANSPORTADOS")
    y -= 5*mm
    transp_h = 9*mm
    mod_frete = {
        "0": "0 - Por conta emitente (CIF)",
        "1": "1 - Por conta dest. (FOB)",
        "2": "2 - Por conta terceiros",
        "9": "9 - Sem frete",
    }.get(str(nota.get("mod_frete", "9")), "9 - Sem frete")
    c.rect(margem, y - transp_h, larg, transp_h)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem+1*mm, y-3*mm, "MODALIDADE DO FRETE")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(margem+1*mm, y-7*mm, mod_frete)
    y -= transp_h

    # ── Dados adicionais ─────────────────────────────────────────
    _secao(c, margem, y, larg, "DADOS ADICIONAIS")
    y -= 5*mm
    adic_h = max(15*mm, H - margem - (topo - y) - 10*mm)
    if adic_h < 10*mm:
        adic_h = 15*mm
    c.rect(margem, y - adic_h, larg*0.70, adic_h)
    c.rect(margem+larg*0.70, y - adic_h, larg*0.30, adic_h)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(margem+1*mm, y-3*mm, "INFORMAÇÕES COMPLEMENTARES")
    c.drawString(margem+larg*0.70+1*mm, y-3*mm, "RESERVADO AO FISCO")
    inf = str(nota.get("inf_adic", ""))
    c.setFont("Helvetica", 6)
    # quebra texto manual
    words = inf.split()
    line, lines = "", []
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, "Helvetica", 6) < (larg*0.70 - 3*mm):
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    for i, l in enumerate(lines[:8]):
        c.drawString(margem+1*mm, y - 6*mm - i*4*mm, l)

    # rodape com ambiente
    if _extrair_tpamb(xml_texto) == "2":
        c.setFillColor(colors.red)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(W/2, margem/2, "AMBIENTE DE HOMOLOGAÇÃO — SEM VALOR FISCAL")
        c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buf.getvalue()


def _secao(c, x, y, w, titulo):
    """Faixa de secao (barra cinza com titulo)."""
    c.setFillColor(colors.lightgrey)
    c.rect(x, y - 5*mm, w, 5*mm, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(x + 2*mm, y - 3.5*mm, titulo.upper())

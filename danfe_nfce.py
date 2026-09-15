"""
Cupom NFC-e — Documento Auxiliar da NFC-e (modelo 65).
Layout thermal 80mm gerado em PDF com reportlab.
"""
from io import BytesIO
import json
from urllib.parse import urlparse
import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader

CUPOM_W = 80 * mm   # largura 80mm
MARGIN  = 4 * mm
INNER   = CUPOM_W - 2 * MARGIN


def _fmt_cnpj(v):
    v = "".join(c for c in str(v or "") if c.isdigit())
    if len(v) == 14:
        return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
    return v

def _fmt_cpf(v):
    v = "".join(c for c in str(v or "") if c.isdigit())
    if len(v) == 11:
        return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"
    return v

def _money(v):
    try:
        return f"R$ {float(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
    except Exception:
        return "R$ 0,00"

def _chave_fmt(c):
    c = str(c or "")
    return " ".join(c[i:i+4] for i in range(0, len(c), 4))


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


def _extrair_dhrecbto(xml_texto: str) -> str:
    """Lê a data/hora real de autorização (protNFe/infProt/dhRecbto) do
    procNFe — já vem em horário local (-03:00) na resposta da SEFAZ, não
    precisa converter. Antes o cupom imprimia a hora de GERAÇÃO DO PDF
    (datetime.now()), o que é incorreto — o manual exige a data/hora real
    de autorização."""
    if not xml_texto:
        return ""
    try:
        import xml.etree.ElementTree as ET
        ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
        tree = ET.fromstring(xml_texto.encode("utf-8"))
        dh = tree.findtext(".//nfe:infProt/nfe:dhRecbto", namespaces=ns) or ""
        if not dh:
            return ""
        # "2026-09-11T14:27:52-03:00" -> "11/09/2026 14:27:52"
        data, hora = dh[:19].split("T")
        ano, mes, dia = data.split("-")
        return f"{dia}/{mes}/{ano} {hora}"
    except Exception:
        return ""


def _extrair_tpamb(xml_texto: str) -> str:
    """Lê o ambiente real da autorização (protNFe/infProt/tpAmb) do procNFe
    — fonte confiável de verdade, ao contrário de um campo "ambiente" que
    nunca existiu na tabela notas (por isso o cupom sempre exibia o selo
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


def _extrair_urlchave(xml_texto: str) -> str:
    """Lê a URL de consulta por chave (infNFeSupl/urlChave) do procNFe."""
    if not xml_texto:
        return ""
    try:
        import xml.etree.ElementTree as ET
        ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
        tree = ET.fromstring(xml_texto.encode("utf-8"))
        return tree.findtext(".//nfe:urlChave", namespaces=ns) or ""
    except Exception:
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


class _NullCanvas:
    """Substituto do canvas do reportlab que não desenha nada — usado no passo
    de medição pra descobrir a altura real do cupom antes de criar a página
    de verdade (sem duplicar a lógica de layout em dois lugares)."""
    def __getattr__(self, _nome):
        return lambda *a, **k: None


def _desenhar_cupom(c, nota: dict, itens: list, y_start: float) -> float:
    """Desenha (ou só mede, se `c` for um _NullCanvas) o cupom inteiro a
    partir de y_start. Retorna o y final (mais baixo ponto alcançado)."""
    y = y_start
    xml_texto = _obter_xml_texto(nota)

    def linha(texto, size=7, bold=False, center=False, right=False, color=colors.black):
        nonlocal y
        c.setFillColor(color)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        x = MARGIN
        if center:
            c.drawCentredString(CUPOM_W/2, y, texto)
        elif right:
            c.drawRightString(CUPOM_W - MARGIN, y, texto)
        else:
            c.drawString(x, y, texto)
        y -= (size + 2) * mm / (72/25.4) * 0.45 + 1.2*mm

    def divisor(tracejado=False):
        nonlocal y
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.3)
        if tracejado:
            c.setDash(2, 2)
        c.line(MARGIN, y, CUPOM_W - MARGIN, y)
        c.setDash()
        y -= 2*mm

    # ── Divisão I — Cabeçalho (Manual DANFE NFC-e, item 3.1.1) ─────
    # Campos obrigatórios: CNPJ, Razão Social, endereço completo (sem país)
    # e o texto padrão "Documento Auxiliar da Nota Fiscal de Consumidor
    # Eletrônica" — não é opcional, é o texto exigido, não "NFC-e — Nota
    # Fiscal de Consumidor" como estava antes.
    linha(str(nota.get("razao_social","")).upper(), size=8, bold=True, center=True)
    cnpj_emit = _fmt_cnpj(nota.get("cnpj_emit",""))
    linha(f"CNPJ: {cnpj_emit}", size=6, center=True)
    end = f"{nota.get('xLgr','')} {nota.get('nro','')} - {nota.get('xMun','')}/{nota.get('uf','')}"
    linha(end[:60], size=6, center=True)
    linha(f"IE: {nota.get('ie','')}", size=6, center=True)
    y -= 1*mm
    linha("DOCUMENTO AUXILIAR DA NOTA FISCAL", size=7, bold=True, center=True)
    linha("DE CONSUMIDOR ELETRÔNICA", size=7, bold=True, center=True)

    # Divisão VIII — em homologação o texto abaixo do cabeçalho é exigido
    # literalmente (não é livre, tem que ser exatamente este):
    if _extrair_tpamb(xml_texto) == "2":
        linha("EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO", size=6.5, bold=True, center=True, color=colors.red)
        linha("SEM VALOR FISCAL", size=6.5, bold=True, center=True, color=colors.red)
    divisor()

    # ── Divisão II — itens (item 3.1.2 do manual) ──────────────────
    # Campos obrigatórios: Código, Descrição, Qtde, Um (unidade), Valor
    # unit., Valor total — faltavam Código e Unidade na versão anterior.
    linha("ITEM CÓD.  DESCRIÇÃO           QTD UN  VL.UNIT   TOTAL", size=5.5, bold=True)
    divisor(tracejado=True)

    for i, item in enumerate(itens, 1):
        cod   = str(item.get("cProd", item.get("codigo", "")))[:12]
        desc  = str(item.get("xProd",""))[:26]
        unid  = str(item.get("uCom", item.get("unidade", "UN")))[:6]
        qtd   = float(item.get("qCom", item.get("qtd", 1)))
        v_un  = float(item.get("vUnCom", item.get("preco", 0)))
        v_tot = qtd * v_un
        cfop  = item.get("CFOP", item.get("cfop",""))
        linha(f"{i:02d}  {cod}  {desc}", size=6.5)
        y += 0.5*mm
        # linha de valores (qtd unid x vUnit = total)
        val_str = f"{qtd:.2f} {unid} x {v_un:.2f} = {v_tot:.2f}"
        c.setFont("Helvetica", 6.5)
        c.drawRightString(CUPOM_W - MARGIN, y, val_str)
        if cfop:
            c.setFont("Helvetica", 5)
            c.setFillColor(colors.grey)
            c.drawString(MARGIN, y, f"CFOP:{cfop}")
            c.setFillColor(colors.black)
        y -= 3*mm

    divisor()

    # ── Divisão III — totais (item 3.1.3 do manual) ────────────────
    v_nf    = float(nota.get("v_nf", 0))
    v_desc  = float(nota.get("v_desc", 0))
    v_troco = float(nota.get("v_troco", 0))  # ainda não coletado na tela

    def linha_total(label, valor, bold=False):
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        sz = 7.5 if bold else 7
        c.setFont(font, sz)
        c.drawString(MARGIN, y, label)
        c.drawRightString(CUPOM_W - MARGIN, y, valor)
        y -= 3.5*mm

    linha_total("Qtde. Total de Itens:", str(len(itens)))
    if v_desc > 0:
        linha_total("Subtotal:", _money(v_nf + v_desc))
        linha_total("Desconto:", f"- {_money(v_desc)}")
    linha_total("Valor a Pagar R$:" if v_desc > 0 else "TOTAL:", _money(v_nf), bold=True)

    # IBS/CBS (ano-teste 2026) — só informativo, não soma no TOTAL acima
    v_ibs, v_cbs = _extrair_ibscbs(xml_texto)
    if v_ibs or v_cbs:
        linha_total("Val. aprox. IBS (teste):", _money(v_ibs))
        linha_total("Val. aprox. CBS (teste):", _money(v_cbs))

    # Pagamento
    tp_map = {"01":"Dinheiro","02":"Cheque","03":"Cartão Crédito","04":"Cartão Débito",
              "05":"Crédito Loja","10":"Vale Alimentação","11":"Vale Refeição",
              "15":"Boleto","99":"Outros"}
    tp_pag = str(nota.get("tp_pag","01"))
    linha_total(f"{tp_map.get(tp_pag, tp_pag).upper()}:", _money(v_nf))
    linha_total("Troco:", _money(v_troco))  # obrigatório desde NT 2016.002
    divisor()

    # ── Divisão VI — Consumidor (item 3.1.6 do manual) ─────────────
    # Sempre precisa aparecer alguma coisa aqui: identificado (CNPJ/CPF em
    # caixa alta) ou "CONSUMIDOR NÃO IDENTIFICADO" — antes, se não tivesse
    # CPF/CNPJ, essa divisão inteira sumia, o que não é permitido.
    cpf_dest  = nota.get("cpf_dest","")
    cnpj_dest = nota.get("cnpj_dest","")
    xnome     = nota.get("xNome_dest","")
    if cpf_dest:
        linha(f"CONSUMIDOR CPF: {_fmt_cpf(cpf_dest)}", size=6, bold=True)
        if xnome and xnome.upper() not in ("CONSUMIDOR NAO IDENTIFICADO",""):
            linha(xnome[:40], size=6)
    elif cnpj_dest:
        linha(f"CONSUMIDOR CNPJ: {_fmt_cnpj(cnpj_dest)}", size=6, bold=True)
        if xnome and xnome.upper() not in ("CONSUMIDOR NAO IDENTIFICADO",""):
            linha(xnome[:40], size=6)
    else:
        linha("CONSUMIDOR NÃO IDENTIFICADO", size=6, bold=True, center=True)
    divisor(tracejado=True)

    # ── Divisão IV/VII — Chave de acesso e Protocolo ───────────────
    # Chave deve sair em 11 blocos de 4 dígitos (item 3.1.4) — o código
    # antigo quebrava errado, em blocos de 11 dígitos.
    chave = str(nota.get("chave",""))
    url_chave = _extrair_urlchave(xml_texto)
    linha("CONSULTE PELA CHAVE DE ACESSO EM:", size=5.5, bold=True, center=True)
    if url_chave:
        linha(url_chave, size=5.5, center=True)
    grupos = _chave_fmt(chave).split(" ")
    linha(" ".join(grupos[:6]), size=6, center=True)
    linha(" ".join(grupos[6:]), size=6, center=True)

    n_prot = nota.get("n_prot","")
    if n_prot:
        y -= 1*mm
        dh_recbto = _extrair_dhrecbto(xml_texto)
        linha(f"Protocolo de autorização: {n_prot}", size=6, center=True)
        if dh_recbto:
            linha(dh_recbto, size=6, center=True)
    divisor()

    # ── QR Code ───────────────────────────────────────────────────
    qr_url = ""
    # tenta extrair do XML salvo ou usa placeholder
    if xml_texto:
        try:
            import xml.etree.ElementTree as ET
            tree = ET.fromstring(xml_texto.encode("utf-8"))
            ns = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
            el = tree.find(".//nfe:qrCode", ns)
            if el is not None and el.text:
                qr_url = el.text
        except Exception:
            pass
    if not qr_url:
        qr_url = f"https://www.nfce.fazenda.sp.gov.br/consulta?p={chave}"

    try:
        qr = qrcode.QRCode(version=1, box_size=3, border=1)
        qr.add_data(qr_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_buf = BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)

        qr_size = 40*mm
        qr_x = (CUPOM_W - qr_size) / 2
        y -= 2*mm
        c.drawImage(ImageReader(qr_buf), qr_x, y - qr_size, width=qr_size, height=qr_size)
        y -= qr_size + 2*mm
        linha("Consulte pela chave ou QR Code", size=6, center=True)
        dominio = urlparse(qr_url).netloc or qr_url
        linha(dominio, size=5.5, center=True)
    except Exception:
        linha("[QR Code indisponível — instale: pip install qrcode pillow]", size=6, center=True)

    divisor()
    linha(f"Nº {str(nota.get('n_nfe',0)).zfill(9)}  Série {str(nota.get('serie',1)).zfill(3)}", size=6, center=True)
    linha("Obrigado pela preferência!", size=7, bold=True, center=True)

    return y


def gerar_cupom(nota: dict) -> bytes:
    itens = nota.get("itens") or []
    if isinstance(itens, str):
        itens = json.loads(itens)

    # Passo 1 — mede a altura real do conteúdo desenhando num canvas "nulo"
    # (mesma função de desenho, sem duplicar a lógica de layout).
    y_fim = _desenhar_cupom(_NullCanvas(), nota, itens, y_start=0)
    altura_total = -y_fim + 6*mm  # + margem inferior

    # Passo 2 — desenha de verdade, já com a altura certa (sem sobra de página).
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(CUPOM_W, altura_total))
    _desenhar_cupom(c, nota, itens, y_start=altura_total - 4*mm)

    c.showPage()
    c.save()
    return buf.getvalue()

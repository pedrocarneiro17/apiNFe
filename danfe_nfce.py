"""
Cupom NFC-e — Documento Auxiliar da NFC-e (modelo 65).
Layout thermal 80mm gerado em PDF com reportlab.
"""
from io import BytesIO
import json
import datetime
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


def gerar_cupom(nota: dict) -> bytes:
    itens = nota.get("itens") or []
    if isinstance(itens, str):
        itens = json.loads(itens)

    # Estima altura do cupom dinamicamente
    n_itens = max(len(itens), 1)
    altura_est = (
        60            # header
        + n_itens * 14
        + 10          # separadores
        + 50          # totais
        + 30          # destinatario
        + 40          # chave + protocolo
        + 60          # qr code
        + 20          # rodape
    ) * mm

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(CUPOM_W, altura_est))

    y = altura_est - 4*mm   # cursor, desce

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

    # ── Header ────────────────────────────────────────────────────
    linha(str(nota.get("razao_social","")).upper(), size=8, bold=True, center=True)
    cnpj_emit = _fmt_cnpj(nota.get("cnpj_emit",""))
    linha(f"CNPJ: {cnpj_emit}", size=6, center=True)
    end = f"{nota.get('xLgr','')} {nota.get('nro','')} - {nota.get('xMun','')}/{nota.get('uf','')}"
    linha(end[:45], size=6, center=True)
    linha(f"IE: {nota.get('ie','')}", size=6, center=True)
    y -= 1*mm
    divisor()

    # ── Titulo ────────────────────────────────────────────────────
    linha("NFC-e — NOTA FISCAL DE CONSUMIDOR", size=7, bold=True, center=True)
    linha("ELETRÔNICA", size=7, bold=True, center=True)
    if str(nota.get("ambiente","2")) == "2":
        linha("*** SEM VALOR FISCAL ***", size=7, bold=True, center=True, color=colors.red)
    divisor()

    # ── Itens ─────────────────────────────────────────────────────
    linha("ITEM  DESCRIÇÃO              QTD    VL.UNIT    TOTAL", size=5.5, bold=True)
    divisor(tracejado=True)

    for i, item in enumerate(itens, 1):
        desc  = str(item.get("xProd",""))[:22]
        qtd   = float(item.get("qCom", item.get("qtd", 1)))
        v_un  = float(item.get("vUnCom", item.get("preco", 0)))
        v_tot = qtd * v_un
        cfop  = item.get("CFOP", item.get("cfop",""))
        linha(f"{i:02d}  {desc}", size=6.5)
        y += 0.5*mm
        # linha de valores (qdt x vUnit = total)
        val_str = f"{qtd:.2f} x {v_un:.2f} = {v_tot:.2f}"
        c.setFont("Helvetica", 6.5)
        c.drawRightString(CUPOM_W - MARGIN, y, val_str)
        if cfop:
            c.setFont("Helvetica", 5)
            c.setFillColor(colors.grey)
            c.drawString(MARGIN, y, f"CFOP:{cfop}")
            c.setFillColor(colors.black)
        y -= 3*mm

    divisor()

    # ── Totais ────────────────────────────────────────────────────
    v_nf    = float(nota.get("v_nf", 0))
    v_desc  = float(nota.get("v_desc", 0))

    def linha_total(label, valor, bold=False):
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        sz = 7.5 if bold else 7
        c.setFont(font, sz)
        c.drawString(MARGIN, y, label)
        c.drawRightString(CUPOM_W - MARGIN, y, valor)
        y -= 3.5*mm

    if v_desc > 0:
        linha_total("Subtotal:", _money(v_nf + v_desc))
        linha_total("Desconto:", f"- {_money(v_desc)}")
    linha_total("TOTAL:", _money(v_nf), bold=True)

    # Pagamento
    tp_map = {"01":"Dinheiro","02":"Cheque","03":"Cartão Crédito","04":"Cartão Débito",
              "05":"Crédito Loja","10":"Vale Alimentação","11":"Vale Refeição",
              "15":"Boleto","99":"Outros"}
    tp_pag = str(nota.get("tp_pag","01"))
    linha_total(f"Pagamento ({tp_map.get(tp_pag, tp_pag)}):", _money(v_nf))
    divisor()

    # ── Destinatário (opcional) ───────────────────────────────────
    cpf_dest  = nota.get("cpf_dest","")
    cnpj_dest = nota.get("cnpj_dest","")
    xnome     = nota.get("xNome_dest","")
    if cpf_dest or cnpj_dest:
        linha("CONSUMIDOR", size=6, bold=True)
        if xnome and xnome.upper() not in ("CONSUMIDOR NAO IDENTIFICADO",""):
            linha(xnome[:40], size=6)
        if cpf_dest:
            linha(f"CPF: {_fmt_cpf(cpf_dest)}", size=6)
        elif cnpj_dest:
            linha(f"CNPJ: {_fmt_cnpj(cnpj_dest)}", size=6)
        divisor(tracejado=True)

    # ── Chave / Protocolo ─────────────────────────────────────────
    chave = str(nota.get("chave",""))
    linha("CHAVE DE ACESSO", size=5.5, bold=True, center=True)
    # quebra em 4 linhas de 11 digitos
    chunks = [chave[i:i+11] for i in range(0, len(chave), 11)]
    for ch in chunks:
        linha(ch, size=6, center=True)

    n_prot = nota.get("n_prot","")
    if n_prot:
        y -= 1*mm
        linha(f"Protocolo: {n_prot}", size=6, center=True)
        linha(f"Data/Hora: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", size=6, center=True)
    divisor()

    # ── QR Code ───────────────────────────────────────────────────
    qr_url = ""
    # tenta extrair do XML salvo ou usa placeholder
    xml_path = nota.get("arquivo_xml","")
    if xml_path:
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
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
        linha("www.nfce.fazenda.sp.gov.br/consulta", size=5.5, center=True)
    except Exception:
        linha("[QR Code indisponível — instale: pip install qrcode pillow]", size=6, center=True)

    divisor()
    linha(f"Nº {str(nota.get('n_nfe',0)).zfill(9)}  Série {str(nota.get('serie',1)).zfill(3)}", size=6, center=True)
    linha("Obrigado pela preferência!", size=7, bold=True, center=True)

    c.showPage()
    c.save()
    return buf.getvalue()

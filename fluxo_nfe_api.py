"""
Emissão de NF-e (mod 55) e NFC-e (mod 65) via webservices SOAP da SEFAZ — Leiaute 4.00.

Fluxo suportado:
  - Consulta de status do serviço (NFeStatusServico4)
  - Autorização síncrona de lote com 1 NF-e (NFeAutorizacao4, indSinc=1)
  - Autorização síncrona de lote com 1 NFC-e (NFeAutorizacao4 no endpoint NFC-e)
  - Cancelamento via evento (NFeRecepcaoEvento4, tpEvento=110111)
  - Consulta de protocolo (NFeConsultaProtocolo4)

Diferenças fundamentais vs NFS-e:
  - Protocolo: SOAP 1.2 com mTLS (não REST JSON)
  - Assinatura: RSA-SHA1 (MOC 7.0 exige — apesar de SHA-1 ser legado)
  - Namespace: http://www.portalfiscal.inf.br/nfe
  - Chave de acesso: 44 dígitos gerados pelo emitente antes de assinar

NFC-e (modelo 65) — diferenças adicionais:
  - Endpoints próprios por UF (separados dos de NF-e)
  - infNFeSupl com qrCode (hash SHA-256 com CSC do emitente)
  - Destinatário opcional (consumidor não identificado)
  - indFinal=1 e indPres obrigatórios
  - CSC (Código de Segurança do Contribuinte) obtido na SEFAZ do estado

Dependências:
    pip install requests lxml signxml cryptography
"""

import os
import re
import random
import hashlib
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, NoEncryption, pkcs12,
)
from lxml import etree
from signxml import XMLSigner, methods
import requests


# ─── Namespace ────────────────────────────────────────────────────
NS  = "http://www.portalfiscal.inf.br/nfe"
WSDL_BASE = "http://www.portalfiscal.inf.br/nfe/wsdl"

# ─── Roteamento por UF — NF-e modelo 55 ──────────────────────────
# Fonte: wsnfe_4.00_mod55.xml (nfephp-org/sped-nfe)
_AUTORIZADORES: dict[str, dict[str, str]] = {
    # UF: {prod: url_base, homo: url_base}
    "AM": {"prod": "https://nfe.sefaz.am.gov.br/services2/services/",
           "homo": "https://homnfe.sefaz.am.gov.br/services2/services/"},
    "BA": {"prod": "https://nfe.sefaz.ba.gov.br/webservices/",
           "homo": "https://hnfe.sefaz.ba.gov.br/webservices/"},
    "GO": {"prod": "https://nfe.sefaz.go.gov.br/nfe/services/",
           "homo": "https://homolog.sefaz.go.gov.br/nfe/services/"},
    "MG": {"prod": "https://nfe.fazenda.mg.gov.br/nfe2/services/",
           "homo": "https://hnfe.fazenda.mg.gov.br/nfe2/services/"},
    "MS": {"prod": "https://nfe.fazenda.ms.gov.br/ws/",
           "homo": "https://hom.nfe.fazenda.ms.gov.br/ws/"},
    "MT": {"prod": "https://nfe.sefaz.mt.gov.br/nfews/v2/services/",
           "homo": "https://homologacao.sefaz.mt.gov.br/nfews/v2/services/"},
    "PE": {"prod": "https://nfe.sefaz.pe.gov.br/nfe-service/services/",
           "homo": "https://nfehomolog.sefaz.pe.gov.br/nfe-service/services/"},
    "PR": {"prod": "https://nfe.fazenda.pr.gov.br/nfe/services/",
           "homo": "https://homologacao.nfe.fazenda.pr.gov.br/nfe/services/"},
    "RS": {"prod": "https://nfe.sefazrs.rs.gov.br/ws/",
           "homo": "https://nfe-homologacao.sefazrs.rs.gov.br/ws/"},
    "SP": {"prod": "https://nfe.fazenda.sp.gov.br/ws/",
           "homo": "https://homologacao.nfe.fazenda.sp.gov.br/ws/"},
    # SVRS: demais estados sem autorizador próprio
    "SVRS": {"prod": "https://nfe.svrs.rs.gov.br/ws/",
             "homo": "https://nfe-homologacao.svrs.rs.gov.br/ws/"},
    # SVAN: Maranhão
    "SVAN": {"prod": "https://www.sefazvirtual.fazenda.gov.br/",
             "homo": "https://hom.sefazvirtual.fazenda.gov.br/"},
    # SVC (contingência)
    "SVC-AN": {"prod": "https://www.sefazvirtual.fazenda.gov.br/",
               "homo": "https://hom.sefazvirtual.fazenda.gov.br/"},
    "SVC-RS": {"prod": "https://nfe.svrs.rs.gov.br/ws/",
               "homo": "https://nfe-homologacao.svrs.rs.gov.br/ws/"},
    # AN: eventos nacionais (cancelamento, manifestação do destinatário)
    "AN": {"prod": "https://www.nfe.fazenda.gov.br/",
           "homo": "https://hom.nfe.fazenda.gov.br/"},
}

# ─── Roteamento por UF — NFC-e modelo 65 ─────────────────────────
# Estados com autorizador próprio; demais usam SVRSN (SVRS para NFC-e)
_AUTORIZADORES_NFCE: dict[str, dict[str, str]] = {
    "MG":    {"prod": "https://nfce.fazenda.mg.gov.br/portalnfce/system/webservices/ws/",
              "homo": "https://hnfce.fazenda.mg.gov.br/portalnfce/system/webservices/ws/"},
    "MS":    {"prod": "https://nfce.fazenda.ms.gov.br/ws/",
              "homo": "https://hom.nfce.fazenda.ms.gov.br/ws/"},
    "MT":    {"prod": "https://nfce.sefaz.mt.gov.br/nfcews/v2/services/",
              "homo": "https://homologacao.sefaz.mt.gov.br/nfcews/v2/services/"},
    "PR":    {"prod": "https://nfce.fazenda.pr.gov.br/nfe/services/",
              "homo": "https://homologacao.nfce.fazenda.pr.gov.br/nfe/services/"},
    "RS":    {"prod": "https://nfce.svrs.rs.gov.br/ws/",
              "homo": "https://nfce-homologacao.svrs.rs.gov.br/ws/"},
    "SP":    {"prod": "https://nfce.fazenda.sp.gov.br/ws/",
              "homo": "https://homologacao.nfce.fazenda.sp.gov.br/ws/"},
    # SVRSN: demais estados
    "SVRSN": {"prod": "https://nfce.svrs.rs.gov.br/ws/",
              "homo": "https://nfce-homologacao.svrs.rs.gov.br/ws/"},
}

_UF_AUTORIZADOR_NFCE: dict[str, str] = {
    "AC": "SVRSN", "AL": "SVRSN", "AM": "SVRSN", "AP": "SVRSN",
    "BA": "SVRSN", "CE": "SVRSN", "DF": "SVRSN", "ES": "SVRSN",
    "GO": "SVRSN", "MA": "SVRSN", "MG": "MG",    "MS": "MS",
    "MT": "MT",    "PA": "SVRSN", "PB": "SVRSN", "PE": "SVRSN",
    "PI": "SVRSN", "PR": "PR",    "RJ": "SVRSN", "RN": "SVRSN",
    "RO": "SVRSN", "RR": "SVRSN", "RS": "RS",    "SC": "SVRSN",
    "SE": "SVRSN", "SP": "SP",    "TO": "SVRSN",
}

# URL do portal de consulta NFC-e por UF (para o QR Code)
_URL_CONSULTA_NFCE: dict[str, dict[str, str]] = {
    "MG": {"prod": "https://portalsped.fazenda.mg.gov.br/portalnfce/system/pages/consultarNota/index.xhtml",
           "homo": "https://portalhomolog.fazenda.mg.gov.br/portalnfce/system/pages/consultarNota/index.xhtml"},
    "MS": {"prod": "https://www.dfe.ms.gov.br/nfce/qrcode",
           "homo": "https://www.dfe.ms.gov.br/nfce/qrcode"},
    "MT": {"prod": "https://www.sefaz.mt.gov.br/nfce/consultanfce",
           "homo": "https://homologacao.sefaz.mt.gov.br/nfce/consultanfce"},
    "PR": {"prod": "https://www.fazenda.pr.gov.br/nfce/qrcode",
           "homo": "https://www.fazenda.pr.gov.br/nfce/qrcode"},
    "RS": {"prod": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM-OCORRENCIAS.aspx",
           "homo": "https://www.sefaz.rs.gov.br/NFCE/NFCE-COM-OCORRENCIAS.aspx"},
    "SP": {"prod": "https://www.nfce.fazenda.sp.gov.br/consulta",
           "homo": "https://www.homologacao.nfce.fazenda.sp.gov.br/consulta"},
}
_URL_CONSULTA_NFCE_PADRAO = {
    "prod": "https://www.nfce.fazenda.gov.br/consulta",
    "homo": "https://www.nfce.fazenda.gov.br/consulta",
}

# UF → autorizador de emissão normal
_UF_AUTORIZADOR: dict[str, str] = {
    "AC": "SVRS", "AL": "SVRS", "AM": "AM",  "AP": "SVRS",
    "BA": "BA",   "CE": "SVRS", "DF": "SVRS", "ES": "SVRS",
    "GO": "GO",   "MA": "SVAN", "MG": "MG",  "MS": "MS",
    "MT": "MT",   "PA": "SVRS", "PB": "SVRS", "PE": "PE",
    "PI": "SVRS", "PR": "PR",   "RJ": "SVRS", "RN": "SVRS",
    "RO": "SVRS", "RR": "SVRS", "RS": "RS",   "SC": "SVRS",
    "SE": "SVRS", "SP": "SP",   "TO": "SVRS",
}

# UF → código IBGE
_UF_IBGE: dict[str, int] = {
    "AC": 12, "AL": 27, "AM": 13, "AP": 16, "BA": 29,
    "CE": 23, "DF": 53, "ES": 32, "GO": 52, "MA": 21,
    "MG": 31, "MS": 50, "MT": 51, "PA": 15, "PB": 25,
    "PE": 26, "PI": 22, "PR": 41, "RJ": 33, "RN": 24,
    "RO": 11, "RR": 14, "RS": 43, "SC": 42, "SE": 28,
    "SP": 35, "TO": 17,
}


# ─── Utilitários ──────────────────────────────────────────────────

def _so_numeros(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _tp_amb() -> str:
    return os.environ.get("NFE_AMBIENTE", "2")  # padrão: homologação


def _is_prod() -> bool:
    return _tp_amb() == "1"


def _url_servico(uf: str, servico: str) -> str:
    """URL do serviço NF-e (mod 55) para a UF."""
    autorizador = _UF_AUTORIZADOR.get(uf.upper(), "SVRS")
    env = "prod" if _is_prod() else "homo"
    base = _AUTORIZADORES[autorizador][env]
    return f"{base}{servico}"


def _url_servico_nfce(uf: str, servico: str) -> str:
    """URL do serviço NFC-e (mod 65) para a UF."""
    autorizador = _UF_AUTORIZADOR_NFCE.get(uf.upper(), "SVRSN")
    env = "prod" if _is_prod() else "homo"
    base = _AUTORIZADORES_NFCE[autorizador][env]
    return f"{base}{servico}"


def _url_consulta_nfce(uf: str) -> str:
    """URL do portal de consulta NFC-e (usada no QR Code)."""
    env = "prod" if _is_prod() else "homo"
    return _URL_CONSULTA_NFCE.get(uf.upper(), _URL_CONSULTA_NFCE_PADRAO)[env]


def _pfx_para_pem(caminho_pfx: str, senha: str):
    """Carrega PFX e escreve cert.pem + key.pem em diretório temporário."""
    with open(caminho_pfx, "rb") as f:
        pfx_data = f.read()
    chave, cert, _ = pkcs12.load_key_and_certificates(
        pfx_data, senha.encode("utf-8") if isinstance(senha, str) else senha
    )
    tmp_dir   = tempfile.mkdtemp()
    cert_path = os.path.join(tmp_dir, "cert.pem")
    key_path  = os.path.join(tmp_dir, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return cert_path, key_path, tmp_dir, chave, cert


# ─── Chave de Acesso ──────────────────────────────────────────────

def _calcular_dv(chave43: str) -> int:
    pesos = [2,3,4,5,6,7,8,9] * 6  # 48 pesos, usa os 43 primeiros
    soma  = sum(int(d) * p for d, p in zip(reversed(chave43), pesos))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def gerar_chave_acesso(cuf: int, cnpj: str, mod: int, serie: int,
                        nnf: int, tp_emis: int = 1,
                        data: datetime | None = None) -> str:
    """
    Gera a chave de acesso de 44 dígitos.
    cUF(2) + AAMM(4) + CNPJ(14) + mod(2) + serie(3) + nNF(9) + tpEmis(1) + cNF(8) + cDV(1)
    """
    if data is None:
        data = datetime.now()
    aamm   = data.strftime("%y%m")
    cnpj14 = _so_numeros(cnpj).zfill(14)
    cnf    = str(random.randint(10_000_000, 99_999_999))
    chave43 = (f"{cuf:02d}{aamm}{cnpj14}{mod:02d}"
               f"{serie:03d}{nnf:09d}{tp_emis}{cnf}")
    return chave43 + str(_calcular_dv(chave43))


# ─── Montagem do XML NF-e ─────────────────────────────────────────

def _sub(pai, tag: str, texto=None):
    el = etree.SubElement(pai, f"{{{NS}}}{tag}")
    if texto is not None:
        el.text = str(texto)
    return el


def montar_nfe_xml(dados: dict) -> tuple[etree._Element, str]:
    """
    Monta o elemento <NFe> completo (sem assinatura).
    Retorna (nfe_element, chave_acesso).

    Campos esperados em `dados` (mínimo para NF-e modelo 55 síncrona):
        # Identificação do emitente
        uf, cnpj_emitente, ie_emitente, xNome_emitente,
        xFant_emitente (opcional), crt (1=Simples, 3=Normal)
        cep_emitente, xLgr_emitente, nro_emitente, xBairro_emitente,
        cMun_emitente (IBGE 7 dig), xMun_emitente

        # Identificação da nota
        serie (int), nnf (int), nat_op, tp_nf (0=entrada,1=saída)
        id_dest (1=interna,2=interestadual,3=exterior)
        tp_emis (1=normal,6=SVC-AN,7=SVC-RS), fin_nfe (1=normal)
        ind_final (0=normal,1=consumidor final)
        ind_pres (1=presencial,2=internet...)

        # Destinatário
        cnpj_destinatario ou cpf_destinatario
        xNome_destinatario, ie_destinatario (ou 'ISENTO')
        cep_destinatario, xLgr_destinatario, nro_destinatario,
        xBairro_destinatario, cMun_destinatario, xMun_destinatario, uf_destinatario

        # Itens: lista de dicts em dados['itens']
        Cada item: nItem, cProd, xProd, NCM, CFOP, uCom, qCom, vUnCom,
                   vProd, uTrib, qTrib, vUnTrib, indTot (1=compõe total)
                   # Tributação ICMS mínima (simples):
                   orig (0=nacional), CSOSN (simples) ou CST (regime normal)

        # Totais (ou calculado automaticamente se omitido)
        vBC, vICMS, vProd (soma itens), vNF

        # Transporte
        mod_frete (0=emitente,1=destinatário,2=terceiro,9=sem frete)

        # Pagamento
        tp_pag (01=dinheiro,03=cartão crédito,04=cartão débito,99=outros)
        v_pag (valor pago)

        # Certificado
        caminho_certificado, senha_certificado
    """
    uf      = dados["uf"].upper()
    cuf     = _UF_IBGE[uf]
    cnpj    = _so_numeros(dados["cnpj_emitente"])
    serie   = int(dados.get("serie", 1))
    nnf     = int(dados["nnf"])
    tp_emis = int(dados.get("tp_emis", 1))
    mod     = 55  # NF-e de produto

    chave = gerar_chave_acesso(cuf, cnpj, mod, serie, nnf, tp_emis)

    nfe = etree.Element(f"{{{NS}}}NFe", xmlns=NS)
    inf = etree.SubElement(nfe, f"{{{NS}}}infNFe",
                           Id=f"NFe{chave}", versao="4.00")

    # ── Grupo B: Identificação ─────────────────────────────────────
    ide = _sub(inf, "ide")
    _sub(ide, "cUF",     cuf)
    _sub(ide, "cNF",     chave[35:43])  # 8 dígitos do cNF (posição 35-42)
    _sub(ide, "natOp",   dados.get("nat_op", "Venda"))
    _sub(ide, "mod",     mod)
    _sub(ide, "serie",   f"{serie:03d}")
    _sub(ide, "nNF",     f"{nnf:09d}")
    _sub(ide, "dhEmi",   datetime.now(timezone(timedelta(hours=-3)))
                                  .strftime("%Y-%m-%dT%H:%M:%S-03:00"))
    _sub(ide, "tpNF",    dados.get("tp_nf", "1"))
    _sub(ide, "idDest",  dados.get("id_dest", "1"))
    _sub(ide, "cMunFG",  _so_numeros(dados["cMun_emitente"]))
    _sub(ide, "tpImp",   dados.get("tp_imp", "1"))  # 1=DANFE retrato
    _sub(ide, "tpEmis",  tp_emis)
    _sub(ide, "cDV",     chave[43])
    _sub(ide, "tpAmb",   _tp_amb())
    _sub(ide, "finNFe",  dados.get("fin_nfe", "1"))
    _sub(ide, "indFinal", dados.get("ind_final", "0"))
    _sub(ide, "indPres",  dados.get("ind_pres", "1"))
    _sub(ide, "procEmi",  "0")
    _sub(ide, "verProc",  dados.get("ver_proc", "1.0.0"))

    # ── Grupo C: Emitente ─────────────────────────────────────────
    emit = _sub(inf, "emit")
    _sub(emit, "CNPJ",   cnpj)
    _sub(emit, "xNome",  dados["xNome_emitente"])
    if dados.get("xFant_emitente"):
        _sub(emit, "xFant", dados["xFant_emitente"])
    end_emit = _sub(emit, "enderEmit")
    _sub(end_emit, "xLgr",   dados["xLgr_emitente"])
    _sub(end_emit, "nro",    dados["nro_emitente"])
    if dados.get("xCpl_emitente"):
        _sub(end_emit, "xCpl", dados["xCpl_emitente"])
    _sub(end_emit, "xBairro", dados["xBairro_emitente"])
    _sub(end_emit, "cMun",    _so_numeros(dados["cMun_emitente"]))
    _sub(end_emit, "xMun",    dados["xMun_emitente"])
    _sub(end_emit, "UF",      uf)
    _sub(end_emit, "CEP",     _so_numeros(dados.get("cep_emitente", "")))
    _sub(end_emit, "cPais",   "1058")
    _sub(end_emit, "xPais",   "BRASIL")
    if dados.get("fone_emitente"):
        _sub(end_emit, "fone", _so_numeros(dados["fone_emitente"]))
    _sub(emit, "IE",  _so_numeros(dados["ie_emitente"]))
    _sub(emit, "CRT", str(dados.get("crt", "1")))  # 1=Simples, 3=Normal

    # ── Grupo E: Destinatário ─────────────────────────────────────
    dest = _sub(inf, "dest")
    cnpj_dest = _so_numeros(dados.get("cnpj_destinatario", ""))
    cpf_dest  = _so_numeros(dados.get("cpf_destinatario", ""))
    if cnpj_dest:
        _sub(dest, "CNPJ", cnpj_dest)
    elif cpf_dest:
        _sub(dest, "CPF", cpf_dest)
    _sub(dest, "xNome", dados["xNome_destinatario"])
    end_dest = _sub(dest, "enderDest")
    _sub(end_dest, "xLgr",    dados["xLgr_destinatario"])
    _sub(end_dest, "nro",     dados["nro_destinatario"])
    if dados.get("xCpl_destinatario"):
        _sub(end_dest, "xCpl", dados["xCpl_destinatario"])
    _sub(end_dest, "xBairro", dados["xBairro_destinatario"])
    _sub(end_dest, "cMun",    _so_numeros(dados["cMun_destinatario"]))
    _sub(end_dest, "xMun",    dados["xMun_destinatario"])
    _sub(end_dest, "UF",      dados["uf_destinatario"].upper())
    _sub(end_dest, "CEP",     _so_numeros(dados.get("cep_destinatario", "")))
    _sub(end_dest, "cPais",   "1058")
    _sub(end_dest, "xPais",   "BRASIL")
    ie_dest = dados.get("ie_destinatario", "")
    _sub(dest, "indIEDest", "9" if ie_dest.upper() == "ISENTO" or not ie_dest else "1")
    if ie_dest and ie_dest.upper() != "ISENTO":
        _sub(dest, "IE", _so_numeros(ie_dest))
    if dados.get("email_destinatario"):
        _sub(dest, "email", dados["email_destinatario"])

    # ── Grupo H: Itens ────────────────────────────────────────────
    itens = dados.get("itens", [])
    v_prod_total = 0.0
    for item in itens:
        det = etree.SubElement(inf, f"{{{NS}}}det", nItem=str(item["nItem"]))
        prod = _sub(det, "prod")
        _sub(prod, "cProd",  item["cProd"])
        _sub(prod, "cEAN",   item.get("cEAN", "SEM GTIN"))
        _sub(prod, "xProd",  item["xProd"])
        _sub(prod, "NCM",    _so_numeros(item["NCM"]))
        _sub(prod, "CFOP",   str(item["CFOP"]))
        _sub(prod, "uCom",   item["uCom"])
        _sub(prod, "qCom",   f"{float(item['qCom']):.4f}")
        _sub(prod, "vUnCom", f"{float(item['vUnCom']):.10f}")
        v_prod = float(item["qCom"]) * float(item["vUnCom"])
        _sub(prod, "vProd",  f"{v_prod:.2f}")
        _sub(prod, "cEANTrib", item.get("cEANTrib", "SEM GTIN"))
        _sub(prod, "uTrib",  item.get("uTrib", item["uCom"]))
        _sub(prod, "qTrib",  f"{float(item.get('qTrib', item['qCom'])):.4f}")
        _sub(prod, "vUnTrib", f"{float(item.get('vUnTrib', item['vUnCom'])):.10f}")
        _sub(prod, "indTot", str(item.get("indTot", "1")))
        v_prod_total += v_prod

        # Tributação ICMS — Simples Nacional (CRT=1): usa CSOSN
        imposto = _sub(det, "imposto")
        icms    = _sub(imposto, "ICMS")
        crt     = int(dados.get("crt", "1"))
        if crt == 1:  # Simples Nacional
            csosn = str(item.get("CSOSN", "102"))
            orig  = str(item.get("orig", "0"))
            grupo_icms = _sub(icms, f"ICMSSN{csosn[:3]}")
            _sub(grupo_icms, "orig", orig)
            _sub(grupo_icms, "CSOSN", csosn)
        else:  # Regime Normal (CRT=3)
            cst_icms = str(item.get("CST_ICMS", "00"))
            orig     = str(item.get("orig", "0"))
            grupo_icms = _sub(icms, f"ICMS{cst_icms}")
            _sub(grupo_icms, "orig", orig)
            _sub(grupo_icms, "CST", cst_icms)
            if cst_icms == "00":
                _sub(grupo_icms, "modBC",  "3")
                _sub(grupo_icms, "vBC",    f"{v_prod:.2f}")
                _sub(grupo_icms, "pICMS",  f"{float(item.get('pICMS', 12)):.2f}")
                v_icms = v_prod * float(item.get("pICMS", 12)) / 100
                _sub(grupo_icms, "vICMS",  f"{v_icms:.2f}")

        # PIS e COFINS: NT (não tributado) como mínimo para Simples Nacional
        pis = _sub(imposto, "PIS")
        if crt == 1:
            pisnt = _sub(pis, "PISNT")
            _sub(pisnt, "CST", "07")  # 07=Operação isenta da contribuição
        else:
            pisal = _sub(pis, "PISAliq")
            _sub(pisal, "CST",   item.get("CST_PIS", "01"))
            _sub(pisal, "vBC",   f"{v_prod:.2f}")
            _sub(pisal, "pPIS",  f"{float(item.get('pPIS', 0.65)):.2f}")
            _sub(pisal, "vPIS",  f"{v_prod * float(item.get('pPIS', 0.65)) / 100:.2f}")

        cofins = _sub(imposto, "COFINS")
        if crt == 1:
            cofinsnt = _sub(cofins, "COFINSNT")
            _sub(cofinsnt, "CST", "07")
        else:
            cofinsal = _sub(cofins, "COFINSAliq")
            _sub(cofinsal, "CST",      item.get("CST_COFINS", "01"))
            _sub(cofinsal, "vBC",      f"{v_prod:.2f}")
            _sub(cofinsal, "pCOFINS",  f"{float(item.get('pCOFINS', 3.0)):.2f}")
            _sub(cofinsal, "vCOFINS",  f"{v_prod * float(item.get('pCOFINS', 3.0)) / 100:.2f}")

    # ── Grupo W: Totalizadores ────────────────────────────────────
    total  = _sub(inf, "total")
    ictot  = _sub(total, "ICMSTot")
    v_nf   = float(dados.get("vNF", v_prod_total))
    _sub(ictot, "vBC",    dados.get("vBC",    "0.00"))
    _sub(ictot, "vICMS",  dados.get("vICMS",  "0.00"))
    _sub(ictot, "vICMSDeson", "0.00")
    _sub(ictot, "vFCP",   "0.00")
    _sub(ictot, "vBCST",  "0.00")
    _sub(ictot, "vST",    "0.00")
    _sub(ictot, "vFCPST", "0.00")
    _sub(ictot, "vFCPSTRet", "0.00")
    _sub(ictot, "vProd",  f"{v_prod_total:.2f}")
    _sub(ictot, "vFrete", dados.get("vFrete", "0.00"))
    _sub(ictot, "vSeg",   "0.00")
    _sub(ictot, "vDesc",  dados.get("vDesc", "0.00"))
    _sub(ictot, "vII",    "0.00")
    _sub(ictot, "vIPI",   "0.00")
    _sub(ictot, "vIPIDevol", "0.00")
    _sub(ictot, "vPIS",   "0.00")
    _sub(ictot, "vCOFINS","0.00")
    _sub(ictot, "vOutro", "0.00")
    _sub(ictot, "vNF",    f"{v_nf:.2f}")

    # ── Grupo X: Transporte ───────────────────────────────────────
    transp = _sub(inf, "transp")
    _sub(transp, "modFrete", str(dados.get("mod_frete", "9")))

    # ── Grupo YA: Pagamento ───────────────────────────────────────
    pag = _sub(inf, "pag")
    det_pag = _sub(pag, "detPag")
    _sub(det_pag, "tPag", str(dados.get("tp_pag", "01")))
    _sub(det_pag, "vPag", f"{v_nf:.2f}")

    # ── Grupo Z: Informações Adicionais ───────────────────────────
    if dados.get("inf_adic") or _tp_amb() == "2":
        inf_adic = _sub(inf, "infAdic")
        if _tp_amb() == "2":
            _sub(inf_adic, "infCpl",
                 dados.get("inf_adic", "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"))
        elif dados.get("inf_adic"):
            _sub(inf_adic, "infCpl", dados["inf_adic"])

    return nfe, chave


# ─── Assinatura XMLDSIG (RSA-SHA1 — exigido pelo MOC NF-e 4.00) ──

def assinar_nfe(nfe_element: etree._Element, chave_privada, certificado) -> etree._Element:
    """
    Assina a tag <infNFe> com RSA-SHA1 (padrão exigido pela SEFAZ).
    Retorna o elemento <NFe> com <Signature> inserida.
    """
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    inf_id = nfe_element.find(f"{{{NS}}}infNFe").get("Id")
    signed = signer.sign(
        nfe_element,
        key=chave_privada,
        cert=certificado,
        reference_uri=inf_id,
    )
    return signed


def assinar_evento(env_element: etree._Element, chave_privada, certificado) -> etree._Element:
    """Assina a tag <infEvento> do envelope de eventos."""
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    inf_id = env_element.find(f".//{{{NS}}}infEvento").get("Id")
    return signer.sign(
        env_element,
        key=chave_privada,
        cert=certificado,
        reference_uri=inf_id,
    )


# ─── SOAP 1.2 ─────────────────────────────────────────────────────

_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"

def _montar_soap(servico: str, cuf: int, xml_inner: str) -> bytes:
    """
    Monta envelope SOAP 1.2.
    servico: ex 'NFeAutorizacao4'
    """
    wsdl_ns = f"{WSDL_BASE}/{servico}"
    soap = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap12:Envelope'
        f'  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        f'  xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f'  xmlns:soap12="{_SOAP_NS}">'
        f'<soap12:Header>'
        f'<nfeCabecMsg xmlns="{wsdl_ns}">'
        f'<cUF>{cuf}</cUF>'
        f'<versaoDados>4.00</versaoDados>'
        f'</nfeCabecMsg>'
        f'</soap12:Header>'
        f'<soap12:Body>'
        f'<nfeDadosMsg xmlns="{wsdl_ns}">'
        f'{xml_inner}'
        f'</nfeDadosMsg>'
        f'</soap12:Body>'
        f'</soap12:Envelope>'
    )
    return soap.encode("utf-8")


def _enviar_soap(url: str, servico: str, cuf: int, xml_inner: str,
                 cert_path: str, key_path: str) -> etree._Element:
    """Envia o envelope SOAP e retorna o XML do corpo da resposta."""
    soap_bytes = _montar_soap(servico, cuf, xml_inner)
    print(f"[nfe] POST {url}", flush=True)
    resp = requests.post(
        url,
        data=soap_bytes,
        cert=(cert_path, key_path),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=30,
    )
    print(f"[nfe] HTTP {resp.status_code}", flush=True)
    resp.raise_for_status()
    return etree.fromstring(resp.content)


def _extrair_body(resp_xml: etree._Element) -> etree._Element:
    """Extrai o primeiro filho do <soap:Body>."""
    body = resp_xml.find(f"{{{_SOAP_NS}}}Body")
    return next(iter(body)) if body is not None else resp_xml


# ─── Operações da SEFAZ ───────────────────────────────────────────

def consultar_status_servico(uf: str, cert_path: str, key_path: str) -> dict:
    """
    Consulta NFeStatusServico4. Retorna dict com cStat e xMotivo.
    cStat=107 significa 'Serviço em Operação'.
    """
    cuf   = _UF_IBGE[uf.upper()]
    xml   = (f'<consStatServ versao="4.00" xmlns="{NS}">'
             f'<tpAmb>{_tp_amb()}</tpAmb>'
             f'<cUF>{cuf}</cUF>'
             f'<xServ>STATUS</xServ>'
             f'</consStatServ>')
    url   = _url_servico(uf, "NFeStatusServico4")
    resp  = _enviar_soap(url, "NFeStatusServico4", cuf, xml, cert_path, key_path)
    body  = _extrair_body(resp)
    ns    = {"nfe": NS}
    cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    print(f"[nfe] Status: cStat={cstat} | {xmot}", flush=True)
    return {"cStat": cstat, "xMotivo": xmot}


def _autorizar(nfe_element: etree._Element, uf: str, cuf: int,
               cert_path: str, key_path: str) -> etree._Element:
    """Envia enviNFe com indSinc=1 (síncrono) e retorna corpo da resposta."""
    nfe_str = etree.tostring(nfe_element, encoding="unicode")
    env_str = (f'<enviNFe versao="4.00" xmlns="{NS}">'
               f'<idLote>1</idLote>'
               f'<indSinc>1</indSinc>'
               f'{nfe_str}'
               f'</enviNFe>')
    url  = _url_servico(uf, "NFeAutorizacao4")
    resp = _enviar_soap(url, "NFeAutorizacao4", cuf, env_str, cert_path, key_path)
    return _extrair_body(resp)


def _montar_proc_nfe(nfe_str: str, prot_xml: str) -> bytes:
    """Monta nfeProc (NF-e autorizada + protocolo) — arquivo definitivo."""
    return (
        f'<nfeProc versao="4.00" xmlns="{NS}">'
        f'{nfe_str}'
        f'{prot_xml}'
        f'</nfeProc>'
    ).encode("utf-8")


# ─── Cancelamento ─────────────────────────────────────────────────

def cancelar_nfe(chave: str, n_prot: str, justificativa: str,
                 uf: str, cnpj: str,
                 cert_path: str, key_path: str,
                 chave_privada, certificado) -> dict:
    """
    Registra evento de cancelamento (tpEvento=110111).
    Prazo: 24h após autorização (antes da circulação da mercadoria).
    """
    cuf      = _UF_IBGE[uf.upper()]
    dh_evento = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    id_evento = f"ID110111{chave}01"

    env = etree.Element(f"{{{NS}}}envEvento", versao="1.00")
    _sub(env, "idLote", "1")
    evento = _sub(env, "evento", versao="1.00")
    inf_ev = etree.SubElement(evento, f"{{{NS}}}infEvento",
                              Id=id_evento, versao="1.00")
    _sub(inf_ev, "cOrgao",     cuf)
    _sub(inf_ev, "tpAmb",      _tp_amb())
    _sub(inf_ev, "CNPJ",       _so_numeros(cnpj))
    _sub(inf_ev, "chNFe",      chave)
    _sub(inf_ev, "dhEvento",   dh_evento)
    _sub(inf_ev, "tpEvento",   "110111")
    _sub(inf_ev, "nSeqEvento", "1")
    _sub(inf_ev, "verEvento",  "1.00")
    det = _sub(inf_ev, "detEvento", versao="1.00")
    _sub(det, "descEvento", "Cancelamento")
    _sub(det, "nProt",      n_prot)
    _sub(det, "xJust",      justificativa)

    env_assinado = assinar_evento(env, chave_privada, certificado)
    env_str      = etree.tostring(env_assinado, encoding="unicode")

    url  = _url_servico(uf, "NFeRecepcaoEvento4")
    resp = _enviar_soap(url, "NFeRecepcaoEvento4", cuf, env_str, cert_path, key_path)
    body = _extrair_body(resp)
    ns   = {"nfe": NS}
    cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    print(f"[nfe] Cancelamento: cStat={cstat} | {xmot}", flush=True)
    return {"cStat": cstat, "xMotivo": xmot}


# ─── Consulta de Protocolo ────────────────────────────────────────

def consultar_protocolo(chave: str, uf: str,
                         cert_path: str, key_path: str) -> dict:
    """Consulta NFeConsultaProtocolo4 pela chave de acesso."""
    cuf = _UF_IBGE[uf.upper()]
    xml = (f'<consSitNFe versao="4.00" xmlns="{NS}">'
           f'<tpAmb>{_tp_amb()}</tpAmb>'
           f'<xServ>CONSULTAR</xServ>'
           f'<chNFe>{chave}</chNFe>'
           f'</consSitNFe>')
    url  = _url_servico(uf, "NFeConsultaProtocolo4")
    resp = _enviar_soap(url, "NFeConsultaProtocolo4", cuf, xml, cert_path, key_path)
    body = _extrair_body(resp)
    ns   = {"nfe": NS}
    cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    nprot = body.findtext(".//nfe:nProt", namespaces=ns) or ""
    return {"cStat": cstat, "xMotivo": xmot, "nProt": nprot}


# ─── Ponto de entrada principal ───────────────────────────────────

def emitir_nfe(dados: dict) -> dict:
    """
    Emite NF-e modelo 55 via SOAP/mTLS.
    Interface: recebe dict com campos do emitente, destinatário, itens e certificado.
    Retorna: {"chave": str, "n_prot": str, "xml_path": str, "cStat": str}

    Ambiente: NFE_AMBIENTE=1 (produção) | 2 (homologação, padrão)
    """
    caminho_pfx = dados["caminho_certificado"]
    senha_pfx   = dados["senha_certificado"]
    uf          = dados["uf"].upper()
    cuf         = _UF_IBGE[uf]

    print(f"[nfe] {'='*60}", flush=True)
    print(f"[nfe] INÍCIO EMISSÃO | UF={uf} | CNPJ={_so_numeros(dados.get('cnpj_emitente',''))} | "
          f"nNF={dados.get('nnf')} | "
          f"ambiente={'PRODUÇÃO' if _is_prod() else 'HOMOLOGAÇÃO'}", flush=True)

    cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
        caminho_pfx, senha_pfx
    )

    try:
        # 1. Verificar status do serviço
        status = consultar_status_servico(uf, cert_path, key_path)
        if status["cStat"] != "107":
            raise RuntimeError(
                f"Serviço SEFAZ indisponível: cStat={status['cStat']} | {status['xMotivo']}"
            )

        # 2. Montar e assinar NF-e
        print("[nfe] Montando XML da NF-e...", flush=True)
        nfe_element, chave = montar_nfe_xml(dados)

        print("[nfe] Assinando com XMLDSIG RSA-SHA1...", flush=True)
        nfe_assinada = assinar_nfe(nfe_element, chave_privada, certificado)
        nfe_str      = etree.tostring(nfe_assinada, encoding="unicode")

        # 3. Enviar para autorização
        print("[nfe] Enviando para SEFAZ (síncrono)...", flush=True)
        body = _autorizar(nfe_assinada, uf, cuf, cert_path, key_path)

        ns    = {"nfe": NS}
        cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
        xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
        nprot = body.findtext(".//nfe:nProt",   namespaces=ns) or ""
        print(f"[nfe] Resposta: cStat={cstat} | {xmot}", flush=True)

        if cstat not in ("100", "150"):
            raise RuntimeError(f"NF-e não autorizada: [{cstat}] {xmot}")

        print(f"[nfe] ✓ AUTORIZADA | chave={chave} | nProt={nprot}", flush=True)

        # 4. Montar procNFe e salvar
        prot_el  = body.find(f".//{{{NS}}}protNFe")
        prot_str = etree.tostring(prot_el, encoding="unicode") if prot_el is not None else ""
        proc_bytes = _montar_proc_nfe(nfe_str, prot_str)

        cnpj_dir   = _so_numeros(dados.get("cnpj_emitente", ""))
        downloads  = (os.environ.get("DOWNLOADS_PATH")
                      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))
        saida_dir  = os.path.join(downloads, cnpj_dir) if cnpj_dir else downloads
        os.makedirs(saida_dir, exist_ok=True)

        xml_path = os.path.join(saida_dir, f"nfe_{chave}-procNFe.xml")
        with open(xml_path, "wb") as f:
            f.write(proc_bytes)
        print(f"[nfe] procNFe salvo: {xml_path}", flush=True)
        print(f"[nfe] {'='*60}", flush=True)

        return {
            "chave":    chave,
            "n_prot":   nprot,
            "cStat":    cstat,
            "xMotivo":  xmot,
            "xml_path": xml_path,
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── NFC-e (Modelo 65) ────────────────────────────────────────────

def _calcular_hash_qrcode(chave: str, tp_amb: str, dh_emi: str,
                           v_nf: str, v_icms: str,
                           dig_val: str, id_csc: str, csc: str) -> str:
    """
    Hash SHA-256 do QR Code conforme NT 2016.002.
    dig_val: DigestValue extraído do XML assinado.
    csc:     Código de Segurança do Contribuinte (sem formatação).
    id_csc:  Identificador do CSC (3 dígitos, ex: '001').
    """
    texto = f"{chave}|{tp_amb}|{dh_emi}|{v_nf}|{v_icms}|{dig_val}|{id_csc}{csc}"
    return hashlib.sha256(texto.encode("utf-8")).hexdigest().upper()


def montar_nfce_xml(dados: dict) -> tuple[etree._Element, str]:
    """
    Monta o elemento <NFe> completo para NFC-e modelo 65 (sem assinatura).
    Retorna (nfe_element, chave_acesso).

    Campos adicionais em `dados` vs NF-e:
        ind_pres  (obrigatório: 1=presencial, 2=internet, 4=entrega)
        ind_final = '1' (sempre consumidor final para NFC-e)
        # Destinatário é opcional — omitir para consumidor não identificado
        # Pagamento obrigatório
        id_csc    Identificador do CSC cadastrado na SEFAZ (ex: '000001')
        csc       Código de Segurança do Contribuinte (token SEFAZ, sem formatação)
    """
    uf      = dados["uf"].upper()
    cuf     = _UF_IBGE[uf]
    cnpj    = _so_numeros(dados["cnpj_emitente"])
    serie   = int(dados.get("serie", 1))
    nnf     = int(dados["nnf"])
    tp_emis = int(dados.get("tp_emis", 1))
    mod     = 65

    chave = gerar_chave_acesso(cuf, cnpj, mod, serie, nnf, tp_emis)
    dh_emi = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S-03:00")

    nfe = etree.Element(f"{{{NS}}}NFe", xmlns=NS)
    inf = etree.SubElement(nfe, f"{{{NS}}}infNFe",
                           Id=f"NFe{chave}", versao="4.00")

    # ── Identificação ────────────────────────────────────────────────
    ide = _sub(inf, "ide")
    _sub(ide, "cUF",      cuf)
    _sub(ide, "cNF",      chave[35:43])
    _sub(ide, "natOp",    dados.get("nat_op", "Venda a Consumidor"))
    _sub(ide, "mod",      mod)
    _sub(ide, "serie",    f"{serie:03d}")
    _sub(ide, "nNF",      f"{nnf:09d}")
    _sub(ide, "dhEmi",    dh_emi)
    _sub(ide, "tpNF",     dados.get("tp_nf", "1"))
    _sub(ide, "idDest",   dados.get("id_dest", "1"))
    _sub(ide, "cMunFG",   _so_numeros(dados["cMun_emitente"]))
    _sub(ide, "tpImp",    "4")   # 4 = DANFE NFC-e
    _sub(ide, "tpEmis",   tp_emis)
    _sub(ide, "cDV",      chave[43])
    _sub(ide, "tpAmb",    _tp_amb())
    _sub(ide, "finNFe",   "1")
    _sub(ide, "indFinal", "1")   # sempre consumidor final
    _sub(ide, "indPres",  str(dados.get("ind_pres", "1")))
    _sub(ide, "procEmi",  "0")
    _sub(ide, "verProc",  dados.get("ver_proc", "1.0.0"))

    # ── Emitente ─────────────────────────────────────────────────────
    emit = _sub(inf, "emit")
    _sub(emit, "CNPJ",  cnpj)
    _sub(emit, "xNome", dados["xNome_emitente"])
    if dados.get("xFant_emitente"):
        _sub(emit, "xFant", dados["xFant_emitente"])
    end_emit = _sub(emit, "enderEmit")
    _sub(end_emit, "xLgr",    dados["xLgr_emitente"])
    _sub(end_emit, "nro",     dados["nro_emitente"])
    if dados.get("xCpl_emitente"):
        _sub(end_emit, "xCpl", dados["xCpl_emitente"])
    _sub(end_emit, "xBairro", dados["xBairro_emitente"])
    _sub(end_emit, "cMun",    _so_numeros(dados["cMun_emitente"]))
    _sub(end_emit, "xMun",    dados["xMun_emitente"])
    _sub(end_emit, "UF",      uf)
    _sub(end_emit, "CEP",     _so_numeros(dados.get("cep_emitente", "")))
    _sub(end_emit, "cPais",   "1058")
    _sub(end_emit, "xPais",   "BRASIL")
    if dados.get("fone_emitente"):
        _sub(end_emit, "fone", _so_numeros(dados["fone_emitente"]))
    _sub(emit, "IE",  _so_numeros(dados["ie_emitente"]))
    _sub(emit, "CRT", str(dados.get("crt", "1")))

    # ── Destinatário (opcional) ───────────────────────────────────────
    cnpj_dest = _so_numeros(dados.get("cnpj_destinatario", dados.get("cnpj_dest", "")))
    cpf_dest  = _so_numeros(dados.get("cpf_destinatario",  dados.get("cpf_dest",  "")))
    xnome     = dados.get("xNome_destinatario", dados.get("xNome_dest", ""))
    if cnpj_dest or cpf_dest:
        dest = _sub(inf, "dest")
        if cnpj_dest:
            _sub(dest, "CNPJ", cnpj_dest)
        else:
            _sub(dest, "CPF", cpf_dest)
        _sub(dest, "xNome", xnome or "CONSUMIDOR NAO IDENTIFICADO")
        _sub(dest, "indIEDest", "9")
        if dados.get("email_destinatario") or dados.get("email_dest"):
            _sub(dest, "email", dados.get("email_destinatario") or dados.get("email_dest"))

    # ── Itens ─────────────────────────────────────────────────────────
    itens = dados.get("itens", [])
    v_prod_total = 0.0
    for item in itens:
        det  = etree.SubElement(inf, f"{{{NS}}}det", nItem=str(item["nItem"]))
        prod = _sub(det, "prod")
        _sub(prod, "cProd",   item.get("cProd", "ITEM"))
        _sub(prod, "cEAN",    item.get("cEAN", "SEM GTIN"))
        _sub(prod, "xProd",   item["xProd"])
        _sub(prod, "NCM",     _so_numeros(item.get("NCM", "00000000")))
        _sub(prod, "CFOP",    str(item.get("CFOP", "5102")))
        _sub(prod, "uCom",    item.get("uCom", item.get("unidade", "UN")))
        q = float(item.get("qCom", item.get("qtd", 1)))
        v = float(item.get("vUnCom", item.get("preco", 0)))
        _sub(prod, "qCom",    f"{q:.4f}")
        _sub(prod, "vUnCom",  f"{v:.10f}")
        v_prod = q * v
        _sub(prod, "vProd",   f"{v_prod:.2f}")
        _sub(prod, "cEANTrib", item.get("cEANTrib", "SEM GTIN"))
        _sub(prod, "uTrib",   item.get("uCom", item.get("unidade", "UN")))
        _sub(prod, "qTrib",   f"{q:.4f}")
        _sub(prod, "vUnTrib", f"{v:.10f}")
        _sub(prod, "indTot",  "1")
        v_prod_total += v_prod

        imposto = _sub(det, "imposto")
        icms    = _sub(imposto, "ICMS")
        crt     = int(dados.get("crt", "1"))
        csosn   = str(item.get("CSOSN", item.get("csosn", "102")))
        orig    = str(item.get("orig", "0"))
        if crt == 1:
            grupo = _sub(icms, f"ICMSSN{csosn[:3]}")
            _sub(grupo, "orig",  orig)
            _sub(grupo, "CSOSN", csosn)
        else:
            cst = str(item.get("CST_ICMS", "00"))
            grupo = _sub(icms, f"ICMS{cst}")
            _sub(grupo, "orig", orig)
            _sub(grupo, "CST",  cst)
            if cst == "00":
                _sub(grupo, "modBC",  "3")
                _sub(grupo, "vBC",    f"{v_prod:.2f}")
                _sub(grupo, "pICMS",  f"{float(item.get('pICMS', 12)):.2f}")
                _sub(grupo, "vICMS",  f"{v_prod * float(item.get('pICMS', 12)) / 100:.2f}")

        pis = _sub(imposto, "PIS")
        pisnt = _sub(pis, "PISNT")
        _sub(pisnt, "CST", "07")

        cofins = _sub(imposto, "COFINS")
        cofinsnt = _sub(cofins, "COFINSNT")
        _sub(cofinsnt, "CST", "07")

    # ── Totais ────────────────────────────────────────────────────────
    v_nf   = float(dados.get("v_nf", dados.get("vNF", v_prod_total)))
    v_desc = float(dados.get("v_desc", dados.get("vDesc", 0)))
    total  = _sub(inf, "total")
    ictot  = _sub(total, "ICMSTot")
    _sub(ictot, "vBC",       "0.00")
    _sub(ictot, "vICMS",     "0.00")
    _sub(ictot, "vICMSDeson","0.00")
    _sub(ictot, "vFCP",      "0.00")
    _sub(ictot, "vBCST",     "0.00")
    _sub(ictot, "vST",       "0.00")
    _sub(ictot, "vFCPST",    "0.00")
    _sub(ictot, "vFCPSTRet", "0.00")
    _sub(ictot, "vProd",     f"{v_prod_total:.2f}")
    _sub(ictot, "vFrete",    "0.00")
    _sub(ictot, "vSeg",      "0.00")
    _sub(ictot, "vDesc",     f"{v_desc:.2f}")
    _sub(ictot, "vII",       "0.00")
    _sub(ictot, "vIPI",      "0.00")
    _sub(ictot, "vIPIDevol", "0.00")
    _sub(ictot, "vPIS",      "0.00")
    _sub(ictot, "vCOFINS",   "0.00")
    _sub(ictot, "vOutro",    "0.00")
    _sub(ictot, "vNF",       f"{v_nf:.2f}")

    # ── Transporte (mod 9 = sem frete, obrigatório no XML) ───────────
    transp = _sub(inf, "transp")
    _sub(transp, "modFrete", "9")

    # ── Pagamento ─────────────────────────────────────────────────────
    pag     = _sub(inf, "pag")
    det_pag = _sub(pag, "detPag")
    _sub(det_pag, "tPag", str(dados.get("tp_pag", "01")))
    _sub(det_pag, "vPag", f"{v_nf:.2f}")

    # ── Informações adicionais ────────────────────────────────────────
    if dados.get("inf_adic") or _tp_amb() == "2":
        inf_adic = _sub(inf, "infAdic")
        _sub(inf_adic, "infCpl",
             dados.get("inf_adic", "NFC-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"))

    # ── infNFeSupl — placeholder; QR Code inserido após assinatura ───
    supl = _sub(inf, "infNFeSupl")
    _sub(supl, "qrCode", "")      # preenchido após assinatura
    _sub(supl, "urlChave", _url_consulta_nfce(uf))

    return nfe, chave, dh_emi


def emitir_nfce(dados: dict) -> dict:
    """
    Emite NFC-e modelo 65 via SOAP/mTLS.

    Campos adicionais obrigatórios vs emitir_nfe:
        id_csc  Identificador do CSC (6 dígitos, ex: '000001') — obtido na SEFAZ
        csc     Código de Segurança do Contribuinte — token alfanumérico da SEFAZ

    Ambiente: NFE_AMBIENTE=1 (produção) | 2 (homologação, padrão)
    """
    caminho_pfx = dados["caminho_certificado"]
    senha_pfx   = dados["senha_certificado"]
    uf          = dados["uf"].upper()
    cuf         = _UF_IBGE[uf]
    id_csc      = str(dados.get("id_csc", "000001")).zfill(6)
    csc         = dados.get("csc", "")
    tp_amb      = _tp_amb()

    print(f"[nfce] {'='*60}", flush=True)
    print(f"[nfce] INÍCIO EMISSÃO NFC-e | UF={uf} | nNF={dados.get('nnf')} | "
          f"ambiente={'PRODUÇÃO' if _is_prod() else 'HOMOLOGAÇÃO'}", flush=True)

    cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
        caminho_pfx, senha_pfx
    )

    try:
        # 1. Status do serviço
        status = consultar_status_servico(uf, cert_path, key_path)
        if status["cStat"] != "107":
            raise RuntimeError(
                f"Serviço SEFAZ indisponível: cStat={status['cStat']} | {status['xMotivo']}"
            )

        # 2. Montar XML
        print("[nfce] Montando XML da NFC-e...", flush=True)
        nfe_element, chave, dh_emi = montar_nfce_xml(dados)

        # 3. Assinar
        print("[nfce] Assinando com XMLDSIG RSA-SHA1...", flush=True)
        nfe_assinada = assinar_nfe(nfe_element, chave_privada, certificado)

        # 4. Extrair DigestValue e montar QR Code
        dig_val_el = nfe_assinada.find(".//{http://www.w3.org/2000/09/xmldsig#}DigestValue")
        dig_val    = dig_val_el.text if dig_val_el is not None else ""

        v_nf   = str(float(dados.get("v_nf", dados.get("vNF", 0))))
        hash_qr = _calcular_hash_qrcode(
            chave, tp_amb, dh_emi, v_nf, "0.00", dig_val, id_csc, csc
        )
        url_consulta = _url_consulta_nfce(uf)
        qr_content   = f"{url_consulta}?p={chave}|{tp_amb}|{id_csc}|{hash_qr}"

        # Atualiza o qrCode no XML assinado
        ns_nfe = {"nfe": NS}
        qr_el  = nfe_assinada.find(".//nfe:qrCode", ns_nfe)
        if qr_el is not None:
            qr_el.text = qr_content

        nfe_str = etree.tostring(nfe_assinada, encoding="unicode")

        # 5. Enviar
        print("[nfce] Enviando NFC-e para SEFAZ (síncrono)...", flush=True)
        env_str = (f'<enviNFe versao="4.00" xmlns="{NS}">'
                   f'<idLote>1</idLote><indSinc>1</indSinc>'
                   f'{nfe_str}</enviNFe>')
        url  = _url_servico_nfce(uf, "NFeAutorizacao4")
        resp = _enviar_soap(url, "NFeAutorizacao4", cuf, env_str, cert_path, key_path)
        body = _extrair_body(resp)

        ns    = {"nfe": NS}
        cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
        xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
        nprot = body.findtext(".//nfe:nProt",   namespaces=ns) or ""
        print(f"[nfce] Resposta: cStat={cstat} | {xmot}", flush=True)

        if cstat not in ("100", "150"):
            raise RuntimeError(f"NFC-e não autorizada: [{cstat}] {xmot}")

        print(f"[nfce] ✓ AUTORIZADA | chave={chave} | nProt={nprot}", flush=True)

        # 6. Salvar procNFe
        prot_el    = body.find(f".//{{{NS}}}protNFe")
        prot_str   = etree.tostring(prot_el, encoding="unicode") if prot_el is not None else ""
        proc_bytes = _montar_proc_nfe(nfe_str, prot_str)

        cnpj_dir  = _so_numeros(dados.get("cnpj_emitente", ""))
        downloads = (os.environ.get("DOWNLOADS_PATH")
                     or os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))
        saida_dir = os.path.join(downloads, cnpj_dir) if cnpj_dir else downloads
        os.makedirs(saida_dir, exist_ok=True)

        xml_path = os.path.join(saida_dir, f"NFe{chave}-procNFe.xml")
        with open(xml_path, "wb") as f:
            f.write(proc_bytes)
        print(f"[nfce] procNFe salvo: {xml_path}", flush=True)
        print(f"[nfce] {'='*60}", flush=True)

        return {
            "chave":    chave,
            "n_prot":   nprot,
            "cStat":    cstat,
            "xMotivo":  xmot,
            "xml_path": xml_path,
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

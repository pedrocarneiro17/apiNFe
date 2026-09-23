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
import gzip
import base64
import hashlib
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, NoEncryption, pkcs12,
)
from lxml import etree
from signxml import XMLSigner, methods
from signxml.util import namespaces as signxml_namespaces
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
    "MG":    {"prod": "https://nfce.fazenda.mg.gov.br/nfce/services/",
              "homo": "https://hnfce.fazenda.mg.gov.br/nfce/services/"},
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
    # A SEFAZ valida urlChave contra um endereço esperado exato (não é só
    # formato/tamanho) — homologação usa "hportalsped", não "portalhomolog".
    "MG": {"prod": "https://portalsped.fazenda.mg.gov.br/portalnfce",
           "homo": "https://hportalsped.fazenda.mg.gov.br/portalnfce"},
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

# Distribuição DFe (NFeDistribuicaoDFe) — webservice ÚNICO nacional (AN),
# não roteado por UF (a mesma URL serve pra todo mundo). Só modelo 55
# (NF-e); NFC-e não é coberto por esse serviço. Esse envelope SOAP não usa
# cabeçalho nfeCabecMsg (ver _montar_soap_distribuicao) — cUFAutor no corpo
# é a UF REAL do autor do pedido (o emitente consultando), não 91/AN;
# confirmado contra a implementação de referência (nfephp-org/sped-nfe).
_URL_DISTRIBUICAO_DFE: dict[str, str] = {
    "prod": "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
    "homo": "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
}

# Manifestação do destinatário — webservice único nacional (AN), não por UF
# (é o destinatário se manifestando, não o emitente; a UF dele é irrelevante
# pra SEFAZ nesse evento) — aqui sim cOrgao=91 é o valor correto.
_CUF_AN = 91
_URL_MANIFESTACAO_AN: dict[str, str] = {
    "prod": "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    "homo": "https://hom.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
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


def _url_distribuicao_dfe() -> str:
    """URL do webservice NFeDistribuicaoDFe — único, nacional, sem roteamento por UF."""
    return _URL_DISTRIBUICAO_DFE["prod" if _is_prod() else "homo"]


def _url_manifestacao_destinatario() -> str:
    """URL do NFeRecepcaoEvento4 pro evento de manifestação do destinatário
    — hospedado no Ambiente Nacional, diferente do cancelamento (que vai
    pro autorizador da UF do EMITENTE)."""
    return _URL_MANIFESTACAO_AN["prod" if _is_prod() else "homo"]


def _url_consulta_nfce(uf: str) -> str:
    """URL do portal de consulta por chave de acesso (campo urlChave)."""
    env = "prod" if _is_prod() else "homo"
    return _URL_CONSULTA_NFCE.get(uf.upper(), _URL_CONSULTA_NFCE_PADRAO)[env]


# URL usada DENTRO do conteúdo do QR Code — a SEFAZ valida contra um
# endereço esperado exato, que não é necessariamente o mesmo do urlChave.
# Fonte: ENCAT (nfce.encat.org/desenvolvedor/qrcode/) — em MG essa URL é a
# MESMA em produção e homologação (diferente do urlChave, que usa
# "hportalsped" só em homologação).
_URL_QRCODE_NFCE: dict[str, dict[str, str]] = {
    "MG": {"prod": "https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml",
           "homo": "https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml"},
}


def _url_qrcode_nfce(uf: str) -> str:
    """URL base usada no conteúdo do QR Code (pode diferir do urlChave)."""
    env = "prod" if _is_prod() else "homo"
    uf_dict = _URL_QRCODE_NFCE.get(uf.upper())
    if uf_dict:
        return uf_dict[env]
    return _url_consulta_nfce(uf)


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


# ─── IBS/CBS — Reforma Tributária (LC 214/2025) ──────────────────
#
# 2026 é o "ano-teste": o schema já aceita os grupos IBS/CBS por item
# (imposto/IBSCBS) e nos totais (total/IBSCBSTot), mas ambos são opcionais
# (minOccurs="0") — por isso as emissões anteriores validaram sem eles.
#
# Os valores abaixo são PLACEHOLDER (zerados/genéricos), só pra deixar a
# estrutura do XML pronta. CST="000" (tributação integral) e
# cClassTrib="000001" são códigos de uso comum, mas PRECISAM ser confirmados
# com o contador antes de qualquer emissão real — são dados tributários,
# não uma decisão técnica do sistema.
_IBSCBS_CST_PADRAO        = "000"
_IBSCBS_CCLASSTRIB_PADRAO = "000001"

# Alíquotas de teste do "ano-teste" 2026 — SÓ PARA VALIDAR SE A SEFAZ ACEITA
# O FORMATO. A SEFAZ rejeita alíquota zerada (cStat 1026), mas estes números
# NÃO são o valor fiscal correto: não substituem confirmação com o contador
# antes de qualquer emissão real.
_IBSCBS_P_IBS_UF_TESTE  = 0.10   # % — em 2026 o IBS municipal ainda não é cobrado
_IBSCBS_P_IBS_MUN_TESTE = 0.00   # % (só passa a valer a partir de 2027)
_IBSCBS_P_CBS_TESTE     = 0.90   # %


def _montar_ibscbs_item(imposto_element, item: dict, v_bc: float) -> float:
    """Grupo <IBSCBS> por item (dentro de <imposto>). Retorna o vBC usado,
    para acumular em <IBSCBSTot>."""
    p_ibs_uf  = float(item.get("pIBSUF",  _IBSCBS_P_IBS_UF_TESTE))
    p_ibs_mun = float(item.get("pIBSMun", _IBSCBS_P_IBS_MUN_TESTE))
    p_cbs     = float(item.get("pCBS",    _IBSCBS_P_CBS_TESTE))
    v_ibs_uf  = v_bc * p_ibs_uf  / 100
    v_ibs_mun = v_bc * p_ibs_mun / 100
    v_ibs     = v_ibs_uf + v_ibs_mun
    v_cbs     = v_bc * p_cbs / 100

    ibscbs = _sub(imposto_element, "IBSCBS")
    _sub(ibscbs, "CST", item.get("CST_IBSCBS", _IBSCBS_CST_PADRAO))
    _sub(ibscbs, "cClassTrib", item.get("cClassTrib_IBSCBS", _IBSCBS_CCLASSTRIB_PADRAO))
    g_ibscbs = _sub(ibscbs, "gIBSCBS")
    _sub(g_ibscbs, "vBC", f"{v_bc:.2f}")
    g_ibs_uf = _sub(g_ibscbs, "gIBSUF")
    _sub(g_ibs_uf, "pIBSUF", f"{p_ibs_uf:.2f}")
    _sub(g_ibs_uf, "vIBSUF", f"{v_ibs_uf:.2f}")
    g_ibs_mun = _sub(g_ibscbs, "gIBSMun")
    _sub(g_ibs_mun, "pIBSMun", f"{p_ibs_mun:.2f}")
    _sub(g_ibs_mun, "vIBSMun", f"{v_ibs_mun:.2f}")
    _sub(g_ibscbs, "vIBS", f"{v_ibs:.2f}")
    g_cbs = _sub(g_ibscbs, "gCBS")
    _sub(g_cbs, "pCBS", f"{p_cbs:.2f}")
    _sub(g_cbs, "vCBS", f"{v_cbs:.2f}")
    return v_bc


def _montar_ibscbs_total(total_element, v_bc_total: float):
    """<IBSCBSTot>, irmão de <ICMSTot> dentro de <total> — só o campo
    obrigatório (vBCIBSCBS); os subgrupos de totalização (gIBS/gCBS/gMono)
    são opcionais e ficam de fora enquanto os valores reais não existem."""
    tot = _sub(total_element, "IBSCBSTot")
    _sub(tot, "vBCIBSCBS", f"{v_bc_total:.2f}")


def _montar_nfref(ide_element, dados: dict):
    """
    Grupo <NFref> — referência a nota(s) fiscal(is) anterior(es), obrigatório
    em devolução (a chave da nota original que está sendo devolvida).
    dados['ref_nfe']: chave de 44 dígitos (str) ou lista de chaves.
    """
    refs = dados.get("ref_nfe") or []
    if isinstance(refs, str):
        refs = [refs] if refs else []
    for chave_ref in refs:
        chave_ref = _so_numeros(chave_ref)
        if len(chave_ref) != 44:
            continue
        nfref = _sub(ide_element, "NFref")
        _sub(nfref, "refNFe", chave_ref)


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

    nfe = etree.Element(f"{{{NS}}}NFe", nsmap={None: NS})
    inf = etree.SubElement(nfe, f"{{{NS}}}infNFe",
                           Id=f"NFe{chave}", versao="4.00")

    # ── Grupo B: Identificação ─────────────────────────────────────
    ide = _sub(inf, "ide")
    _sub(ide, "cUF",     cuf)
    _sub(ide, "cNF",     chave[35:43])  # 8 dígitos do cNF (posição 35-42)
    _sub(ide, "natOp",   dados.get("nat_op", "Venda"))
    _sub(ide, "mod",     mod)
    _sub(ide, "serie",   str(serie))
    _sub(ide, "nNF",     str(nnf))
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
    _montar_nfref(ide, dados)

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

        # IBS/CBS — Reforma Tributária (ver comentário em _montar_ibscbs_item)
        _montar_ibscbs_item(imposto, item, v_prod)

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
    _montar_ibscbs_total(total, v_prod_total)

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
                 dados.get("inf_adic") or "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL")
        elif dados.get("inf_adic"):
            _sub(inf_adic, "infCpl", dados["inf_adic"])

    return nfe, chave


# ─── Assinatura XMLDSIG (RSA-SHA1 — exigido pelo MOC NF-e 4.00) ──

class _XMLSignerSEFAZ(XMLSigner):
    """
    XMLSigner ajustado às exigências da SEFAZ:
      - permite RSA-SHA1: o signxml recusa SHA-1 por padrão (algoritmo inseguro
        para uso geral), mas o MOC NF-e 4.00 exige exatamente esse algoritmo —
        não é uma escolha nossa, é o que a SEFAZ aceita.
      - remove o prefixo "ds:" do bloco <Signature>: a SEFAZ rejeita qualquer
        prefixo de namespace na mensagem (erro "Uso de prefixo de namespace
        não permitido"), então a assinatura precisa sair como
        <Signature xmlns="http://www.w3.org/2000/09/xmldsig#"> sem prefixo.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.namespaces = {None: signxml_namespaces.ds}

    def check_deprecated_methods(self):
        pass


def assinar_nfe(nfe_element: etree._Element, chave_privada, certificado) -> etree._Element:
    """
    Assina a tag <infNFe> com RSA-SHA1 (padrão exigido pela SEFAZ).
    Retorna o elemento <NFe> com <Signature> inserida.
    """
    signer = _XMLSignerSEFAZ(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    inf_id = nfe_element.find(f"{{{NS}}}infNFe").get("Id")
    signed = signer.sign(
        nfe_element,
        key=chave_privada,
        cert=[certificado],
        reference_uri=inf_id,
    )
    return signed


def assinar_evento(env_element: etree._Element, chave_privada, certificado) -> etree._Element:
    """Assina a tag <infEvento> do envelope de eventos."""
    signer = _XMLSignerSEFAZ(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    inf_id = env_element.find(f".//{{{NS}}}infEvento").get("Id")
    return signer.sign(
        env_element,
        key=chave_privada,
        cert=[certificado],
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


def _montar_soap_distribuicao(xml_inner: str) -> bytes:
    """
    Envelope SOAP do NFeDistribuicaoDFe — layout DIFERENTE dos demais
    serviços (confirmado contra a implementação de referência
    nfephp-org/sped-nfe, `Tools::sefazDistDFe`):
      - SEM <nfeCabecMsg> no Header (esse webservice não usa cabeçalho).
      - O corpo vem embrulhado no nome do MÉTODO (nfeDistDFeInteresse),
        não direto dentro de nfeDadosMsg como nos outros serviços.
    Usar _montar_soap genérico aqui causa 500 da SEFAZ (envelope não
    reconhecido).
    """
    ns_wsdl = f"{WSDL_BASE}/NFeDistribuicaoDFe"
    soap = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap12:Envelope'
        f'  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        f'  xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f'  xmlns:soap12="{_SOAP_NS}">'
        f'<soap12:Body>'
        f'<nfeDistDFeInteresse xmlns="{ns_wsdl}">'
        f'<nfeDadosMsg xmlns="{ns_wsdl}">'
        f'{xml_inner}'
        f'</nfeDadosMsg>'
        f'</nfeDistDFeInteresse>'
        f'</soap12:Body>'
        f'</soap12:Envelope>'
    )
    return soap.encode("utf-8")


def _enviar_soap_distribuicao(url: str, xml_inner: str,
                              cert_path: str, key_path: str) -> etree._Element:
    """Envia a consulta de Distribuição DFe (envelope/SOAPAction próprios)."""
    soap_bytes = _montar_soap_distribuicao(xml_inner)
    soap_action = f'"{WSDL_BASE}/NFeDistribuicaoDFe/nfeDistDFeInteresse"'
    print(f"[nfe] POST {url}", flush=True)
    resp = requests.post(
        url,
        data=soap_bytes,
        cert=(cert_path, key_path),
        headers={
            "Content-Type": "application/soap+xml; charset=utf-8",
            "SOAPAction": soap_action,
        },
        timeout=30,
    )
    print(f"[nfe] HTTP {resp.status_code}", flush=True)
    resp.raise_for_status()
    return etree.fromstring(resp.content)


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


# ─── Distribuição DFe — busca notas onde o CNPJ é emitente OU destinatário ─
#
# Só modelo 55 (NF-e). Como destinatário, só vem o "resumo" (resNFe) até o
# CNPJ fazer a manifestação do destinatário — a NF-e completa (procNFe) fica
# disponível direto quando você é o emitente, ou depois de manifestar quando
# é destinatário (manifestação ainda não implementada neste sistema).

_TIPOS_SCHEMA = {
    "resNFe": "resumo", "procNFe": "completa",
    "resEvento": "evento_resumo", "procEventoNFe": "evento_completo",
}


def _classificar_schema(schema: str) -> str:
    prefixo = schema.split("_")[0] if schema else ""
    return _TIPOS_SCHEMA.get(prefixo, "outro")


def distribuir_dfe(cnpj: str, uf: str, ult_nsu: int, cert_path: str, key_path: str) -> dict:
    """
    Consulta NFeDistribuicaoDFe a partir do NSU informado (0 = do início).
    Devolve até 50 documentos por chamada — repita com o `ultNSU` retornado
    até `tem_mais` vir False pra cobrir tudo.

    `uf`: UF do autor do pedido (o emitente consultando, não o Ambiente
    Nacional) — cUFAutor é o código IBGE dessa UF, diferente da
    manifestação do destinatário (que usa cOrgao=91/AN). Confirmado
    contra a implementação de referência (nfephp-org/sped-nfe).
    """
    cuf_autor = _UF_IBGE[uf.upper()]
    xml = (f'<distDFeInt versao="1.01" xmlns="{NS}">'
           f'<tpAmb>{_tp_amb()}</tpAmb>'
           f'<cUFAutor>{cuf_autor}</cUFAutor>'
           f'<CNPJ>{_so_numeros(cnpj)}</CNPJ>'
           f'<distNSU><ultNSU>{str(ult_nsu).zfill(15)}</ultNSU></distNSU>'
           f'</distDFeInt>')
    body = _consultar_dist_dfe(xml, cert_path, key_path)
    ns   = {"nfe": NS}
    cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    ult   = int(body.findtext(".//nfe:ultNSU", namespaces=ns) or "0")
    maxn  = int(body.findtext(".//nfe:maxNSU", namespaces=ns) or "0")
    documentos = _extrair_docs_zip(body)

    print(f"[nfe] Distribuição DFe: cStat={cstat} | ultNSU={ult} | maxNSU={maxn} "
          f"| docs={len(documentos)}", flush=True)
    return {
        "cStat": cstat, "xMotivo": xmot,
        "ultNSU": ult, "maxNSU": maxn,
        "tem_mais": ult < maxn,
        "documentos": documentos,
    }


def _consultar_dist_dfe(xml_inner: str, cert_path: str, key_path: str):
    url  = _url_distribuicao_dfe()
    resp = _enviar_soap_distribuicao(url, xml_inner, cert_path, key_path)
    return _extrair_body(resp)


def _extrair_docs_zip(body) -> list:
    ns = {"nfe": NS}
    documentos = []
    for doc_zip in body.findall(".//nfe:docZip", namespaces=ns):
        nsu    = doc_zip.get("NSU", "")
        schema = doc_zip.get("schema", "")
        try:
            xml_doc = gzip.decompress(base64.b64decode(doc_zip.text))
        except Exception as e:
            print(f"[nfe] erro ao descompactar docZip NSU={nsu}: {e}", flush=True)
            continue
        documentos.append({
            "nsu": nsu, "schema": schema,
            "tipo": _classificar_schema(schema),
            "xml": xml_doc,
        })
    return documentos


def consultar_dfe_por_chave(chave: str, cnpj: str, uf: str, cert_path: str, key_path: str) -> dict:
    """
    Consulta NFeDistribuicaoDFe filtrando por uma chave de acesso específica
    (modo `consChNFe`, alternativa ao `distNSU` — não depende de paginação
    nem de onde o cursor de NSU está). Útil pra checar se uma nota
    específica está (ou não) na caixa de distribuição desse CNPJ, sem
    precisar varrer o histórico inteiro. Confirmado contra a implementação
    de referência (nfephp-org/sped-nfe, método sefazDistDFe com $chave).
    """
    cuf_autor = _UF_IBGE[uf.upper()]
    xml = (f'<distDFeInt versao="1.01" xmlns="{NS}">'
           f'<tpAmb>{_tp_amb()}</tpAmb>'
           f'<cUFAutor>{cuf_autor}</cUFAutor>'
           f'<CNPJ>{_so_numeros(cnpj)}</CNPJ>'
           f'<consChNFe><chNFe>{_so_numeros(chave)}</chNFe></consChNFe>'
           f'</distDFeInt>')
    body = _consultar_dist_dfe(xml, cert_path, key_path)
    ns   = {"nfe": NS}
    cstat = body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    documentos = _extrair_docs_zip(body)

    print(f"[nfe] Distribuição DFe (por chave {chave}): cStat={cstat} | docs={len(documentos)}", flush=True)
    return {"cStat": cstat, "xMotivo": xmot, "documentos": documentos}


def _parse_resumo_nfe(xml_bytes: bytes, cnpj_consultado: str) -> dict:
    """
    Extrai os campos exibíveis de um resNFe (resumo) ou procNFe (completa).
    `papel` compara o CNPJ do emitente do documento com o CNPJ consultado —
    "emitente" (nota emitida por ele) ou "destinatario" (nota recebida).
    """
    try:
        root = etree.fromstring(xml_bytes)
    except Exception:
        return {}
    ns = {"nfe": NS}

    def t(*tags):
        return root.findtext(".//nfe:" + "/nfe:".join(tags), namespaces=ns) or ""

    # resNFe usa nomes de tag próprios (CNPJ/xNome do emitente diretos);
    # procNFe usa a estrutura completa emit/dest.
    cnpj_emit = t("CNPJ") or t("emit", "CNPJ")
    x_nome    = t("xNome") or t("emit", "xNome")
    dh_emi    = t("dhEmi") or t("infNFe", "ide", "dhEmi")
    v_nf      = t("vNF") or t("infNFe", "total", "ICMSTot", "vNF")
    c_sit     = t("cSitNFe")
    ch_nfe    = t("chNFe")
    if not ch_nfe:
        inf_el = root.find(".//nfe:infNFe", namespaces=ns)
        if inf_el is not None:
            ch_nfe = (inf_el.get("Id", "") or "")[3:]  # remove prefixo "NFe"

    alvo = _so_numeros(cnpj_consultado)
    papel = "emitente" if _so_numeros(cnpj_emit)[:8] == alvo[:8] else "destinatario"

    return {
        "chave": ch_nfe, "cnpj_emit": cnpj_emit, "xNome_emit": x_nome,
        "dhEmi": dh_emi[:10] if dh_emi else "", "vNF": v_nf,
        "cSitNFe": c_sit, "papel": papel,
    }


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
                 chave_privada, certificado, modelo: int = 55) -> dict:
    """
    Registra evento de cancelamento (tpEvento=110111).
    Prazo: 24h após autorização (antes da circulação da mercadoria).

    modelo: 55 (NF-e) ou 65 (NFC-e) — o evento de cancelamento é enviado
    pelo autorizador do MESMO modelo da nota (a chave de acesso já indica
    o modelo, mas quem decide a rota é este parâmetro).
    """
    cuf      = _UF_IBGE[uf.upper()]
    dh_evento = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    id_evento = f"ID110111{chave}01"

    env = etree.Element(f"{{{NS}}}envEvento", versao="1.00", nsmap={None: NS})
    _sub(env, "idLote", "1")
    evento = etree.SubElement(env, f"{{{NS}}}evento", versao="1.00")
    inf_ev = etree.SubElement(evento, f"{{{NS}}}infEvento", Id=id_evento)
    _sub(inf_ev, "cOrgao",     cuf)
    _sub(inf_ev, "tpAmb",      _tp_amb())
    _sub(inf_ev, "CNPJ",       _so_numeros(cnpj))
    _sub(inf_ev, "chNFe",      chave)
    _sub(inf_ev, "dhEvento",   dh_evento)
    _sub(inf_ev, "tpEvento",   "110111")
    _sub(inf_ev, "nSeqEvento", "1")
    _sub(inf_ev, "verEvento",  "1.00")
    det = etree.SubElement(inf_ev, f"{{{NS}}}detEvento", versao="1.00")
    _sub(det, "descEvento", "Cancelamento")
    _sub(det, "nProt",      n_prot)
    _sub(det, "xJust",      justificativa)

    # A assinatura tem que ficar DENTRO de <evento> (irmã de infEvento), não
    # em <envEvento> — por isso assinamos o sub-elemento "evento", não o
    # envelope inteiro.
    evento_assinado = assinar_evento(evento, chave_privada, certificado)
    if evento_assinado is not evento:
        env.replace(evento, evento_assinado)
    env_str = etree.tostring(env, encoding="unicode")

    url  = (_url_servico_nfce(uf, "NFeRecepcaoEvento4") if modelo == 65
            else _url_servico(uf, "NFeRecepcaoEvento4"))
    resp = _enviar_soap(url, "NFeRecepcaoEvento4", cuf, env_str, cert_path, key_path)
    body = _extrair_body(resp)
    ns   = {"nfe": NS}
    # cStat do lote (retEnvEvento) vs. cStat do evento em si (infEvento) —
    # o segundo é o que diz se o cancelamento foi homologado ou não.
    cstat = body.findtext(".//nfe:infEvento/nfe:cStat",   namespaces=ns) \
         or body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:infEvento/nfe:xMotivo", namespaces=ns) \
         or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    print(f"[nfe] Cancelamento: cStat={cstat} | {xmot}", flush=True)
    return {"cStat": cstat, "xMotivo": xmot}


# ─── Manifestação do Destinatário ─────────────────────────────────
#
# Necessária pra liberar o XML completo (procNFe) de uma nota onde você é
# DESTINATÁRIO — a Distribuição DFe só devolve resumo (resNFe) até isso ser
# feito. Diferente do cancelamento: é o destinatário se manifestando sobre
# uma nota de outra empresa, então vai pro Ambiente Nacional (cOrgao=91),
# não pro autorizador da UF de ninguém.

_DESC_EVENTO_MANIFESTACAO = {
    "210200": "Confirmacao da Operacao",
    "210210": "Ciencia da Operacao",
    "210220": "Desconhecimento da Operacao",
    "210240": "Operacao nao Realizada",
}


def manifestar_destinatario(chave: str, cnpj: str, cert_path: str, key_path: str,
                            chave_privada, certificado,
                            tp_evento: str = "210210", justificativa: str = "") -> dict:
    """
    Registra a manifestação do destinatário sobre uma NF-e de terceiros.

    tp_evento:
      210210 = Ciência da Operação — não confirma nem nega nada, só
               desbloqueia o XML completo na próxima Distribuição DFe.
      210200 = Confirmação da Operação
      210220 = Desconhecimento da Operação (exige justificativa)
      210240 = Operação não Realizada (exige justificativa)

    Prazo da SEFAZ: até 180 dias da emissão pra manifestar (nacional).
    """
    if tp_evento not in _DESC_EVENTO_MANIFESTACAO:
        raise ValueError(f"tp_evento inválido: {tp_evento!r}")
    if tp_evento in ("210220", "210240") and len(justificativa.strip()) < 15:
        raise ValueError("Justificativa obrigatória (mín. 15 caracteres) para esse tipo de manifestação.")

    dh_evento = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    id_evento = f"ID{tp_evento}{chave}01"

    env = etree.Element(f"{{{NS}}}envEvento", versao="1.00", nsmap={None: NS})
    _sub(env, "idLote", "1")
    evento = etree.SubElement(env, f"{{{NS}}}evento", versao="1.00")
    inf_ev = etree.SubElement(evento, f"{{{NS}}}infEvento", Id=id_evento)
    _sub(inf_ev, "cOrgao",     _CUF_AN)
    _sub(inf_ev, "tpAmb",      _tp_amb())
    _sub(inf_ev, "CNPJ",       _so_numeros(cnpj))
    _sub(inf_ev, "chNFe",      chave)
    _sub(inf_ev, "dhEvento",   dh_evento)
    _sub(inf_ev, "tpEvento",   tp_evento)
    _sub(inf_ev, "nSeqEvento", "1")
    _sub(inf_ev, "verEvento",  "1.00")
    det = etree.SubElement(inf_ev, f"{{{NS}}}detEvento", versao="1.00")
    _sub(det, "descEvento", _DESC_EVENTO_MANIFESTACAO[tp_evento])
    if tp_evento in ("210220", "210240"):
        _sub(det, "xJust", justificativa.strip())

    evento_assinado = assinar_evento(evento, chave_privada, certificado)
    if evento_assinado is not evento:
        env.replace(evento, evento_assinado)
    env_str = etree.tostring(env, encoding="unicode")

    url  = _url_manifestacao_destinatario()
    resp = _enviar_soap(url, "NFeRecepcaoEvento4", _CUF_AN, env_str, cert_path, key_path)
    body = _extrair_body(resp)
    ns   = {"nfe": NS}
    cstat = body.findtext(".//nfe:infEvento/nfe:cStat",   namespaces=ns) \
         or body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:infEvento/nfe:xMotivo", namespaces=ns) \
         or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    print(f"[nfe] Manifestação destinatário: tpEvento={tp_evento} cStat={cstat} | {xmot}", flush=True)
    return {"cStat": cstat, "xMotivo": xmot}


# ─── Inutilização de Numeração ────────────────────────────────────

def inutilizar_numeracao(uf: str, cnpj: str, ano: int, modelo: int,
                          serie: int, nnf_ini: int, nnf_fin: int,
                          justificativa: str,
                          cert_path: str, key_path: str,
                          chave_privada, certificado) -> dict:
    """
    Inutiliza uma faixa de numeração de NF-e/NFC-e que nunca foi usada
    (NFeInutilizacao4) — obrigatório sempre que um número é pulado antes de
    ser autorizado (não dá pra simplesmente ignorar o furo na sequência).

    ano: ano de 4 dígitos (ex: 2026) — convertido para 2 dígitos no XML.
    modelo: 55 (NF-e) ou 65 (NFC-e).
    justificativa: mínimo de 15 caracteres (exigência da SEFAZ).
    """
    cuf  = _UF_IBGE[uf.upper()]
    cnpj_num = _so_numeros(cnpj)
    ano2 = ano % 100
    id_inut = (f"ID{cuf:02d}{ano2:02d}{cnpj_num:0>14}{modelo:02d}"
               f"{serie:03d}{nnf_ini:09d}{nnf_fin:09d}")

    inut = etree.Element(f"{{{NS}}}inutNFe", versao="4.00", nsmap={None: NS})
    inf  = etree.SubElement(inut, f"{{{NS}}}infInut", Id=id_inut)
    _sub(inf, "tpAmb",  _tp_amb())
    _sub(inf, "xServ",  "INUTILIZAR")
    _sub(inf, "cUF",    cuf)
    _sub(inf, "ano",    f"{ano2:02d}")
    _sub(inf, "CNPJ",   cnpj_num)
    _sub(inf, "mod",    modelo)
    _sub(inf, "serie",  serie)
    _sub(inf, "nNFIni", nnf_ini)
    _sub(inf, "nNFFin", nnf_fin)
    _sub(inf, "xJust",  justificativa)

    signer = _XMLSignerSEFAZ(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    inut_assinado = signer.sign(
        inut, key=chave_privada, cert=[certificado], reference_uri=id_inut,
    )
    inut_str = etree.tostring(inut_assinado, encoding="unicode")

    url  = (_url_servico_nfce(uf, "NFeInutilizacao4") if modelo == 65
            else _url_servico(uf, "NFeInutilizacao4"))
    resp = _enviar_soap(url, "NFeInutilizacao4", cuf, inut_str, cert_path, key_path)
    body = _extrair_body(resp)
    ns    = {"nfe": NS}
    cstat = body.findtext(".//nfe:infInut/nfe:cStat",   namespaces=ns) \
         or body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:infInut/nfe:xMotivo", namespaces=ns) \
         or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    print(f"[nfe] Inutilização: cStat={cstat} | {xmot}", flush=True)
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
    cstat = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:cStat",   namespaces=ns) \
         or body.findtext(".//nfe:cStat", namespaces=ns) or ""
    xmot  = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:xMotivo", namespaces=ns) \
         or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
    nprot = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:nProt",   namespaces=ns) \
         or body.findtext(".//nfe:nProt", namespaces=ns) or ""
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

        # 2. Montar NF-e
        print("[nfe] Montando XML da NF-e...", flush=True)
        nfe_element, chave = montar_nfe_xml(dados)

        print("[nfe] Assinando com XMLDSIG RSA-SHA1...", flush=True)
        nfe_assinada = assinar_nfe(nfe_element, chave_privada, certificado)

        # O schema da NFe exige a tag <Signature> — por isso a validação
        # só pode acontecer depois de assinado, nunca antes.
        print("[nfe] Validando XML contra schema XSD...", flush=True)
        from validador_xml import validar_ou_abortar
        validar_ou_abortar(nfe_assinada)

        nfe_str = etree.tostring(nfe_assinada, encoding="unicode")

        # 3. Enviar para autorização
        print("[nfe] Enviando para SEFAZ (síncrono)...", flush=True)
        body = _autorizar(nfe_assinada, uf, cuf, cert_path, key_path)

        # cStat aparece em dois níveis: o do lote (retEnviNFe, ex. 104=
        # "Lote processado" — não é aprovação nem rejeição) e o da NF-e em si,
        # dentro de protNFe/infProt. É esse segundo que importa.
        ns    = {"nfe": NS}
        cstat = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:cStat",   namespaces=ns) \
             or body.findtext(".//nfe:cStat", namespaces=ns) or ""
        xmot  = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:xMotivo", namespaces=ns) \
             or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
        nprot = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:nProt",   namespaces=ns) \
             or body.findtext(".//nfe:nProt", namespaces=ns) or ""
        print(f"[nfe] Resposta: cStat={cstat} | {xmot}", flush=True)

        if cstat not in ("100", "150"):
            raise RuntimeError(f"NF-e não autorizada: [{cstat}] {xmot}")

        print(f"[nfe] OK AUTORIZADA | chave={chave} | nProt={nprot}", flush=True)

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

def _montar_qrcode_nfce(url_base: str, chave: str, tp_amb: str,
                         id_csc: str, csc: str) -> str:
    """
    Monta o conteúdo do QR Code da NFC-e — formato "versão 2" (emissão
    online, tpEmis=1), que é o que a implementação de referência do mercado
    (nfephp-org/sped-nfe, usada por praticamente todo emissor real) gera:

        <url>?p=<chave>|2|<tpAmb>|<idCSC>|<hash>

    onde idCSC é numérico, SEM zero à esquerda, e:

        hash = SHA-1("<chave>|2|<tpAmb>|<idCSC>" + CSC), hex MAIÚSCULO.

    (O formato mais longo documentado no PDF da NT 2015.002 — com chNFe=,
    nVersao=100, dhEmi=, vNF= etc — bate no schema mas na prática trava com
    erro genérico no autorizador real da SEFAZ; não é esse o usado aqui.)
    """
    id_csc_num = str(int(id_csc))
    seq = f"{chave}|2|{tp_amb}|{id_csc_num}"
    hash_qr = hashlib.sha1((seq + csc).encode("utf-8")).hexdigest().upper()
    return f"{url_base}?p={seq}|{hash_qr}"


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

    nfe = etree.Element(f"{{{NS}}}NFe", nsmap={None: NS})
    inf = etree.SubElement(nfe, f"{{{NS}}}infNFe",
                           Id=f"NFe{chave}", versao="4.00")

    # ── Identificação ────────────────────────────────────────────────
    ide = _sub(inf, "ide")
    _sub(ide, "cUF",      cuf)
    _sub(ide, "cNF",      chave[35:43])
    _sub(ide, "natOp",    dados.get("nat_op", "Venda a Consumidor"))
    _sub(ide, "mod",      mod)
    _sub(ide, "serie",    str(serie))
    _sub(ide, "nNF",      str(nnf))
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
    _montar_nfref(ide, dados)

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

        # IBS/CBS — Reforma Tributária (ver comentário em _montar_ibscbs_item)
        _montar_ibscbs_item(imposto, item, v_prod)

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
    _montar_ibscbs_total(total, v_prod_total)

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
             dados.get("inf_adic") or "NFC-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL")

    # infNFeSupl (QR Code) NÃO entra aqui: pelo schema da NFe, ele é filho de
    # <NFe> (irmão de infNFe e de Signature), inserido só depois de assinar
    # — ver emitir_nfce(). Colocá-lo dentro de infNFe (como fazíamos antes)
    # quebra a validação XSD e entraria indevidamente no digest da assinatura.

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

        # 4. Montar QR Code — formato oficial da NT 2015.002 (chNFe=...&nVersao=100&...).
        # Os valores (vNF, vICMS, dhEmi, digVal) são lidos de volta do próprio
        url_consulta = _url_consulta_nfce(uf)   # campo urlChave
        url_qrcode   = _url_qrcode_nfce(uf)      # conteúdo do QR Code
        qr_content   = _montar_qrcode_nfce(url_qrcode, chave, tp_amb, id_csc, csc)

        # infNFeSupl é filho de <NFe>, mas pelo schema (leiauteNFe_v4.00.xsd)
        # a ordem exigida é infNFe → infNFeSupl → Signature — ou seja, ele
        # entra ANTES da assinatura, não depois. Insere com addprevious()
        # em vez de anexar no final (que deixaria Signature antes dele).
        # O _XMLSignerSEFAZ cria a tag <Signature> sem namespace explícito
        # (QName(None, ...), pra sair sem prefixo no XML) — por isso ela é
        # encontrada pelo nome puro, não pelo namespace do xmldsig.
        sig_el = nfe_assinada.find("Signature")
        supl = etree.Element(f"{{{NS}}}infNFeSupl")
        # Formato "p=chave|2|tpAmb|idCSC|hash" não tem "&", então não precisa
        # de CDATA (a referência nfephp também usa texto puro aqui).
        _sub(supl, "qrCode", qr_content)
        _sub(supl, "urlChave", url_consulta)
        sig_el.addprevious(supl)

        # Validação só agora, com a árvore completa e na ordem certa —
        # o schema exige a tag <Signature>, então precisa ser depois de assinar.
        print("[nfce] Validando XML contra schema XSD...", flush=True)
        from validador_xml import validar_ou_abortar
        validar_ou_abortar(nfe_assinada)

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
        cstat = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:cStat",   namespaces=ns) \
             or body.findtext(".//nfe:cStat", namespaces=ns) or ""
        xmot  = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:xMotivo", namespaces=ns) \
             or body.findtext(".//nfe:xMotivo", namespaces=ns) or ""
        nprot = body.findtext(".//nfe:protNFe/nfe:infProt/nfe:nProt",   namespaces=ns) \
             or body.findtext(".//nfe:nProt", namespaces=ns) or ""
        print(f"[nfce] Resposta: cStat={cstat} | {xmot}", flush=True)

        if cstat not in ("100", "150"):
            raise RuntimeError(f"NFC-e não autorizada: [{cstat}] {xmot}")

        print(f"[nfce] OK AUTORIZADA | chave={chave} | nProt={nprot}", flush=True)

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

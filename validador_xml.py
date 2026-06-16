"""
Validação do XML da NF-e / NFC-e contra os schemas XSD oficiais da SEFAZ.

Os schemas são baixados uma vez do repositório sped-nfe (espelho dos pacotes
oficiais SEFAZ) e cacheados localmente em schemas/.

Uso:
    from validador_xml import validar_nfe
    erros = validar_nfe(xml_bytes_ou_element)
    if erros:
        raise ValueError(erros[0])
"""
import os
import shutil
import requests
import xmlschema
from lxml import etree

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_SCHEMA_DIR  = os.path.join(_BASE_DIR, "schemas")

# Schemas oficiais — pacote PL_010_V1.30 (NF-e 4.00 mais recente)
_SCHEMA_BASE = (
    "https://raw.githubusercontent.com/nfephp-org/sped-nfe"
    "/master/schemes/PL_010_V1.30/"
)

# Schema principal — os demais são descobertos e baixados via API do GitHub
_SCHEMA_PRINCIPAL = "nfe_v4.00.xsd"
_GITHUB_API_TREE  = (
    "https://api.github.com/repos/nfephp-org/sped-nfe"
    "/git/trees/master?recursive=1"
)

_schema_cache: dict[str, xmlschema.XMLSchema] = {}


def _baixar_schemas():
    """Baixa todos os XSD do pacote PL_010_V1.30 para schemas/."""
    os.makedirs(_SCHEMA_DIR, exist_ok=True)
    # Marca de conclusão — evita re-baixar a cada restart
    flag = os.path.join(_SCHEMA_DIR, ".downloaded")
    if os.path.isfile(flag):
        return

    print("[validador] Baixando schemas XSD da SEFAZ (primeira vez)...", flush=True)
    try:
        resp = requests.get(_GITHUB_API_TREE, timeout=15)
        resp.raise_for_status()
        tree = resp.json().get("tree", [])
        xsds = [
            x["path"] for x in tree
            if x["path"].startswith("schemes/PL_010_V1.30/") and x["path"].endswith(".xsd")
        ]
    except Exception as e:
        print(f"[validador] AVISO: não foi possível listar schemas: {e}", flush=True)
        return

    ok = 0
    for path in xsds:
        nome = os.path.basename(path)
        dest = os.path.join(_SCHEMA_DIR, nome)
        if os.path.isfile(dest):
            ok += 1
            continue
        url = f"https://raw.githubusercontent.com/nfephp-org/sped-nfe/master/{path}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            ok += 1
        except Exception as e:
            print(f"[validador] AVISO: falha ao baixar {nome}: {e}", flush=True)

    if ok > 0:
        open(flag, "w").close()
        print(f"[validador] {ok} schemas baixados com sucesso.", flush=True)


def _get_schema(nome: str = _SCHEMA_PRINCIPAL) -> xmlschema.XMLSchema | None:
    """Retorna o schema compilado (com cache em memória)."""
    if nome in _schema_cache:
        return _schema_cache[nome]

    _baixar_schemas()
    path = os.path.join(_SCHEMA_DIR, nome)
    if not os.path.isfile(path):
        return None

    try:
        schema = xmlschema.XMLSchema(path, base_url=_SCHEMA_DIR)
        _schema_cache[nome] = schema
        return schema
    except Exception as e:
        print(f"[validador] Erro ao compilar schema {nome}: {e}", flush=True)
        return None


def _to_bytes(xml) -> bytes:
    """Aceita bytes, str ou lxml Element."""
    if isinstance(xml, (bytes, bytearray)):
        return bytes(xml)
    if isinstance(xml, str):
        return xml.encode("utf-8")
    return etree.tostring(xml, encoding="utf-8", xml_declaration=True)


def validar_nfe(xml, schema_nome: str = "nfe_v4.00.xsd") -> list[str]:
    """
    Valida o XML da NF-e / NFC-e contra o schema XSD.

    Retorna lista de mensagens de erro (vazia = válido).
    Se os schemas não puderem ser baixados, retorna lista vazia com aviso
    (não bloqueia a emissão por falta de conectividade).
    """
    schema = _get_schema(schema_nome)
    if schema is None:
        print("[validador] Schema indisponível — validação ignorada.", flush=True)
        return []

    xml_bytes = _to_bytes(xml)
    try:
        erros = []
        for err in schema.iter_errors(xml_bytes):
            erros.append(f"{err.path}: {err.reason}")
        return erros
    except Exception as e:
        print(f"[validador] Erro durante validação: {e}", flush=True)
        return []


def validar_ou_abortar(xml, schema_nome: str = "nfe_v4.00.xsd"):
    """
    Valida e levanta ValueError com o primeiro erro encontrado.
    Usado antes de assinar e enviar para a SEFAZ.
    """
    erros = validar_nfe(xml, schema_nome)
    if erros:
        raise ValueError(
            f"XML inválido ({len(erros)} erro(s)):\n" + "\n".join(erros[:5])
        )

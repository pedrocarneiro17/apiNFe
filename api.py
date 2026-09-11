"""
api.py — API REST de emissão de NF-e/NFC-e (modelo 55/65).

Autenticação: header `X-API-Key` (ou `?api_key=`) — mesma env API_KEY já
usada pelo endpoint de leitura `/api/notas` em webapp.py.

Emitentes são os mesmos "clientes" cadastrados na tela admin — não existe
uma tabela paralela: ao contrário de um cadastro "genérico" (que só guardaria
um certificado isolado), aqui o emitente já precisa do perfil fiscal completo
(endereço, IE, CRT) pra emitir, então cadastrar via API ou via UI edita o
mesmo registro.

Fluxo:
  1. (uma vez)     POST /api/v1/emitentes            → cadastra cadastral + .pfx
  2. (a cada nota) POST /api/v1/nfe | /api/v1/nfce    → emite (Idempotency-Key obrigatório)
  3. (se precisar) POST /api/v1/notas/<chave>/cancelar
                   POST /api/v1/inutilizacoes
                   POST /api/v1/danfe                 → PDF a partir da chave

Idempotência: diferente de outros documentos fiscais que não têm numeração
sequencial, aqui cada emissão consome um número real (nNF) — um retry sem
proteção poderia furar a numeração. Por isso toda emissão exige o header
`Idempotency-Key` (escolhido pelo chamador, ex: id do pedido dele): reenviar
com a mesma chave devolve a nota já emitida, sem consumir número novo.
"""
import os
import re
import json
import time
import base64
import shutil
import tempfile
import uuid
import traceback
import datetime
from functools import wraps

from flask import Blueprint, request, jsonify

import db

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


# ── Infra: request id, log, auth ─────────────────────────────────

def _req_id() -> str:
    return uuid.uuid4().hex[:8]


def _log(req_id, msg):
    print(f"[api] {req_id} {msg}", flush=True)


def _registrar_log(req_id, metodo, endpoint, emitente_id, status, sucesso, erro, dur):
    """Grava o registro da chamada (nunca deixa uma falha de log quebrar a API)."""
    try:
        db.registrar_api_log({
            "request_id": req_id, "metodo": metodo, "endpoint": endpoint,
            "emitente_id": emitente_id, "status_code": status,
            "sucesso": sucesso, "erro": (erro or "")[:1000] or None,
            "duracao_ms": dur,
        })
    except Exception as e:
        print(f"[api] falha ao registrar log: {e}", flush=True)


def _com_log(f):
    """Registra toda chamada da API (sucesso e erro) para auditoria/diagnóstico."""
    @wraps(f)
    def dec(*a, **k):
        t0 = time.time()
        endpoint, metodo = request.path, request.method
        body_in = request.get_json(silent=True) or {}
        emitente_id = (body_in.get("emitente_id") or request.form.get("id")
                       or request.args.get("emitente_id"))
        try:
            resp = f(*a, **k)
        except Exception as e:
            _registrar_log(None, metodo, endpoint, emitente_id, 500, False,
                            str(e), int((time.time() - t0) * 1000))
            raise
        body_obj, status = (resp[0], resp[1]) if isinstance(resp, tuple) else (resp, 200)
        req_id, sucesso, erro = None, status < 400, None
        try:
            data = body_obj.get_json(silent=True) if hasattr(body_obj, "get_json") else None
            if isinstance(data, dict):
                req_id = data.get("request_id")
                sucesso = bool(data.get("ok")) if "ok" in data else sucesso
                erro = data.get("erro")
        except Exception:
            pass
        _registrar_log(req_id, metodo, endpoint, emitente_id, status, sucesso, erro,
                        int((time.time() - t0) * 1000))
        return resp
    return dec


def _requer_api_key(f):
    @wraps(f)
    def dec(*a, **k):
        api_key = os.environ.get("API_KEY", "")
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not api_key or key != api_key:
            return jsonify({"ok": False, "erro": "Não autorizado"}), 401
        return f(*a, **k)
    return dec


def _erro(msg, code=400, req_id=None):
    payload = {"ok": False, "erro": msg}
    if req_id:
        payload["request_id"] = req_id
    return jsonify(payload), code


def _so_numeros(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _certs_path() -> str:
    p = os.environ.get("CERTS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "certs")
    os.makedirs(p, exist_ok=True)
    return p


def _resolver_cert(caminho: str) -> str:
    if os.path.isabs(caminho) and os.path.isfile(caminho):
        return caminho
    base = _certs_path()
    candidato = os.path.join(base, os.path.basename(caminho))
    return candidato if os.path.isfile(candidato) else caminho


def _modelo_da_chave(chave: str) -> int:
    """O modelo (55/65) já está codificado na própria chave de acesso
    (posições 21-22 do padrão de 44 dígitos: cUF+AAMM+CNPJ+mod+...) — evita
    pedir de novo ao chamador em cancelamento/danfe."""
    try:
        return int(chave[20:22])
    except (ValueError, IndexError):
        return 55


# ── Emitentes (cadastro + certificado) ───────────────────────────

_CAMPOS_CLIENTE = ("razao_social", "xFant", "cnpj", "ie", "crt", "uf", "cuf",
                    "cep", "xLgr", "nro", "xCpl", "xBairro", "cMun", "xMun",
                    "fone", "serie", "id_csc", "csc")


@api_bp.route("/emitentes", methods=["POST"])
@_com_log
@_requer_api_key
def cadastrar_emitente():
    emitente_id = (request.form.get("id") or "").strip()
    if not emitente_id:
        return _erro("Campo 'id' é obrigatório.")

    try:
        dados = json.loads(request.form.get("dados") or "{}")
    except Exception:
        return _erro("Campo 'dados' precisa ser um JSON válido (string).")

    senha = request.form.get("senha", "")
    arq = request.files.get("cert")
    existente = db.carregar_cliente(emitente_id) or {}

    caminho_certificado = existente.get("caminho_certificado", "")
    if arq:
        if not arq.filename.lower().endswith(".pfx"):
            return _erro("Envie o arquivo .pfx no campo 'cert' (multipart/form-data).")
        pfx_bytes = arq.read()
        # Valida a senha abrindo o PFX antes de salvar (mesmo padrão de
        # validação usado no cadastro de certificado do projeto NFS-e).
        tmp = tempfile.NamedTemporaryFile(suffix=".pfx", delete=False)
        try:
            tmp.write(pfx_bytes)
            tmp.close()
            from fluxo_nfe_api import _pfx_para_pem
            _cp, _kp, tmp_dir, _priv, _cert = _pfx_para_pem(tmp.name, senha)
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            return _erro(f"Não foi possível abrir o certificado "
                         f"(senha incorreta ou arquivo inválido): {e}")
        finally:
            os.remove(tmp.name)
        nome_arquivo = f"{emitente_id}.pfx"
        with open(os.path.join(_certs_path(), nome_arquivo), "wb") as f:
            f.write(pfx_bytes)
        caminho_certificado = nome_arquivo

    cliente_dados = {campo: dados.get(campo, existente.get(campo, "")) for campo in _CAMPOS_CLIENTE}
    cliente_dados["cnpj"]  = _so_numeros(cliente_dados.get("cnpj", ""))
    cliente_dados["ie"]    = _so_numeros(cliente_dados.get("ie", ""))
    cliente_dados["fone"]  = _so_numeros(cliente_dados.get("fone", ""))
    cliente_dados["cep"]   = _so_numeros(cliente_dados.get("cep", ""))
    cliente_dados["uf"]    = (cliente_dados.get("uf") or "").upper()
    cliente_dados["crt"]   = int(cliente_dados.get("crt") or 1)
    cliente_dados["cuf"]   = int(cliente_dados.get("cuf") or 0)
    cliente_dados["serie"] = int(cliente_dados.get("serie") or 1)
    cliente_dados["caminho_certificado"] = caminho_certificado
    cliente_dados["senha_certificado"]   = senha or existente.get("senha_certificado", "")

    db.salvar_cliente(emitente_id, cliente_dados)
    return jsonify({"ok": True, "id": emitente_id})


@api_bp.route("/emitentes", methods=["GET"])
@_com_log
@_requer_api_key
def listar_emitentes():
    clientes = db.listar_clientes()
    for c in clientes:
        c.pop("senha_certificado", None)
    return jsonify({"ok": True, "emitentes": clientes})


@api_bp.route("/emitentes/<emitente_id>", methods=["GET"])
@_com_log
@_requer_api_key
def obter_emitente(emitente_id):
    c = db.carregar_cliente(emitente_id)
    if not c:
        return _erro("Emitente não encontrado.", 404)
    c.pop("senha_certificado", None)
    return jsonify({"ok": True, "emitente": c})


@api_bp.route("/emitentes/<emitente_id>", methods=["DELETE"])
@_com_log
@_requer_api_key
def excluir_emitente(emitente_id):
    db.deletar_cliente(emitente_id)
    return jsonify({"ok": True})


# ── Emissão de NF-e / NFC-e ───────────────────────────────────────

_CAMPOS_ITEM_OBRIG = ("cProd", "xProd", "qCom", "vUnCom", "CFOP", "NCM")


def _validar_itens(itens):
    if not itens or not isinstance(itens, list):
        return "Campo 'itens' precisa ser uma lista não vazia."
    for i, item in enumerate(itens):
        faltando = [c for c in _CAMPOS_ITEM_OBRIG if not item.get(c)]
        if faltando:
            return f"Item {i + 1}: campos ausentes {faltando}"
    return None


def _montar_dados_emissao_api(cliente: dict, body: dict, modelo: int, nnf: int) -> dict:
    dest   = body.get("destinatario", {}) or {}
    pag    = body.get("pagamento", {}) or {}
    transp = body.get("transporte", {}) or {}
    return {
        "caminho_certificado": _resolver_cert(cliente.get("caminho_certificado", "")),
        "senha_certificado":   cliente.get("senha_certificado", ""),

        "uf":               cliente.get("uf", ""),
        "cnpj_emitente":    cliente.get("cnpj", ""),
        "ie_emitente":      cliente.get("ie", ""),
        "xNome_emitente":   cliente.get("razao_social", ""),
        "xFant_emitente":   cliente.get("xFant", ""),
        "crt":              int(cliente.get("crt", 1)),
        "xLgr_emitente":    cliente.get("xLgr", ""),
        "nro_emitente":     cliente.get("nro", ""),
        "xCpl_emitente":    cliente.get("xCpl", ""),
        "xBairro_emitente": cliente.get("xBairro", ""),
        "cMun_emitente":    cliente.get("cMun", ""),
        "xMun_emitente":    cliente.get("xMun", ""),
        "cep_emitente":     cliente.get("cep", ""),
        "fone_emitente":    cliente.get("fone", ""),
        "cuf":              int(cliente.get("cuf", 0)),

        "serie":     int(cliente.get("serie", 1)),
        "nnf":       nnf,
        "nat_op":    body.get("nat_op", "Venda" if modelo == 55 else "Venda a Consumidor"),
        "tp_nf":     body.get("tp_nf", "1"),
        "id_dest":   body.get("id_dest", "1"),
        "ind_final": body.get("ind_final", "1" if modelo == 65 else "0"),
        "ind_pres":  body.get("ind_pres", "1"),
        "mod_frete": transp.get("mod_frete", "9"),
        "tp_pag":    pag.get("tp_pag", "01"),
        "fin_nfe":   body.get("fin_nfe", "1"),
        "ref_nfe":   _so_numeros(body.get("ref_nfe", "")),

        "cnpj_destinatario":    _so_numeros(dest.get("cnpj", "")),
        "cpf_destinatario":     _so_numeros(dest.get("cpf", "")),
        "xNome_destinatario":   dest.get("nome") or ("CONSUMIDOR NAO IDENTIFICADO" if modelo == 65 else ""),
        "ie_destinatario":      dest.get("ie", ""),
        "xLgr_destinatario":    dest.get("xLgr", ""),
        "nro_destinatario":     dest.get("nro", ""),
        "xCpl_destinatario":    dest.get("xCpl", ""),
        "xBairro_destinatario": dest.get("xBairro", ""),
        "cMun_destinatario":    dest.get("cMun", ""),
        "xMun_destinatario":    dest.get("xMun", ""),
        "uf_destinatario":      dest.get("uf", ""),
        "cep_destinatario":     _so_numeros(dest.get("cep", "")),
        "email_destinatario":   dest.get("email", ""),

        "itens": body.get("itens", []),

        "vNF":    float(body.get("v_nf", 0)),
        "vDesc":  str(body.get("v_desc", "0.00")),
        "vFrete": str(body.get("v_frete", "0.00")),

        "inf_adic": body.get("inf_adic", ""),
        "modelo":   modelo,

        "id_csc": cliente.get("id_csc", "000001"),
        "csc":    cliente.get("csc", ""),
    }


def _resposta_emissao(req_id, registro, nota, ja_existia):
    xml_b64 = pdf_b64 = ""
    xml_path = (nota or {}).get("arquivo_xml", "")
    try:
        if xml_path and os.path.isfile(xml_path):
            with open(xml_path, "rb") as f:
                xml_b64 = base64.b64encode(f.read()).decode("ascii")
        if nota:
            modelo = int(nota.get("modelo", 55))
            if modelo == 65:
                from danfe_nfce import gerar_cupom as _gerar_pdf
            else:
                from danfe import gerar_danfe as _gerar_pdf
            pdf_b64 = base64.b64encode(_gerar_pdf(nota)).decode("ascii")
    except Exception as e:
        _log(req_id, f"aviso: falha ao preparar arquivos: {e}")

    resp = {
        "ok": True, "request_id": req_id,
        "chave_acesso": registro.get("chave", ""),
        "protocolo":    registro.get("n_prot", ""),
        "numero":       (nota or {}).get("n_nfe"),
        "xml_base64":   xml_b64,
        "danfe_base64": pdf_b64,
    }
    if ja_existia:
        resp["nota_existente"] = True
        resp["aviso"] = ("Já existe uma nota emitida com essa Idempotency-Key — "
                          "devolvendo a nota existente em vez de emitir duplicata.")
    return resp


def _emitir(modelo: int):
    req_id = _req_id()
    body = request.get_json(silent=True)
    if not body:
        return _erro("Corpo JSON inválido ou ausente.", req_id=req_id)

    idem_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    if not idem_key:
        return _erro("Header 'Idempotency-Key' é obrigatório (evita duplicar a "
                      "numeração em caso de reenvio).", req_id=req_id)

    emitente_id = body.get("emitente_id", "")
    cliente = db.carregar_cliente(emitente_id)
    if not cliente:
        return _erro(f"Emitente '{emitente_id}' não encontrado.", 404, req_id)
    if not cliente.get("caminho_certificado"):
        return _erro("Emitente sem certificado digital cadastrado.", req_id=req_id)

    erro_itens = _validar_itens(body.get("itens"))
    if erro_itens:
        return _erro(erro_itens, req_id=req_id)

    # Idempotência: mesma (emitente, key) já emitida antes? Devolve sem
    # consumir número novo — protege contra retry duplicando a numeração.
    existente = db.buscar_emissao_idempotente(emitente_id, idem_key)
    if existente:
        nota = db.get_nota(existente["nota_id"]) if existente.get("nota_id") else None
        _log(req_id, f"emitir BLOQUEADO (idempotência) | key={idem_key} | "
                      f"chave={existente.get('chave')}")
        return jsonify(_resposta_emissao(req_id, existente, nota, ja_existia=True))

    nnf = db.proximo_numero_nfe(emitente_id, modelo=modelo)
    dados_emissao = _montar_dados_emissao_api(cliente, body, modelo, nnf)

    nota_dados = {
        "cliente_id": emitente_id, "n_nfe": nnf, "serie": int(cliente.get("serie", 1)),
        "nat_op": dados_emissao["nat_op"], "tp_nf": dados_emissao["tp_nf"],
        "id_dest": dados_emissao["id_dest"], "ind_final": dados_emissao["ind_final"],
        "ind_pres": dados_emissao["ind_pres"], "mod_frete": dados_emissao["mod_frete"],
        "tp_pag": dados_emissao["tp_pag"],
        "cnpj_dest": dados_emissao["cnpj_destinatario"], "cpf_dest": dados_emissao["cpf_destinatario"],
        "xNome_dest": dados_emissao["xNome_destinatario"], "ie_dest": dados_emissao["ie_destinatario"],
        "xLgr_dest": dados_emissao["xLgr_destinatario"], "nro_dest": dados_emissao["nro_destinatario"],
        "xCpl_dest": dados_emissao["xCpl_destinatario"], "xBairro_dest": dados_emissao["xBairro_destinatario"],
        "cMun_dest": dados_emissao["cMun_destinatario"], "xMun_dest": dados_emissao["xMun_destinatario"],
        "uf_dest": dados_emissao["uf_destinatario"], "cep_dest": dados_emissao["cep_destinatario"],
        "email_dest": dados_emissao["email_destinatario"],
        "itens": dados_emissao["itens"], "inf_adic": dados_emissao["inf_adic"],
        "v_nf": dados_emissao["vNF"], "v_desc": float(dados_emissao["vDesc"] or 0),
        "v_frete": float(dados_emissao["vFrete"] or 0), "modelo": modelo,
        "fin_nfe": dados_emissao["fin_nfe"], "ref_nfe": dados_emissao["ref_nfe"],
    }
    nota_id = db.criar_nota(nota_dados)
    db.update_nota_status(nota_id, "emitindo")

    _log(req_id, f"emitir | emitente={emitente_id} | modelo={modelo} | "
                  f"nnf={nnf} | nota_id={nota_id}")

    try:
        import fluxo_nfe_api
        resultado = (fluxo_nfe_api.emitir_nfce(dados_emissao) if modelo == 65
                     else fluxo_nfe_api.emitir_nfe(dados_emissao))
    except Exception as e:
        db.update_nota_status(nota_id, "erro", str(e))
        _log(req_id, f"emitir ERRO | nota_id={nota_id} | {e}")
        traceback.print_exc()
        return _erro(
            f"{e} — número {nnf} (série {cliente.get('serie', 1)}) pode ter sido "
            f"reservado sem autorização; se não for reenviado com a mesma "
            f"Idempotency-Key, pode ser necessário solicitar inutilização desse número.",
            502, req_id,
        )

    db.update_nota_emitida(nota_id, resultado["chave"], resultado["n_prot"], resultado["xml_path"])
    db.registrar_emissao_idempotente(
        emitente_id, idem_key, modelo, resultado["chave"], resultado["n_prot"],
        nota_id, resultado["xml_path"],
    )
    _log(req_id, f"emitir OK | nota_id={nota_id} | chave={resultado['chave']}")

    nota = db.get_nota(nota_id)
    return jsonify(_resposta_emissao(
        req_id, {"chave": resultado["chave"], "n_prot": resultado["n_prot"]},
        nota, ja_existia=False,
    ))


@api_bp.route("/nfe", methods=["POST"])
@_com_log
@_requer_api_key
def emitir_nfe_endpoint():
    return _emitir(55)


@api_bp.route("/nfce", methods=["POST"])
@_com_log
@_requer_api_key
def emitir_nfce_endpoint():
    return _emitir(65)


# ── Cancelamento ──────────────────────────────────────────────────

@api_bp.route("/notas/<chave_acesso>/cancelar", methods=["POST"])
@_com_log
@_requer_api_key
def cancelar_endpoint(chave_acesso):
    req_id = _req_id()
    body = request.get_json(silent=True) or {}
    emitente_id = body.get("emitente_id", "")
    justificativa = (body.get("justificativa") or "").strip()
    if len(justificativa) < 15:
        return _erro("Campo 'justificativa' deve ter ao menos 15 caracteres.", req_id=req_id)

    nota = db.get_nota_por_chave(chave_acesso)
    if not nota:
        return _erro("Nota não encontrada.", 404, req_id)
    if emitente_id and str(nota.get("cliente_id")) != str(emitente_id):
        return _erro("Chave não pertence ao emitente informado.", 404, req_id)
    if nota.get("status") != "emitido":
        return _erro("Só é possível cancelar notas emitidas.", req_id=req_id)

    modelo = _modelo_da_chave(chave_acesso)
    _log(req_id, f"cancelar | chave={chave_acesso} | modelo={modelo}")

    try:
        from fluxo_nfe_api import cancelar_nfe, _pfx_para_pem
        caminho_pfx = _resolver_cert(nota["caminho_certificado"])
        cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
            caminho_pfx, nota["senha_certificado"])
        try:
            resultado = cancelar_nfe(
                chave=nota["chave"], n_prot=nota["n_prot"], justificativa=justificativa,
                uf=nota["uf"], cnpj=nota["cnpj_emit"],
                cert_path=cert_path, key_path=key_path,
                chave_privada=chave_privada, certificado=certificado, modelo=modelo,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        _log(req_id, f"cancelar ERRO: {e}")
        traceback.print_exc()
        return _erro(str(e), 502, req_id)

    if resultado["cStat"] not in ("101", "135"):
        db.update_nota_status(nota["id"], "erro",
                               f"Cancelamento recusado [{resultado['cStat']}]: {resultado['xMotivo']}")
        return _erro(f"Cancelamento recusado [{resultado['cStat']}]: {resultado['xMotivo']}", 502, req_id)

    db.update_nota_cancelada(nota["id"])
    _log(req_id, f"cancelar OK | chave={chave_acesso}")
    return jsonify({"ok": True, "request_id": req_id,
                    "cStat": resultado["cStat"], "xMotivo": resultado["xMotivo"]})


# ── Inutilização de numeração ─────────────────────────────────────

@api_bp.route("/inutilizacoes", methods=["POST"])
@_com_log
@_requer_api_key
def inutilizar_endpoint():
    req_id = _req_id()
    body = request.get_json(silent=True) or {}
    emitente_id = body.get("emitente_id", "")
    cliente = db.carregar_cliente(emitente_id)
    if not cliente:
        return _erro(f"Emitente '{emitente_id}' não encontrado.", 404, req_id)
    if not cliente.get("caminho_certificado"):
        return _erro("Emitente sem certificado digital cadastrado.", req_id=req_id)

    modelo  = int(body.get("modelo", 55))
    serie   = int(body.get("serie", 1))
    ano     = int(body.get("ano") or datetime.datetime.now().year)
    nnf_ini = int(body.get("nnf_ini", 0))
    nnf_fin = int(body.get("nnf_fin", nnf_ini))
    justificativa = (body.get("justificativa") or "").strip()

    if len(justificativa) < 15:
        return _erro("Campo 'justificativa' deve ter ao menos 15 caracteres.", req_id=req_id)
    if nnf_fin < nnf_ini:
        return _erro("'nnf_fin' não pode ser menor que 'nnf_ini'.", req_id=req_id)

    _log(req_id, f"inutilizar | emitente={emitente_id} | modelo={modelo} | "
                  f"serie={serie} | {nnf_ini}-{nnf_fin}")

    from fluxo_nfe_api import inutilizar_numeracao as _inutilizar, _pfx_para_pem
    caminho_pfx = _resolver_cert(cliente["caminho_certificado"])
    cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
        caminho_pfx, cliente.get("senha_certificado", ""))
    try:
        resultado = _inutilizar(
            uf=cliente["uf"], cnpj=cliente["cnpj"], ano=ano, modelo=modelo,
            serie=serie, nnf_ini=nnf_ini, nnf_fin=nnf_fin, justificativa=justificativa,
            cert_path=cert_path, key_path=key_path,
            chave_privada=chave_privada, certificado=certificado,
        )
    except Exception as e:
        _log(req_id, f"inutilizar ERRO: {e}")
        traceback.print_exc()
        return _erro(str(e), 502, req_id)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if resultado.get("cStat") != "102":
        return _erro(f"Inutilização recusada [{resultado['cStat']}]: {resultado['xMotivo']}", 502, req_id)

    _log(req_id, f"inutilizar OK | {nnf_ini}-{nnf_fin}")
    return jsonify({"ok": True, "request_id": req_id,
                    "cStat": resultado["cStat"], "xMotivo": resultado["xMotivo"]})


# ── DANFE / Cupom a partir da chave ───────────────────────────────

@api_bp.route("/danfe", methods=["POST"])
@_com_log
@_requer_api_key
def danfe_endpoint():
    req_id = _req_id()
    body = request.get_json(silent=True) or {}
    chave = body.get("chave_acesso", "")
    if not chave:
        return _erro("Informe 'chave_acesso'.", req_id=req_id)

    nota = db.get_nota_por_chave(chave)
    if not nota:
        return _erro("Nota não encontrada.", 404, req_id)

    try:
        modelo = int(nota.get("modelo", 55))
        if modelo == 65:
            from danfe_nfce import gerar_cupom as _gerar_pdf
        else:
            from danfe import gerar_danfe as _gerar_pdf
        pdf = _gerar_pdf(nota)
    except Exception as e:
        _log(req_id, f"danfe ERRO: {e}")
        traceback.print_exc()
        return _erro(str(e), 500, req_id)

    return jsonify({"ok": True, "request_id": req_id,
                    "danfe_base64": base64.b64encode(pdf).decode("ascii")})

"""
Emissor NF-e — Painel Web (Flask).
Gerencia clientes (emitentes), produtos e notas fiscais modelo 55.
"""
import os
import re
import json
import threading
from functools import wraps
from datetime import datetime, timedelta

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, abort, send_file)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "nfe_webapp_2025")
app.permanent_session_lifetime = timedelta(days=30)

from api import api_bp
app.register_blueprint(api_bp)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")

def _init_app():
    try:
        db.init_db()
    except Exception as e:
        print(f"[startup] AVISO: init_db falhou — {e}", flush=True)

with app.app_context():
    _init_app()


def _so_numeros(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _certs_path() -> str:
    p = os.environ.get("CERTS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "certs"
    )
    os.makedirs(p, exist_ok=True)
    return p


def _requer_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logado"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("admin_notas"))


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        if (request.form.get("usuario") == ADMIN_USER and
                request.form.get("senha") == ADMIN_PASS):
            session.permanent = True
            session["logado"] = True
            return redirect(url_for("admin_notas"))
        erro = "Usuário ou senha incorretos."
    return render_template("admin/login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Notas ─────────────────────────────────────────────────────────

@app.route("/admin/notas")
@_requer_login
def admin_notas():
    cliente_id = request.args.get("cliente_id", "")
    status     = request.args.get("status", "")
    notas      = db.listar_notas(
        cliente_id=cliente_id or None,
        status=status or None,
    )
    clientes = db.listar_clientes()
    return render_template("admin/notas.html",
                           notas=notas, clientes=clientes,
                           filtro_cliente=cliente_id, filtro_status=status)


@app.route("/admin/notas/nova", methods=["GET", "POST"])
@_requer_login
def admin_nova_nota():
    clientes = db.listar_clientes()
    produtos  = db.listar_produtos()

    if request.method == "GET":
        return render_template("admin/nova_nota.html",
                               clientes=clientes, produtos=produtos)

    # POST — receber JSON do formulário
    dados = request.get_json(force=True) or {}

    cliente_id = dados.get("cliente_id", "")
    cliente    = db.carregar_cliente(cliente_id)
    if not cliente:
        return jsonify({"erro": "Emitente não encontrado"}), 404

    n_nfe = db.proximo_numero_nfe(cliente_id, modelo=55)

    nota_dados = {
        "cliente_id":   cliente_id,
        "n_nfe":        n_nfe,
        "serie":        int(cliente.get("serie", 1)),
        "nat_op":       dados.get("nat_op", "Venda"),
        "tp_nf":        dados.get("tp_nf", "1"),
        "id_dest":      dados.get("id_dest", "1"),
        "ind_final":    dados.get("ind_final", "0"),
        "ind_pres":     dados.get("ind_pres", "1"),
        "mod_frete":    dados.get("mod_frete", "9"),
        "tp_pag":       dados.get("tp_pag", "01"),
        "cnpj_dest":    _so_numeros(dados.get("cnpj_dest", "")),
        "cpf_dest":     _so_numeros(dados.get("cpf_dest", "")),
        "xNome_dest":   dados.get("xNome_dest", ""),
        "ie_dest":      dados.get("ie_dest", ""),
        "xLgr_dest":    dados.get("xLgr_dest", ""),
        "nro_dest":     dados.get("nro_dest", ""),
        "xCpl_dest":    dados.get("xCpl_dest", ""),
        "xBairro_dest": dados.get("xBairro_dest", ""),
        "cMun_dest":    dados.get("cMun_dest", ""),
        "xMun_dest":    dados.get("xMun_dest", ""),
        "uf_dest":      dados.get("uf_dest", ""),
        "cep_dest":     _so_numeros(dados.get("cep_dest", "")),
        "email_dest":   dados.get("email_dest", ""),
        "itens":        dados.get("itens", []),
        "inf_adic":     dados.get("inf_adic", ""),
        "v_nf":         float(dados.get("v_nf", 0)),
        "v_desc":       float(dados.get("v_desc", 0)),
        "v_frete":      float(dados.get("v_frete", 0)),
        "modelo":       55,
        "fin_nfe":      dados.get("fin_nfe", "1"),
        "ref_nfe":      _so_numeros(dados.get("ref_nfe", "")),
    }

    nota_id = db.criar_nota(nota_dados)
    return jsonify({"ok": True, "nota_id": nota_id})


@app.route("/admin/notas/<int:nota_id>/emitir", methods=["POST"])
@_requer_login
def admin_emitir_nota(nota_id):
    nota = db.get_nota(nota_id)
    if not nota:
        return jsonify({"erro": "Nota não encontrada"}), 404
    if nota["status"] == "emitindo":
        return jsonify({"erro": "Já está emitindo"}), 400
    if nota["status"] == "emitido":
        return jsonify({"erro": "Nota já emitida"}), 400

    db.update_nota_status(nota_id, "emitindo")

    def tarefa():
        try:
            import fluxo_nfe_api
            dados_emissao = _montar_dados_emissao(nota)
            modelo = int(nota.get("modelo", 55))
            if modelo == 65:
                resultado = fluxo_nfe_api.emitir_nfce(dados_emissao)
            else:
                resultado = fluxo_nfe_api.emitir_nfe(dados_emissao)
            db.update_nota_emitida(
                nota_id,
                resultado["chave"],
                resultado["n_prot"],
                resultado["xml_path"],
            )
            print(f"[emitir] OK nota={nota_id} chave={resultado['chave']}", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            db.update_nota_status(nota_id, "erro", str(e))
            print(f"[emitir] ERRO nota={nota_id} ERRO: {e}", flush=True)

    threading.Thread(target=tarefa, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/admin/notas/<int:nota_id>/cancelar", methods=["POST"])
@_requer_login
def admin_cancelar_nota(nota_id):
    nota = db.get_nota(nota_id)
    if not nota:
        return jsonify({"erro": "Nota não encontrada"}), 404
    if nota["status"] != "emitido":
        return jsonify({"erro": "Só é possível cancelar notas emitidas"}), 400

    body = request.get_json(force=True) or {}
    justificativa = body.get("justificativa", "").strip()
    if len(justificativa) < 15:
        return jsonify({"erro": "Justificativa deve ter ao menos 15 caracteres"}), 400

    def tarefa():
        try:
            from fluxo_nfe_api import cancelar_nfe, _pfx_para_pem
            import shutil

            caminho_pfx = _resolver_cert(nota["caminho_certificado"])
            cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
                caminho_pfx, nota["senha_certificado"]
            )
            try:
                resultado = cancelar_nfe(
                    chave=nota["chave"],
                    n_prot=nota["n_prot"],
                    justificativa=justificativa,
                    uf=nota["uf"],
                    cnpj=nota["cnpj_emit"],
                    cert_path=cert_path,
                    key_path=key_path,
                    chave_privada=chave_privada,
                    certificado=certificado,
                )
                if resultado["cStat"] in ("101", "135"):
                    db.update_nota_cancelada(nota_id)
                    print(f"[cancelar] OK nota={nota_id} cancelada", flush=True)
                else:
                    db.update_nota_status(nota_id, "erro",
                        f"Cancelamento recusado [{resultado['cStat']}]: {resultado['xMotivo']}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            db.update_nota_status(nota_id, "emitido", f"Erro no cancelamento: {e}")

    threading.Thread(target=tarefa, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/admin/notas/<int:nota_id>/reiniciar", methods=["POST"])
@_requer_login
def admin_reiniciar_nota(nota_id):
    db.update_nota_status(nota_id, "pendente", None)
    return jsonify({"ok": True})


@app.route("/admin/notas/<int:nota_id>/excluir", methods=["POST"])
@_requer_login
def admin_excluir_nota(nota_id):
    db.excluir_nota(nota_id)
    return jsonify({"ok": True})


@app.route("/admin/notas/<int:nota_id>/xml")
@_requer_login
def admin_download_xml(nota_id):
    nota = db.get_nota(nota_id)
    if not nota or not nota.get("arquivo_xml"):
        abort(404)
    caminho = nota["arquivo_xml"]
    if not os.path.isfile(caminho):
        abort(404)
    return send_file(caminho, as_attachment=True)


@app.route("/admin/notas/<int:nota_id>/pdf")
@_requer_login
def admin_download_pdf(nota_id):
    nota = db.get_nota(nota_id)
    if not nota:
        abort(404)
    from danfe import gerar_danfe
    pdf_bytes = gerar_danfe(nota)
    from flask import Response
    chave = nota.get("chave") or f"NF{nota_id}"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=DANFE_{chave}.pdf"},
    )


@app.route("/admin/notas/<int:nota_id>/status")
@_requer_login
def admin_nota_status(nota_id):
    nota = db.get_nota(nota_id)
    if not nota:
        return jsonify({"erro": "Não encontrada"}), 404
    return jsonify({
        "status":     nota["status"],
        "observacao": nota.get("observacao", ""),
        "chave":      nota.get("chave", ""),
        "n_prot":     nota.get("n_prot", ""),
    })


# ── Clientes ──────────────────────────────────────────────────────

@app.route("/admin/clientes")
@_requer_login
def admin_clientes():
    clientes = db.listar_clientes()
    return render_template("admin/clientes.html", clientes=clientes)


@app.route("/admin/clientes/salvar", methods=["POST"])
@_requer_login
def admin_salvar_cliente():
    nome = request.form.get("id", "").strip()
    if not nome:
        return jsonify({"erro": "ID obrigatório"}), 400
    existente = db.carregar_cliente(nome) or {}
    dados = {
        "razao_social": request.form.get("razao_social", ""),
        "xFant":        request.form.get("xFant", ""),
        "cnpj":         _so_numeros(request.form.get("cnpj", "")),
        "ie":           _so_numeros(request.form.get("ie", "")),
        "crt":          int(request.form.get("crt", 1)),
        "uf":           request.form.get("uf", "").upper(),
        "cuf":          int(request.form.get("cuf", 0)),
        "cep":          _so_numeros(request.form.get("cep", "")),
        "xLgr":         request.form.get("xLgr", ""),
        "nro":          request.form.get("nro", ""),
        "xCpl":         request.form.get("xCpl", ""),
        "xBairro":      request.form.get("xBairro", ""),
        "cMun":         request.form.get("cMun", ""),
        "xMun":         request.form.get("xMun", ""),
        "fone":         _so_numeros(request.form.get("fone", "")),
        "serie":        int(request.form.get("serie", 1)),
        "caminho_certificado": existente.get("caminho_certificado", ""),
        "senha_certificado":   request.form.get("senha_certificado", "") or existente.get("senha_certificado", ""),
        "id_csc":       request.form.get("id_csc", "") or existente.get("id_csc", "000001"),
        "csc":          request.form.get("csc", "") or existente.get("csc", ""),
    }
    db.salvar_cliente(nome, dados)
    return jsonify({"ok": True})


@app.route("/admin/clientes/<cliente_id>/cert", methods=["POST"])
@_requer_login
def admin_upload_cert(cliente_id):
    arq = request.files.get("cert")
    if not arq or not arq.filename.lower().endswith(".pfx"):
        return jsonify({"erro": "Envie um arquivo .pfx"}), 400
    nome_arquivo = f"{cliente_id}.pfx"
    dados_pfx = arq.read()
    caminho = os.path.join(_certs_path(), nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(dados_pfx)
    db.salvar_certificado_bytes(cliente_id, dados_pfx)
    cliente = db.carregar_cliente(cliente_id) or {}
    cliente["caminho_certificado"] = nome_arquivo
    db.salvar_cliente(cliente_id, cliente)
    return jsonify({"ok": True, "arquivo": nome_arquivo})


@app.route("/admin/clientes/<cliente_id>/excluir", methods=["POST"])
@_requer_login
def admin_excluir_cliente(cliente_id):
    db.deletar_cliente(cliente_id)
    return jsonify({"ok": True})


@app.route("/admin/clientes/<cliente_id>/numeracao", methods=["POST"])
@_requer_login
def admin_definir_numeracao(cliente_id):
    """Corrige manualmente o próximo número (nNF) de NF-e/NFC-e do emitente
    — ex: depois de números consumidos por engano em homologação."""
    body = request.get_json(force=True) or {}
    modelo = int(body.get("modelo", 55))
    valor = int(body.get("valor", 1))
    if modelo not in (55, 65) or valor < 1:
        return jsonify({"erro": "Parâmetros inválidos"}), 400
    db.definir_numero_nfe(cliente_id, modelo, valor)
    return jsonify({"ok": True})


# ── Produtos ──────────────────────────────────────────────────────

@app.route("/admin/produtos")
@_requer_login
def admin_produtos():
    produtos = db.listar_produtos()
    return render_template("admin/produtos.html", produtos=produtos)


@app.route("/admin/produtos/json")
@_requer_login
def admin_produtos_json():
    return jsonify(db.listar_produtos())


@app.route("/admin/produtos/salvar", methods=["POST"])
@_requer_login
def admin_salvar_produto():
    dados = {
        "codigo":    request.form.get("codigo", "").strip(),
        "descricao": request.form.get("descricao", "").strip(),
        "ncm":       _so_numeros(request.form.get("ncm", "")),
        "cfop":      request.form.get("cfop", "5102"),
        "unidade":   request.form.get("unidade", "UN").upper(),
        "preco":     request.form.get("preco", "0").replace(",", "."),
        "orig":      request.form.get("orig", "0"),
        "csosn":     request.form.get("csosn", "102"),
        "cst_icms":  request.form.get("cst_icms", "00"),
        "p_icms":    request.form.get("p_icms", "12").replace(",", "."),
    }
    if not dados["codigo"] or not dados["descricao"]:
        return jsonify({"erro": "Código e descrição obrigatórios"}), 400
    pid = request.form.get("produto_id", "")
    db.salvar_produto(dados, int(pid) if pid else None)
    return jsonify({"ok": True})


@app.route("/admin/produtos/<int:produto_id>/excluir", methods=["POST"])
@_requer_login
def admin_excluir_produto(produto_id):
    db.deletar_produto(produto_id)
    return jsonify({"ok": True})


# ── NFC-e (modelo 65) ────────────────────────────────────────────

@app.route("/admin/nfce/nova", methods=["GET", "POST"])
@_requer_login
def admin_nova_nfce():
    clientes = db.listar_clientes()
    produtos  = db.listar_produtos()

    if request.method == "GET":
        return render_template("admin/nova_nfce.html",
                               clientes=clientes, produtos=produtos)

    dados = request.get_json(force=True) or {}
    cliente_id = dados.get("cliente_id", "")
    cliente    = db.carregar_cliente(cliente_id)
    if not cliente:
        return jsonify({"erro": "Emitente não encontrado"}), 404

    n_nfe = db.proximo_numero_nfe(cliente_id, modelo=65)

    nota_dados = {
        "cliente_id":   cliente_id,
        "n_nfe":        n_nfe,
        "serie":        int(dados.get("serie", 1)),
        "nat_op":       dados.get("nat_op", "Venda a Consumidor"),
        "tp_nf":        "1",
        "id_dest":      "1",
        "ind_final":    "1",
        "ind_pres":     dados.get("ind_pres", "1"),
        "mod_frete":    "9",
        "tp_pag":       dados.get("tp_pag", "01"),
        "cnpj_dest":    _so_numeros(dados.get("cnpj_dest", "")),
        "cpf_dest":     _so_numeros(dados.get("cpf_dest", "")),
        "xNome_dest":   dados.get("xNome_dest", "CONSUMIDOR NAO IDENTIFICADO"),
        "ie_dest":      "",
        "xLgr_dest":    "", "nro_dest": "", "xCpl_dest": "",
        "xBairro_dest": "", "cMun_dest": "", "xMun_dest": "",
        "uf_dest":      dados.get("uf_dest", ""),
        "cep_dest":     "", "email_dest": "",
        "itens":        dados.get("itens", []),
        "inf_adic":     dados.get("inf_adic", ""),
        "v_nf":         float(dados.get("v_nf", 0)),
        "v_desc":       float(dados.get("v_desc", 0)),
        "v_frete":      0,
        "modelo":       65,
    }

    nota_id = db.criar_nota(nota_dados)
    return jsonify({"ok": True, "nota_id": nota_id})


@app.route("/admin/notas/<int:nota_id>/cupom")
@_requer_login
def admin_download_cupom(nota_id):
    nota = db.get_nota(nota_id)
    if not nota:
        abort(404)
    from danfe_nfce import gerar_cupom
    pdf_bytes = gerar_cupom(nota)
    from flask import Response
    chave = nota.get("chave") or f"NFC{nota_id}"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Cupom_{chave}.pdf"},
    )


# ── API REST ──────────────────────────────────────────────────────

@app.route("/api/notas", methods=["GET"])
def api_listar_notas():
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key or key != os.environ.get("API_KEY", ""):
        return jsonify({"erro": "Não autorizado"}), 401
    notas = db.listar_notas(
        cliente_id=request.args.get("cliente_id"),
        status=request.args.get("status"),
    )
    return jsonify(notas)


# ── Helpers ───────────────────────────────────────────────────────

def _resolver_cert(caminho: str) -> str:
    if os.path.isabs(caminho) and os.path.isfile(caminho):
        return caminho
    base = _certs_path()
    candidato = os.path.join(base, os.path.basename(caminho))
    if not os.path.isfile(candidato):
        # Disco do container é efêmero (Railway) — se o .pfx sumiu num
        # redeploy, re-materializa a partir do Postgres (persistente).
        cliente_id = os.path.splitext(os.path.basename(caminho))[0]
        dados = db.get_certificado_bytes(cliente_id)
        if dados:
            os.makedirs(base, exist_ok=True)
            with open(candidato, "wb") as f:
                f.write(dados)
    return candidato if os.path.isfile(candidato) else caminho


def _montar_dados_emissao(nota: dict) -> dict:
    """Combina dados da nota + dados do emitente (cliente) para emitir_nfe."""
    return {
        # Certificado
        "caminho_certificado": _resolver_cert(nota.get("caminho_certificado", "")),
        "senha_certificado":   nota.get("senha_certificado", ""),

        # Emitente
        "uf":              nota.get("uf", ""),
        "cnpj_emitente":   nota.get("cnpj_emit", ""),
        "ie_emitente":     nota.get("ie", ""),
        "xNome_emitente":  nota.get("razao_social", ""),
        "xFant_emitente":  nota.get("xFant", ""),
        "crt":             int(nota.get("crt", 1)),
        "xLgr_emitente":   nota.get("xLgr", ""),
        "nro_emitente":    nota.get("nro", ""),
        "xCpl_emitente":   nota.get("xCpl", ""),
        "xBairro_emitente":nota.get("xBairro", ""),
        "cMun_emitente":   nota.get("cMun", ""),
        "xMun_emitente":   nota.get("xMun", ""),
        "cep_emitente":    nota.get("cep", ""),
        "fone_emitente":   nota.get("fone", ""),

        # Identificação
        "serie":    int(nota.get("serie", 1)),
        "nnf":      int(nota.get("n_nfe", 1)),
        "nat_op":   nota.get("nat_op", "Venda"),
        "tp_nf":    nota.get("tp_nf", "1"),
        "id_dest":  nota.get("id_dest", "1"),
        "ind_final":nota.get("ind_final", "0"),
        "ind_pres": nota.get("ind_pres", "1"),
        "mod_frete":nota.get("mod_frete", "9"),
        "tp_pag":   nota.get("tp_pag", "01"),
        "fin_nfe":  nota.get("fin_nfe", "1"),
        "ref_nfe":  nota.get("ref_nfe", ""),

        # Destinatário
        "cnpj_destinatario":    nota.get("cnpj_dest", ""),
        "cpf_destinatario":     nota.get("cpf_dest", ""),
        "xNome_destinatario":   nota.get("xNome_dest", ""),
        "ie_destinatario":      nota.get("ie_dest", ""),
        "xLgr_destinatario":    nota.get("xLgr_dest", ""),
        "nro_destinatario":     nota.get("nro_dest", ""),
        "xCpl_destinatario":    nota.get("xCpl_dest", ""),
        "xBairro_destinatario": nota.get("xBairro_dest", ""),
        "cMun_destinatario":    nota.get("cMun_dest", ""),
        "xMun_destinatario":    nota.get("xMun_dest", ""),
        "uf_destinatario":      nota.get("uf_dest", ""),
        "cep_destinatario":     nota.get("cep_dest", ""),
        "email_destinatario":   nota.get("email_dest", ""),

        # Itens
        "itens": nota.get("itens", []),

        # Totais
        "vNF":    float(nota.get("v_nf", 0)),
        "vDesc":  str(nota.get("v_desc", "0.00")),
        "vFrete": str(nota.get("v_frete", "0.00")),

        # Info adicional
        "inf_adic": nota.get("inf_adic", ""),

        # Modelo
        "modelo": int(nota.get("modelo", 55)),

        # NFC-e: CSC (Código de Segurança do Contribuinte) cadastrado na SEFAZ
        "id_csc": nota.get("id_csc", "000001"),
        "csc":    nota.get("csc", ""),
    }


@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ── SPED Fiscal ───────────────────────────────────────────────────────────────

@app.route("/admin/sped", methods=["GET", "POST"])
@_requer_login
def sped_fiscal():
    clientes = db.listar_clientes()
    hoje = datetime.now()

    if request.method == "POST":
        cliente_id = request.form.get("cliente_id", "")
        ano  = int(request.form.get("ano",  hoje.year))
        mes  = int(request.form.get("mes",  hoje.month))

        cliente = next((c for c in clientes if str(c["id"]) == str(cliente_id)), None)
        if not cliente:
            return render_template("admin/sped.html", clientes=clientes,
                                   erro="Emitente não encontrado.", hoje=hoje)

        notas = db.listar_notas(cliente_id=cliente_id)
        # filtra pelo mês/ano e deserializa itens
        notas_mes = []
        for n in notas:
            criado = str(n.get("criado_em", ""))[:7]  # "YYYY-MM"
            if criado == f"{ano}-{mes:02d}":
                if isinstance(n.get("itens"), str):
                    try:
                        n["itens"] = json.loads(n["itens"])
                    except Exception:
                        n["itens"] = []
                notas_mes.append(n)

        from sped_fiscal import gerar_efd
        txt = gerar_efd(cliente, notas_mes, ano=ano, mes=mes)

        nome_arq = f"EFD_{_so_numeros(cliente.get('cnpj',''))}_{ano}{mes:02d}.txt"
        from io import BytesIO
        return send_file(
            BytesIO(txt),
            mimetype="text/plain",
            as_attachment=True,
            download_name=nome_arq,
        )

    return render_template("admin/sped.html", clientes=clientes, hoje=hoje)


# ── Inutilização de Numeração ──────────────────────────────────────────────

@app.route("/admin/inutilizar", methods=["GET", "POST"])
@_requer_login
def inutilizar_numeracao():
    clientes = db.listar_clientes()
    hoje = datetime.now()

    if request.method == "GET":
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje)

    cliente_id = request.form.get("cliente_id", "")
    modelo     = int(request.form.get("modelo", 55))
    serie      = int(request.form.get("serie", 1))
    ano        = int(request.form.get("ano", hoje.year))
    nnf_ini    = int(request.form.get("nnf_ini", 0))
    nnf_fin    = int(request.form.get("nnf_fin", nnf_ini))
    justificativa = request.form.get("justificativa", "").strip()

    cliente = next((c for c in clientes if str(c["id"]) == str(cliente_id)), None)
    if not cliente:
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                               erro="Emitente não encontrado.")
    if len(justificativa) < 15:
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                               erro="Justificativa deve ter ao menos 15 caracteres.")
    if nnf_fin < nnf_ini:
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                               erro="Número final não pode ser menor que o inicial.")
    if not cliente.get("caminho_certificado"):
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                               erro="Emitente sem certificado digital cadastrado.")

    from fluxo_nfe_api import inutilizar_numeracao as _inutilizar, _pfx_para_pem
    import shutil

    caminho_pfx = _resolver_cert(cliente["caminho_certificado"])
    cert_path, key_path, tmp_dir, chave_privada, certificado = _pfx_para_pem(
        caminho_pfx, cliente.get("senha_certificado", "")
    )
    try:
        resultado = _inutilizar(
            uf=cliente["uf"], cnpj=cliente["cnpj"], ano=ano, modelo=modelo,
            serie=serie, nnf_ini=nnf_ini, nnf_fin=nnf_fin,
            justificativa=justificativa,
            cert_path=cert_path, key_path=key_path,
            chave_privada=chave_privada, certificado=certificado,
        )
    except Exception as e:
        return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                               erro=f"Erro ao inutilizar: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    sucesso = resultado.get("cStat") == "102"
    return render_template("admin/inutilizar.html", clientes=clientes, hoje=hoje,
                           resultado=resultado, sucesso=sucesso,
                           form=request.form)


if __name__ == "__main__":
    print("Acesse: http://localhost:5000/admin/notas")
    app.run(debug=False, port=5000)

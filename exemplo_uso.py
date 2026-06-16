"""
Exemplo de uso do fluxo_nfe_api.py.

Antes de rodar:
    pip install requests lxml signxml cryptography
    set NFE_AMBIENTE=2   (homologação — padrão)
"""

import os
from fluxo_nfe_api import emitir_nfe, cancelar_nfe, consultar_status_servico

# ── Certificado ───────────────────────────────────────────────────
CERT_PATH = "certs/meu_certificado.pfx"
CERT_SENHA = "senha123"

# ── Dados da NF-e ─────────────────────────────────────────────────
dados = {
    # Certificado
    "caminho_certificado": CERT_PATH,
    "senha_certificado":   CERT_SENHA,

    # Emitente
    "uf":              "MG",
    "cnpj_emitente":   "12.345.678/0001-95",
    "ie_emitente":     "1234567890123",
    "xNome_emitente":  "EMPRESA EXEMPLO LTDA",
    "xFant_emitente":  "EXEMPLO",
    "crt":             1,  # 1=Simples Nacional, 3=Regime Normal
    "xLgr_emitente":   "Rua das Flores",
    "nro_emitente":    "123",
    "xBairro_emitente":"Centro",
    "cMun_emitente":   "3106200",  # IBGE BH
    "xMun_emitente":   "Belo Horizonte",
    "cep_emitente":    "30130-110",

    # Identificação da nota
    "serie":     1,
    "nnf":       1,       # número da nota — incremente por emissão
    "nat_op":    "Venda de mercadoria",
    "tp_nf":     "1",     # 1=Saída
    "id_dest":   "1",     # 1=Interna
    "ind_final": "0",     # 0=Normal (B2B)
    "ind_pres":  "1",     # 1=Presencial
    "mod_frete": "9",     # 9=Sem frete
    "tp_pag":    "01",    # 01=Dinheiro

    # Destinatário
    "cnpj_destinatario":    "98.765.432/0001-10",
    "xNome_destinatario":   "CLIENTE EXEMPLO SA",
    "ie_destinatario":      "9876543210",
    "xLgr_destinatario":    "Av. Principal",
    "nro_destinatario":     "456",
    "xBairro_destinatario": "Savassi",
    "cMun_destinatario":    "3106200",
    "xMun_destinatario":    "Belo Horizonte",
    "uf_destinatario":      "MG",
    "cep_destinatario":     "30140-070",

    # Itens
    "itens": [
        {
            "nItem":   1,
            "cProd":   "001",
            "xProd":   "PRODUTO EXEMPLO",
            "NCM":     "84713012",
            "CFOP":    "5102",       # Venda dentro do estado
            "uCom":    "UN",
            "qCom":    2,
            "vUnCom":  100.00,
            "orig":    "0",          # 0=Nacional
            "CSOSN":   "102",        # Simples: tributado sem permissão de crédito
        }
    ],
}

if __name__ == "__main__":
    os.environ.setdefault("NFE_AMBIENTE", "2")  # homologação

    # Verificar status da SEFAZ antes de emitir
    status = consultar_status_servico(
        dados["uf"],
        "certs/cert.pem",   # gerado automaticamente pelo emitir_nfe
        "certs/key.pem",
    )
    print(f"Status SEFAZ: {status}")

    # Emitir
    resultado = emitir_nfe(dados)
    print(f"\nResultado:")
    print(f"  Chave: {resultado['chave']}")
    print(f"  Protocolo: {resultado['n_prot']}")
    print(f"  XML: {resultado['xml_path']}")

    # Cancelar (se necessário, dentro de 24h)
    # cancelar_nfe(
    #     chave=resultado["chave"],
    #     n_prot=resultado["n_prot"],
    #     justificativa="Cancelamento de teste com mais de 15 caracteres",
    #     uf=dados["uf"],
    #     cnpj=dados["cnpj_emitente"],
    #     cert_path="...", key_path="...",
    #     chave_privada=..., certificado=...,
    # )

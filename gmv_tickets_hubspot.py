"""
gmv_tickets_hubspot.py
======================
Busca tickets no HubSpot (stage 1326342405) cujo GMV ainda não foi preenchido,
calcula o GMV da empresa no BigQuery (silver_all_emissions) em 3 meses fechados
e atualiza as propriedades gmv_mes_atual, gmv_ultimo_mes, gmv_penultimo_mes e
media_gmv_3m no ticket.

Variáveis de ambiente necessárias:
  HUBSPOT_TOKEN   — Private App token do HubSpot
  GCP_PROJECT     — (opcional) projeto BigQuery, padrão: dw-onfly-prd

Autenticação GCP:
  - Local: rode `gcloud auth application-default login` antes de executar.
  - Produção/CI: defina GOOGLE_APPLICATION_CREDENTIALS apontando para o JSON
    da service account.
"""

import os
import time
import csv
from datetime import date, datetime

import requests
from google.cloud import bigquery

# ── Configurações ─────────────────────────────────────────────────────────────

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
BQ_PROJECT    = os.environ.get("GCP_PROJECT", "dw-onfly-prd")

HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}

STAGE_ID  = "1326342405"
GMV_PROPS = ["gmv_mes_atual", "gmv_ultimo_mes", "gmv_penultimo_mes", "media_gmv_3m"]

NOMES_ALVO = [
    "Leonidas Alves Chow",
    "Lucas Eduardo Domingues de Avelar",
]

# ── Helpers de data ───────────────────────────────────────────────────────────

def mes_range(ano: int, mes: int):
    inicio = date(ano, mes, 1)
    fim    = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
    return inicio, fim

def volta_um_mes(ano: int, mes: int):
    return (ano - 1, 12) if mes == 1 else (ano, mes - 1)

def periodos_do_ticket(data_referencia: date) -> dict:
    ano, mes = data_referencia.year, data_referencia.month
    p_atual     = mes_range(ano, mes)
    p_ultimo    = mes_range(*volta_um_mes(ano, mes))
    p_penultimo = mes_range(*volta_um_mes(*volta_um_mes(ano, mes)))
    return {
        "mes_atual":     p_atual,
        "ultimo_mes":    p_ultimo,
        "penultimo_mes": p_penultimo,
    }

# ── HubSpot: owners ───────────────────────────────────────────────────────────

def load_owners() -> list:
    owners, url, params = [], "https://api.hubapi.com/crm/v3/owners", {"limit": 100}
    while url:
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        owners.extend(data.get("results", []))
        url    = data.get("paging", {}).get("next", {}).get("link")
        params = {}
    return owners

def nome_completo(o: dict) -> str:
    return f"{o.get('firstName','').strip()} {o.get('lastName','').strip()}".strip()

def get_owner_ids(nomes: list) -> list:
    all_owners = load_owners()
    ids = []
    for nome in nomes:
        match = [o for o in all_owners if nome_completo(o).lower() == nome.lower()]
        if match:
            owner_id = str(match[0]["id"])
            ids.append(owner_id)
            print(f"  {nome} -> owner_id {owner_id}")
        else:
            print(f"  AVISO: owner '{nome}' não encontrado no HubSpot")
    return ids

# ── HubSpot: tickets ──────────────────────────────────────────────────────────

def fetch_tickets_sem_gmv() -> list:
    """Retorna todos os tickets no stage alvo com algum GMV faltando."""
    tickets, after = [], None

    while True:
        payload = {
            "filterGroups": [
                {"filters": [
                    {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": STAGE_ID},
                    {"propertyName": prop, "operator": "NOT_HAS_PROPERTY"},
                ]}
                for prop in GMV_PROPS
            ],
            "properties": [
                "id_onfly",
                f"hs_v2_date_entered_{STAGE_ID}",
                "proprietario_da_empresa_atual",
                *GMV_PROPS,
            ],
            "limit": 100,
        }
        if after:
            payload["after"] = after

        for tentativa in range(5):
            resp = requests.post(
                "https://api.hubapi.com/crm/v3/objects/tickets/search",
                headers=HEADERS, json=payload,
            )
            if resp.status_code == 429:
                wait = 2 ** tentativa
                print(f"  Rate limit, aguardando {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError("Rate limit persistente após 5 tentativas")

        data    = resp.json()
        results = data.get("results", [])
        tickets.extend(results)
        print(f"  {len(results)} tickets recuperados (total: {len(tickets)})")

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.3)

    return tickets

# ── BigQuery: GMV ─────────────────────────────────────────────────────────────

def gmv_periodo(client: bigquery.Client, id_onfly: int, data_inicio: date, data_fim: date) -> float:
    query = """
        SELECT SUM(total_amount_currency_brl) AS gmv
        FROM `dw-onfly-prd.travel_core.silver_all_emissions`
        WHERE status = 2
          AND company_id = @id_onfly
          AND DATE(created_at) >= @data_inicio
          AND DATE(created_at) < @data_fim
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("id_onfly",    "INT64", id_onfly),
        bigquery.ScalarQueryParameter("data_inicio", "DATE",  str(data_inicio)),
        bigquery.ScalarQueryParameter("data_fim",    "DATE",  str(data_fim)),
    ])
    rows = list(client.query(query, job_config=job_config).result())
    return float(rows[0].gmv) if rows and rows[0].gmv is not None else 0.0

# ── Atualização HubSpot ───────────────────────────────────────────────────────

def patch_ticket(ticket_id: str, gmv: dict) -> bool:
    payload = {"properties": {
        "gmv_mes_atual":     gmv["gmv_mes_atual"],
        "gmv_ultimo_mes":    gmv["gmv_ultimo_mes"],
        "gmv_penultimo_mes": gmv["gmv_penultimo_mes"],
        "media_gmv_3m":      gmv["media_gmv_3m"],
    }}
    resp = requests.patch(
        f"https://api.hubapi.com/crm/v3/objects/tickets/{ticket_id}",
        headers=HEADERS, json=payload,
    )
    return resp.status_code in (200, 204), resp.text

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== GMV Tickets HubSpot ← BigQuery ===\n")

    # 1. Owners alvo
    print("Carregando owners...")
    owner_ids_alvo = get_owner_ids(NOMES_ALVO)
    print(f"Owner IDs alvo: {owner_ids_alvo}\n")

    # 2. Tickets sem GMV
    print("Buscando tickets sem GMV no stage alvo...")
    tickets_raw = fetch_tickets_sem_gmv()
    print(f"Total: {len(tickets_raw)} tickets encontrados\n")

    # 3. Filtrar por owner e montar registros válidos
    tickets_validos, tickets_sem_dados = [], []
    for t in tickets_raw:
        props   = t.get("properties", {})
        id_onfly = props.get("id_onfly")
        data_str = props.get(f"hs_v2_date_entered_{STAGE_ID}")
        owner    = props.get("proprietario_da_empresa_atual", "")

        # Filtro de owner (opcional — remova se quiser processar todos)
        # if owner not in owner_ids_alvo:
        #     continue

        if not id_onfly or not data_str:
            tickets_sem_dados.append({"ticket_id": t["id"], "motivo": "sem id_onfly ou data"})
            continue

        data_referencia = datetime.fromisoformat(data_str.replace("Z", "+00:00")).date()
        tickets_validos.append({
            "ticket_id":       t["id"],
            "id_onfly":        int(id_onfly),
            "data_referencia": data_referencia,
            "periodos":        periodos_do_ticket(data_referencia),
        })

    print(f"Tickets válidos para processar: {len(tickets_validos)}")
    print(f"Tickets ignorados (sem dados):  {len(tickets_sem_dados)}\n")

    # 4. BigQuery client
    print("Conectando ao BigQuery...")
    bq = bigquery.Client(project=BQ_PROJECT)
    print("Conectado.\n")

    # 5. Calcular GMV e atualizar HubSpot
    atualizados, erros = [], []
    total = len(tickets_validos)
    print(f"Processando {total} tickets...\n")

    for i, rec in enumerate(tickets_validos):
        id_onfly = rec["id_onfly"]
        periodos = rec["periodos"]

        gmv_atual     = gmv_periodo(bq, id_onfly, *periodos["mes_atual"])
        gmv_ultimo    = gmv_periodo(bq, id_onfly, *periodos["ultimo_mes"])
        gmv_penultimo = gmv_periodo(bq, id_onfly, *periodos["penultimo_mes"])
        media         = round((gmv_atual + gmv_ultimo + gmv_penultimo) / 3, 2)

        gmv_data = {
            "gmv_mes_atual":     gmv_atual,
            "gmv_ultimo_mes":    gmv_ultimo,
            "gmv_penultimo_mes": gmv_penultimo,
            "media_gmv_3m":      media,
        }

        ok, msg = patch_ticket(rec["ticket_id"], gmv_data)

        if ok:
            atualizados.append({**rec, **gmv_data, "data_referencia": str(rec["data_referencia"])})
            print(f"  [{i+1}/{total}] ticket={rec['ticket_id']} id_onfly={id_onfly} "
                  f"atual={gmv_atual:.2f} ultimo={gmv_ultimo:.2f} penultimo={gmv_penultimo:.2f} ✓")
        else:
            erros.append({**rec, "erro": msg, "data_referencia": str(rec["data_referencia"])})
            print(f"  [{i+1}/{total}] ticket={rec['ticket_id']} ERRO: {msg}")

        if (i + 1) % 50 == 0:
            time.sleep(0.5)

    # 6. Relatório final
    print(f"\n=== Concluído ===")
    print(f"Atualizados: {len(atualizados)}")
    print(f"Erros:       {len(erros)}")

    if erros:
        caminho = "gmv_erros.csv"
        with open(caminho, "w", newline="", encoding="utf-8") as f:
            campos = ["ticket_id", "id_onfly", "data_referencia", "erro"]
            writer = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(erros)
        print(f"Erros exportados: {caminho}")


if __name__ == "__main__":
    main()

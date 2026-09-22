# GMV Tickets HubSpot ← BigQuery

Script que busca tickets no HubSpot em um stage específico, calcula o GMV da empresa no BigQuery em 3 meses fechados e atualiza as propriedades do ticket automaticamente.

---

## O que faz

1. Busca todos os tickets no stage `1326342405` com alguma propriedade de GMV faltando
2. Para cada ticket, pega o `id_onfly` e a data de entrada no stage
3. Consulta o GMV da empresa no BigQuery (`silver_all_emissions`) em 3 períodos:
   - Mês de entrada no stage
   - Mês anterior
   - Dois meses antes
4. Atualiza as propriedades `gmv_mes_atual`, `gmv_ultimo_mes`, `gmv_penultimo_mes` e `media_gmv_3m` no ticket

---

## Pré-requisitos

- Python 3.9+
- Acesso ao projeto `dw-onfly-prd` no BigQuery
- Token de Private App do HubSpot com permissão de leitura/escrita em Tickets e Owners

---

## Configuração local

```bash
# 1. Clone o repositório
git clone https://github.com/seu-org/seu-repo
cd seu-repo

# 2. Instale as dependências
pip install -r requirements.txt

# 3. Autentique no GCP
gcloud auth application-default login

# 4. Configure as variáveis de ambiente
cp .env.example .env
# Edite o .env e preencha o HUBSPOT_TOKEN
```

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `HUBSPOT_TOKEN` | ✅ | Token de Private App do HubSpot |
| `GCP_PROJECT` | ❌ | Projeto BigQuery (padrão: `dw-onfly-prd`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | ❌ | Caminho para o JSON da service account (só necessário em produção/CI sem `gcloud auth`) |

---

## Como rodar

```bash
HUBSPOT_TOKEN=seu_token python gmv_tickets_hubspot.py
```

Em caso de erro em algum ticket, um arquivo `gmv_erros.csv` é gerado com os detalhes.

---

## Fonte dos dados

- **HubSpot:** tickets via API CRM v3
- **BigQuery:** `dw-onfly-prd.travel_core.silver_all_emissions` (status = 2), agrupado por `company_id`

---

## Agendamento (Claude Code Rotinas)

O script está configurado para rodar como Rotina no Claude Code com os conectores de HubSpot e BigQuery. Para agendar diariamente às 7h, use o cron: `0 7 * * *`.

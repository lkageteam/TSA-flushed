"""
Script de debug pour voir exactement ce que le sync fait pour un agent spécifique.

Pour l'agent 69,397,831 :
1. Trouver son mongo_id dans tsa_reference
2. Interroger MongoDB avec les mêmes filtres que le script
3. Compter par date
"""

import pandas as pd
import pymongo
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).parent))
from connections.config import MONGO_URI, MONGO_DB_NAME, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
import pymysql

load_dotenv()

TARGET_CORP_NUM = '69397831'

print("=" * 70)
print(f"DEBUG AGENT: {TARGET_CORP_NUM}")
print("=" * 70)

# ─── 1. Trouver mongo_id dans tsa_reference (MySQL) ────────────────────────────
print("\n[1] Recherche mongo_id dans tsa_reference...")
conn = pymysql.connect(
    host=MYSQL_HOST,
    port=int(MYSQL_PORT),
    user=MYSQL_USER,
    password=MYSQL_PASSWORD,
    database=MYSQL_DATABASE
)
cursor = conn.cursor()
cursor.execute("SELECT mongo_id, corporate_num, tsa_full_name FROM tsa_reference WHERE corporate_num = %s", (TARGET_CORP_NUM,))
row = cursor.fetchone()
if not row:
    print(f"  ❌ Agent {TARGET_CORP_NUM} NON trouvé dans tsa_reference")
    sys.exit(1)

mongo_id, corp_num, tsa_name = row
print(f"  ✓ mongo_id: {mongo_id}")
print(f"  ✓ corporate_num: {corp_num}")
print(f"  ✓ tsa_full_name: {tsa_name}")

# ─── 2. Voir ce qui est dans MySQL deployments_daily pour cet agent ─────────────
print("\n[2] Contenu actuel de deployments_daily pour cet agent...")
cursor.execute("""
    SELECT deployment_date, deployment_count
    FROM deployments_daily
    WHERE corporate_num = %s
    ORDER BY deployment_date DESC
    LIMIT 5
""", (corp_num,))
mysql_rows = cursor.fetchall()
if mysql_rows:
    print(f"  {len(mysql_rows)} entrée(s) trouvée(s):")
    for date, count in mysql_rows:
        print(f"    - {date}: {count} deployments")
else:
    print("  ❌ Aucune entrée dans deployments_daily")

# ─── 3. Interroger MongoDB avec les mêmes filtres que le script ────────────────
print("\n[3] Requête MongoDB (même logique que sync_deployments_to_mysql.py)...")
now = datetime.now()
month_start = datetime(now.year, now.month, 1, 0, 0, 0)
print(f"  Plage de dates: {month_start} → {now}")

client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = client[MONGO_DB_NAME]

# Filtres identiques au script
query = {
    "createdAt": {"$gte": month_start, "$lte": now},
    "agentId": mongo_id,
    "type": "deployment",
    "typePos": "Merchant",
}

print(f"  Query: {query}")

cursor_mongo = db["pos"].find(query, {"agentId": 1, "createdAt": 1, "type": 1, "typePos": 1}).sort("createdAt", 1)

docs = list(cursor_mongo)
print(f"  → {len(docs)} documents trouvés dans MongoDB")

# ─── 4. Agrégation par date (même logique que le script) ───────────────────────
print("\n[4] Agrégation par date...")
daily_counts = {}
for doc in docs:
    created_at = doc.get("createdAt")
    if not created_at:
        continue
    date_str = created_at.strftime("%Y-%m-%d")
    daily_counts[date_str] = daily_counts.get(date_str, 0) + 1

print(f"  {len(daily_counts)} jours avec des deployments:")
for date in sorted(daily_counts.keys()):
    print(f"    - {date}: {daily_counts[date]} deployments")

# ─── 5. Vérifier la différence avec ce que l'utilisateur a téléchargé ───────────
print("\n[5] Comparaison...")
total_script = sum(daily_counts.values())
print(f"  Total script: {total_script} deployments")
print(f"  Total utilisateur: 41 deployments")
print(f"  Différence: {total_script - 41}")

# ─── 6. Afficher tous les documents bruts pour vérification ────────────────────
print("\n[6] Détail des documents MongoDB (tous)...")
for i, doc in enumerate(docs, 1):
    created_at = doc.get("createdAt")
    type_ = doc.get("type")
    type_pos = doc.get("typePos")
    print(f"  {i}. {created_at} | type='{type_}' | typePos='{type_pos}'")

client.close()
conn.close()

print("\n" + "=" * 70)
print("FIN DU DEBUG")
print("=" * 70)

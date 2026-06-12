import pymysql
import pymongo
import paramiko
import socket
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

TARGET_CORP_NUM = '69397831'
MONGO_ID = '65bb7c0e1db80a22de9e1abb'

print("=" * 70)
print(f"Vérification sync pour {TARGET_CORP_NUM} ({MONGO_ID})")
print("=" * 70)

# ─── SSH Tunnel ──────────────────────────────────────────────────────────────
def get_mysql_connection():
    # Try direct first
    try:
        conn = pymysql.connect(
            host=os.getenv('MYSQL_HOST'),
            port=int(os.getenv('MYSQL_PORT')),
            user=os.getenv('MYSQL_USER'),
            password=os.getenv('MYSQL_PASSWORD'),
            database=os.getenv('MYSQL_DATABASE'),
            connect_timeout=5
        )
        print("[MySQL] Connexion directe OK.")
        return conn
    except:
        print("[MySQL] Connexion directe échouée, tentative tunnel SSH...")

    # Fallback SSH tunnel
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=os.getenv('SSH_HOST'),
        port=int(os.getenv('SSH_PORT')),
        username=os.getenv('SSH_USER'),
        password=os.getenv('SSH_PASS'),
        timeout=10
    )
    transport = client.get_transport()
    local_port = transport.request_port_forward('127.0.0.1', 0)
    transport.request_port_forward('', local_port, os.getenv('MYSQL_HOST'), int(os.getenv('MYSQL_PORT')))

    print(f"[SSH] Tunnel actif : 127.0.0.1:{local_port} → {os.getenv('MYSQL_HOST')}:{os.getenv('MYSQL_PORT')}")

    conn = pymysql.connect(
        host='127.0.0.1',
        port=local_port,
        user=os.getenv('MYSQL_USER'),
        password=os.getenv('MYSQL_PASSWORD'),
        database=os.getenv('MYSQL_DATABASE'),
        connect_timeout=30
    )
    print("[MySQL] Connexion via tunnel SSH OK.")
    return conn

# ─── MySQL ───────────────────────────────────────────────────────────────────
print("\nMySQL:")
conn = get_mysql_connection()
cursor = conn.cursor()
cursor.execute("""
    SELECT deployment_date, deployment_count
    FROM deployments_daily
    WHERE corporate_num = %s
    ORDER BY deployment_date
""", (TARGET_CORP_NUM,))
mysql_rows = cursor.fetchall()
if mysql_rows:
    for date, count in mysql_rows:
        print(f"  {date}: {count}")
    mysql_total = sum(r[1] for r in mysql_rows)
    print(f"  Total MySQL: {mysql_total}")
else:
    print("  Aucune donnée")
    mysql_total = 0
conn.close()

# ─── MongoDB ──────────────────────────────────────────────────────────────────
print("\nMongoDB:")
client = pymongo.MongoClient(
    os.getenv('MONGO_URI'),
    serverSelectionTimeoutMS=8000
)
db = client[os.getenv('MONGO_DB_NAME')]

cursor = db['pos'].find({
    'createdAt': {'$gte': datetime(2026, 6, 1), '$lte': datetime.now()},
    'agentId': MONGO_ID,
    'type': 'deployment',
    'typePos': 'Merchant'
}, {'createdAt': 1}).sort('createdAt', 1)

dates = {}
for doc in cursor:
    date_str = str(doc['createdAt'])[:10]
    dates[date_str] = dates.get(date_str, 0) + 1

for date in sorted(dates.keys()):
    print(f"  {date}: {dates[date]}")
mongo_total = sum(dates.values())
print(f"  Total MongoDB: {mongo_total}")

client.close()

# ─── Comparaison ──────────────────────────────────────────────────────────────
print("\nComparaison:")
print(f"  MySQL: {mysql_total}")
print(f"  MongoDB: {mongo_total}")
print(f"  Différence: {mysql_total - mongo_total}")

if mysql_total == mongo_total:
    print("  ✓ SYNCHRONISÉ")
else:
    print("  ❌ DIFFÉRENCE DÉTECTÉE")

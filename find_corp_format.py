import pymysql
import os
from dotenv import load_dotenv

load_dotenv()

conn = pymysql.connect(
    host=os.getenv('MYSQL_HOST'),
    port=int(os.getenv('MYSQL_PORT')),
    user=os.getenv('MYSQL_USER'),
    password=os.getenv('MYSQL_PASSWORD'),
    database=os.getenv('MYSQL_DATABASE')
)
cursor = conn.cursor()

# Chercher avec différentes variantes du numéro
variants = [
    '69,397,831',
    '69397831',
    '69 397 831',
    '69397831.0',
]

print("Recherche du corporate_num dans tsa_reference...")
for v in variants:
    cursor.execute("SELECT corporate_num, mongo_id, tsa_full_name FROM tsa_reference WHERE corporate_num = %s", (v,))
    row = cursor.fetchone()
    if row:
        print(f"TROUVÉ: {row}")
        break

# Si pas trouvé, chercher tous qui contiennent 69
if not row:
    cursor.execute("SELECT corporate_num, mongo_id, tsa_full_name FROM tsa_reference WHERE corporate_num LIKE '%69%'")
    rows = cursor.fetchall()
    print(f"\nTous les corporate_num contenant '69' ({len(rows)}):")
    for r in rows:
        print(f"  {r}")

conn.close()

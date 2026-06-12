"""
Setup script for lka_tsa_deployments MySQL database.
Creates the database, deployments_daily and tsa_reference tables,
then loads TSA_List.xlsx into tsa_reference.

Run locally: python mysql/setup_deployments_db.py
Requires: paramiko, pandas, openpyxl
"""

import math
import time
from pathlib import Path

import pandas as pd
import paramiko
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from connections.config import (
    SSH_HOST, SSH_USER, SSH_PASS, SSH_PORT,
    MYSQL_PASSWORD, MYSQL_DATABASE, TABLE_DEPLOYMENTS, TABLE_TSA_REF,
)

# ─── Constants ────────────────────────────────────────────────────────────────
EXCEL_PATH = Path(__file__).parent.parent / "TSA_List.xlsx"
BATCH_SIZE = 500
MAX_SSH_RETRIES = 5
_MYSQL_CONTAINER_NAME = None


# ─── SSH helpers ──────────────────────────────────────────────────────────────
def ssh_exec(client, cmd, input_data=None):
    """Exécute une commande SSH, retourne (stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(cmd)
    if input_data:
        stdin.write(input_data)
        stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err


def _get_mysql_container_name(client):
    """Trouve le nom du conteneur Docker MySQL (avec cache)."""
    global _MYSQL_CONTAINER_NAME
    if _MYSQL_CONTAINER_NAME:
        return _MYSQL_CONTAINER_NAME
    out, err = ssh_exec(client, "docker ps --format '{{.Names}}'")
    for name in out.splitlines():
        name = name.strip()
        if "mysql" in name.lower():
            _MYSQL_CONTAINER_NAME = name
            return name
    raise RuntimeError("Conteneur Docker MySQL introuvable. Containers actifs : " + out)


def mysql_exec(client, sql, database="", container_name=None):
    """Exécute du SQL via docker exec mysql."""
    if container_name is None:
        container_name = _get_mysql_container_name(client)
    db_flag = database if database else ""
    cmd = f"docker exec -i {container_name} mysql -u root -p'{MYSQL_PASSWORD}' {db_flag} --default-character-set=utf8mb4"
    out, err = ssh_exec(client, cmd, input_data=sql.encode("utf-8"))
    real_err = "\n".join(
        line for line in err.splitlines()
        if line and not line.startswith("[Warning]") and "password" not in line.lower()
    )
    return out, real_err


def make_rows_batch(df_batch):
    rows = []
    for _, row in df_batch.iterrows():
        vals = []
        for v in row.values:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                vals.append("NULL")
            elif isinstance(v, (int, float)):
                vals.append(str(v))
            else:
                vals.append("'" + str(v).replace("'", "\\'") + "'")
        rows.append("(" + ", ".join(vals) + ")")
    return rows


# ─── SQL definitions ──────────────────────────────────────────────────────────
SQL_CREATE_DB = f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

SQL_CREATE_TSA_REF = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_TSA_REF}` (
    `mongo_id`       VARCHAR(50) PRIMARY KEY,
    `corporate_num`  VARCHAR(50) NOT NULL,
    `tsa_full_name`  VARCHAR(255),
    `region`         VARCHAR(100),
    UNIQUE KEY `uk_corporate` (`corporate_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

SQL_CREATE_DEPLOYMENTS = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_DEPLOYMENTS}` (
    `corporate_num`    VARCHAR(50)  NOT NULL,
    `tsa_full_name`    VARCHAR(255),
    `region`           VARCHAR(100),
    `deployment_date`  DATE         NOT NULL,
    `deployment_count` INT          DEFAULT 0,
    PRIMARY KEY (`corporate_num`, `deployment_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


# ─── Excel loading ────────────────────────────────────────────────────────────
def load_tsa_excel() -> pd.DataFrame:
    print(f"[Excel] Chargement de {EXCEL_PATH} …")
    df = pd.read_excel(EXCEL_PATH, dtype=str)
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r"[\s/\\()\-]+", "_", regex=True)
        .str.replace(r"[^\w]", "", regex=True)
        .str.strip("_")
    )
    df = df.where(pd.notna(df), None)
    print(f"[OK] Excel chargé : {len(df)} lignes, colonnes = {list(df.columns)}")

    # Map to canonical column names
    col_map = {}
    for col in df.columns:
        c = col.lower()
        if c == "tsa_id":
            col_map[col] = "mongo_id"
        elif c == "numero_corporate" or (c.startswith("numero_corporate") and not c.startswith("numero_corporate2")):
            col_map[col] = "corporate_num"
        elif c == "tsa_full_name" or c == "tsafullname":
            col_map[col] = "tsa_full_name"
        elif c == "region_intern" or c == "regionintern":
            col_map[col] = "region"
    df = df.rename(columns=col_map)

    required = {"mongo_id", "corporate_num", "tsa_full_name", "region"}
    missing = required - set(df.columns)
    if missing:
        print(f"[WARN] Colonnes manquantes après mapping : {missing}")
        print(f"       Colonnes disponibles : {list(df.columns)}")
        # Show first few rows to debug
        print(df.head(3).to_string())
        raise ValueError(f"Colonnes manquantes dans TSA_List.xlsx : {missing}")

    df = df[["mongo_id", "corporate_num", "tsa_full_name", "region"]].copy()
    df = df.dropna(subset=["mongo_id", "corporate_num"])
    df["mongo_id"] = df["mongo_id"].str.strip()
    df["corporate_num"] = df["corporate_num"].str.strip()
    print(f"[OK] {len(df)} TSA valides après nettoyage.")
    return df


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    # 1. SSH connection with retries
    client = None
    for attempt in range(1, MAX_SSH_RETRIES + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
            client.connect(
                SSH_HOST, port=SSH_PORT,
                username=SSH_USER, password=SSH_PASS,
                timeout=20, look_for_keys=False, allow_agent=False,
            )
            break
        except Exception as e:
            client.close()
            if attempt == MAX_SSH_RETRIES:
                raise
            print(f"  [SSH tentative {attempt}/{MAX_SSH_RETRIES}] {e} — retry dans 3s…")
            time.sleep(3)
    print(f"[OK] SSH connecté → {SSH_HOST}")

    try:
        # 2. Create database
        print(f"\n[DB] Création base de données `{MYSQL_DATABASE}` …")
        out, err = mysql_exec(client, SQL_CREATE_DB)
        if err:
            print(f"  [WARN] {err}")
        else:
            print(f"  OK")

        # 3. Create tsa_reference table
        print(f"\n[Table] Création `{TABLE_TSA_REF}` …")
        out, err = mysql_exec(client, SQL_CREATE_TSA_REF, database=MYSQL_DATABASE)
        if err:
            print(f"  [WARN] {err}")
        else:
            print(f"  OK")

        # 4. Create deployments_daily table
        print(f"\n[Table] Création `{TABLE_DEPLOYMENTS}` …")
        out, err = mysql_exec(client, SQL_CREATE_DEPLOYMENTS, database=MYSQL_DATABASE)
        if err:
            print(f"  [WARN] {err}")
        else:
            print(f"  OK")

        # 5. Load TSA_List.xlsx → tsa_reference
        df = load_tsa_excel()

        print(f"\n[Insert] Truncate + chargement de {len(df)} TSA dans `{TABLE_TSA_REF}` …")
        out, err = mysql_exec(client, f"TRUNCATE TABLE `{TABLE_TSA_REF}`;", database=MYSQL_DATABASE)
        if err:
            print(f"  [WARN] truncate : {err}")

        col_list = "`mongo_id`, `corporate_num`, `tsa_full_name`, `region`"
        total_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(df), BATCH_SIZE):
            batch_rows = make_rows_batch(df[["mongo_id", "corporate_num", "tsa_full_name", "region"]].iloc[i:i + BATCH_SIZE])
            sql_batch = (
                f"INSERT INTO `{TABLE_TSA_REF}` ({col_list}) VALUES\n"
                + ",\n".join(batch_rows)
                + "\nON DUPLICATE KEY UPDATE"
                + "  `corporate_num`=VALUES(`corporate_num`),"
                + "  `tsa_full_name`=VALUES(`tsa_full_name`),"
                + "  `region`=VALUES(`region`);\n"
            )
            out, err = mysql_exec(client, sql_batch, database=MYSQL_DATABASE)
            if err:
                print(f"  [WARN] batch {i // BATCH_SIZE + 1}/{total_batches}: {err}")
            else:
                print(f"  batch {i // BATCH_SIZE + 1}/{total_batches} OK")

        # 6. Verify
        print(f"\n[Verify] Comptage des lignes …")
        out, err = mysql_exec(
            client,
            f"SELECT COUNT(*) as cnt FROM `{TABLE_TSA_REF}`; SELECT COUNT(*) as cnt FROM `{TABLE_DEPLOYMENTS}`;",
            database=MYSQL_DATABASE,
        )
        print(f"  {out}")

        print("\n[DONE] Setup terminé avec succès.")

    finally:
        client.close()


if __name__ == "__main__":
    main()

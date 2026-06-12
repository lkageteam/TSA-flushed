"""
MongoDB → MySQL sync script for TSA deployments.

Reads deployment records from MongoDB `pos` collection for the current month
and upserts daily counts into MySQL `deployments_daily` table.
Also refreshes `tsa_reference` from TSA_List.xlsx on each run.
Runs via GitHub Actions hourly.

Usage: python sync_deployments_to_mysql.py
"""

import math
import select
import socketserver
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
import pymongo
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

load_dotenv(Path(__file__).parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).parent))
from connections.config import (
    MONGO_URI, MONGO_DB_NAME,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE,
    SSH_HOST, SSH_USER, SSH_PASS, SSH_PORT,
    TABLE_DEPLOYMENTS, TABLE_TSA_REF,
    REGION_NORMALIZATION,
)

try:
    import paramiko
except ImportError:
    paramiko = None

# ─── Constants ────────────────────────────────────────────────────────────────
EXCEL_PATH = Path(__file__).parent / "TSA_List.xlsx"
BATCH_SIZE = 500
MONGO_ATTEMPTS = 5
MONGO_RETRY_BASE_DELAY_S = 3
MONGO_RETRY_MAX_DELAY_S = 15


# ─── Retry helpers ────────────────────────────────────────────────────────────
def _mongo_retry_delay(attempt: int) -> int:
    return min(MONGO_RETRY_MAX_DELAY_S, MONGO_RETRY_BASE_DELAY_S + max(0, attempt - 1) * 2)


def _is_connection_like_error(exc: Exception) -> bool:
    message = str(exc).lower()
    tokens = (
        "timed out", "timeout", "can't connect", "cannot connect",
        "connection refused", "connection reset", "server has gone away",
        "lost connection", "network is unreachable", "serverselectiontimeout",
        "econnreset", "econnrefused", "ehostunreach", "broken pipe",
    )
    return any(token in message for token in tokens)


# ─── SSH tunnel helpers ───────────────────────────────────────────────────────
class _TunnelForwardHandler(socketserver.BaseRequestHandler):
    chain_host = "127.0.0.1"
    chain_port = 3306
    transport = None

    def handle(self):
        try:
            channel = self.transport.open_channel(
                "direct-tcpip",
                (self.chain_host, self.chain_port),
                self.request.getpeername(),
            )
        except Exception:
            return
        if channel is None:
            return
        try:
            while True:
                readers, _, _ = select.select([self.request, channel], [], [])
                if self.request in readers:
                    data = self.request.recv(1024)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readers:
                    data = channel.recv(1024)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()
            self.request.close()


class _ThreadingForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def _open_mysql_ssh_tunnel(remote_port: int = 3306):
    if paramiko is None:
        raise RuntimeError("Paramiko non installé, tunnel SSH impossible.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        password=SSH_PASS,
        timeout=10,
        auth_timeout=10,
        banner_timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    transport = client.get_transport()
    if transport is None:
        client.close()
        raise RuntimeError("Transport SSH indisponible.")

    class Handler(_TunnelForwardHandler):
        pass

    Handler.chain_host = "127.0.0.1"
    Handler.chain_port = remote_port
    Handler.transport = transport

    server = _ThreadingForwardServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        client.close()


def _build_mysql_engine(host: str, port: int):
    db_url = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{host}:{port}/{MYSQL_DATABASE}?charset=utf8mb4"
    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30},
    )


def get_mysql_engine():
    """Try direct MySQL, fallback to SSH tunnel."""
    engine = _build_mysql_engine(MYSQL_HOST, MYSQL_PORT)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[MySQL] Connexion directe OK.")
        return engine
    except Exception as exc:
        engine.dispose()
        if not _is_connection_like_error(exc):
            raise
        print(f"[MySQL] Connexion directe échouée : {exc}")
        if paramiko is None:
            raise RuntimeError("MySQL direct échoué et paramiko absent.") from exc
        print("[MySQL] Tentative via tunnel SSH...")
        try:
            with _open_mysql_ssh_tunnel(MYSQL_PORT) as local_port:
                print(f"[SSH] Tunnel actif : 127.0.0.1:{local_port} → {SSH_HOST}:{MYSQL_PORT}")
                engine = _build_mysql_engine("127.0.0.1", local_port)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                print("[MySQL] Connexion via tunnel SSH OK.")
                return engine
        except Exception as tunnel_exc:
            raise RuntimeError(
                f"MySQL échoué (direct + tunnel SSH). Dernière erreur: {tunnel_exc}"
            ) from tunnel_exc


# ─── Excel / TSA reference ────────────────────────────────────────────────────
def load_tsa_reference() -> pd.DataFrame:
    """Load TSA_List.xlsx and return a cleaned DataFrame."""
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

    df = df[["mongo_id", "corporate_num", "tsa_full_name", "region"]].copy()
    df = df.dropna(subset=["mongo_id", "corporate_num"])
    df["mongo_id"] = df["mongo_id"].str.strip()
    df["corporate_num"] = df["corporate_num"].str.strip()
    print(f"[OK] {len(df)} TSA chargés depuis Excel.")
    return df


def sync_tsa_reference(engine, df_tsa: pd.DataFrame):
    """Upsert tsa_reference table from DataFrame."""
    print(f"[MySQL] Sync tsa_reference ({len(df_tsa)} lignes) …")
    rows = []
    for _, row in df_tsa.iterrows():
        def esc(v):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return "NULL"
            return "'" + str(v).replace("'", "\\'") + "'"
        rows.append(f"({esc(row['mongo_id'])}, {esc(row['corporate_num'])}, {esc(row['tsa_full_name'])}, {esc(row['region'])})")

    with engine.connect() as conn:
        total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            sql = (
                f"INSERT INTO `{TABLE_TSA_REF}` (`mongo_id`, `corporate_num`, `tsa_full_name`, `region`) VALUES\n"
                + ",\n".join(batch)
                + "\nON DUPLICATE KEY UPDATE"
                + " `corporate_num`=VALUES(`corporate_num`),"
                + " `tsa_full_name`=VALUES(`tsa_full_name`),"
                + " `region`=VALUES(`region`)"
            )
            conn.execute(text(sql))
            conn.commit()
            print(f"  tsa_reference batch {i // BATCH_SIZE + 1}/{total_batches} OK")
    print("[OK] tsa_reference synchronisé.")


# ─── Monthly reset ────────────────────────────────────────────────────────────
def maybe_reset_previous_month(engine):
    """Delete rows from previous months (keep only current month)."""
    now = datetime.now()
    if now.day == 1:
        print("[Reset] 1er du mois — suppression des données du mois précédent …")
        with engine.connect() as conn:
            result = conn.execute(
                text("DELETE FROM `deployments_daily` WHERE `deployment_date` < DATE_FORMAT(NOW(), '%Y-%m-01')")
            )
            conn.commit()
            print(f"  {result.rowcount} lignes supprimées.")


# ─── MongoDB data extraction ──────────────────────────────────────────────────
def fetch_mongo_deployments(valid_mongo_ids: set, query_start: datetime, query_end: datetime) -> dict:
    """
    Query MongoDB pos collection for DEPLOYMENT type records.
    Returns dict: {(mongo_id, date_str): count}
    """
    last_error = None
    for attempt in range(1, MONGO_ATTEMPTS + 1):
        mongo_client = None
        try:
            print(f"  -> Mongo tentative {attempt}/{MONGO_ATTEMPTS} …")
            mongo_client = pymongo.MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=30000,
            )
            mongo_client.admin.command("ping")
            db = mongo_client[MONGO_DB_NAME]

            cursor = db["pos"].find(
                {
                    "createdAt": {"$gte": query_start, "$lte": query_end},
                    "agentId": {"$in": list(valid_mongo_ids)},
                },
                {"agentId": 1, "type": 1, "createdAt": 1},
            ).batch_size(10000)

            daily_counts = {}
            total_docs = 0
            for doc in cursor:
                if str(doc.get("type", "")).upper() != "DEPLOYMENT":
                    continue
                agent_id = str(doc.get("agentId", "")).strip()
                created_at = doc.get("createdAt")
                if not agent_id or not created_at:
                    continue
                date_str = created_at.strftime("%Y-%m-%d")
                key = (agent_id, date_str)
                daily_counts[key] = daily_counts.get(key, 0) + 1
                total_docs += 1

            print(f"  -> Mongo OK : {total_docs} déploiements, {len(daily_counts)} (agent,date) uniques.")
            return daily_counts

        except Exception as exc:
            last_error = exc
            print(f"  -> Mongo tentative {attempt}/{MONGO_ATTEMPTS} échouée : {exc}")
            if not _is_connection_like_error(exc) or attempt == MONGO_ATTEMPTS:
                raise RuntimeError(
                    f"ECHEC définitif Mongo après {MONGO_ATTEMPTS} tentatives. Dernière erreur: {last_error}"
                ) from exc
            delay = _mongo_retry_delay(attempt)
            print(f"     Retry dans {delay}s …")
            time.sleep(delay)
        finally:
            if mongo_client is not None:
                mongo_client.close()

    return {}


# ─── MySQL upsert ─────────────────────────────────────────────────────────────
def upsert_deployments(engine, daily_counts: dict, tsa_map: dict):
    """
    Upsert deployment counts into deployments_daily.
    tsa_map: {mongo_id: (corporate_num, tsa_full_name, region)}
    """
    if not daily_counts:
        print("[MySQL] Aucun déploiement à insérer.")
        return

    rows = []
    skipped = 0
    for (mongo_id, date_str), count in daily_counts.items():
        if mongo_id not in tsa_map:
            skipped += 1
            continue
        corp_num, full_name, region = tsa_map[mongo_id]

        def esc(v):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return "NULL"
            return "'" + str(v).replace("'", "\\'") + "'"

        rows.append(
            f"({esc(corp_num)}, {esc(full_name)}, {esc(region)}, '{date_str}', {count})"
        )

    if skipped:
        print(f"  [WARN] {skipped} enregistrements ignorés (mongo_id inconnu dans tsa_reference).")

    if not rows:
        print("[MySQL] Aucune ligne à écrire après mapping.")
        return

    print(f"[MySQL] Upsert {len(rows)} lignes dans `{TABLE_DEPLOYMENTS}` …")
    with engine.connect() as conn:
        total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            sql = (
                f"INSERT INTO `{TABLE_DEPLOYMENTS}` "
                f"(`corporate_num`, `tsa_full_name`, `region`, `deployment_date`, `deployment_count`) VALUES\n"
                + ",\n".join(batch)
                + "\nON DUPLICATE KEY UPDATE"
                + " `deployment_count`=VALUES(`deployment_count`),"
                + " `tsa_full_name`=VALUES(`tsa_full_name`),"
                + " `region`=VALUES(`region`)"
            )
            conn.execute(text(sql))
            conn.commit()
            print(f"  batch {i // BATCH_SIZE + 1}/{total_batches} OK")

    print("[OK] Upsert terminé.")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"[START] sync_deployments_to_mysql — {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 1. Build MySQL engine (direct or SSH tunnel fallback)
    engine = get_mysql_engine()

    # 2. Load TSA reference from Excel
    df_tsa = load_tsa_reference()

    # 3. Sync tsa_reference table
    sync_tsa_reference(engine, df_tsa)

    # 4. Build lookup maps
    # mongo_id → (corporate_num, tsa_full_name, region)
    tsa_map = {}
    for _, row in df_tsa.iterrows():
        tsa_map[row["mongo_id"]] = (row["corporate_num"], row["tsa_full_name"], row["region"])

    valid_mongo_ids = set(tsa_map.keys())
    print(f"[Info] {len(valid_mongo_ids)} mongo_ids valides pour la requête MongoDB.")

    # 5. Monthly reset if applicable
    maybe_reset_previous_month(engine)

    # 6. Date range: start of current month → now
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1, 0, 0, 0)
    print(f"[Info] Plage de requête : {month_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # 7. Fetch from MongoDB
    print("\n[Mongo] Extraction des déploiements …")
    daily_counts = fetch_mongo_deployments(valid_mongo_ids, month_start, now)

    # 8. Upsert to MySQL
    print(f"\n[MySQL] Écriture des données …")
    upsert_deployments(engine, daily_counts, tsa_map)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n[DONE] Sync terminé en {elapsed:.1f}s.")


if __name__ == "__main__":
    main()

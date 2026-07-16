"""
MongoDB → MySQL sync script for TSA deployments.

Modes:
  --mode deployments   Sync Merchant POS deployments from MongoDB (7h-18h UTC)
  --mode transmissions Refresh tsa_reference from TSA_List.xlsx only
  --mode all           Both (default)

Usage: python sync_deployments_to_mysql.py [--mode deployments|transmissions|all]
"""

import argparse
import io
import math
import os
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
    SSH_HOST, SSH_USER, SSH_PASS, SSH_PORT, SSH_PKEY,
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
MAX_SSH_ATTEMPTS   = 7
SSH_RETRY_BASE_S   = 5
SSH_RETRY_MAX_S    = 30

MYSQL_ATTEMPTS     = 7
MYSQL_RETRY_BASE_S = 3
MYSQL_RETRY_MAX_S  = 20


def _ssh_retry_delay(attempt: int) -> float:
    return min(SSH_RETRY_MAX_S, SSH_RETRY_BASE_S + (attempt - 1) * 5)


def _mysql_retry_delay(attempt: int) -> float:
    return min(MYSQL_RETRY_MAX_S, MYSQL_RETRY_BASE_S + (attempt - 1) * 3)


def _connect_ssh_with_retry() -> "paramiko.SSHClient":
    """Open an SSH connection, retrying on transient auth/network errors.

    Auth par CLE (SSH_PKEY) d'abord, mot de passe en repli : le VPS traverse
    des fenêtres de refus de l'auth mot de passe (saturation sshd par le
    brute-force botnet — ~10 700 'Failed password'/48h mesurés le 2026-07-16),
    qui expliquaient ~10% d'échecs des crons de ce repo. La clé échappe au
    chemin mot de passe/PAM. Détails : D:\\LKA\\MYSQL_CONNECTION_METHODS.md §6.
    """
    pkey = None
    if SSH_PKEY:
        try:
            pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(SSH_PKEY))
        except Exception as exc:
            print(f"[SSH] SSH_PKEY illisible ({exc}) - repli mot de passe.")

    last_exc: Exception = RuntimeError("No SSH attempt made.")
    for attempt in range(1, MAX_SSH_ATTEMPTS + 1):
        auth_modes = ([("clé", {"pkey": pkey})] if pkey else []) + [
            ("mot de passe", {"password": SSH_PASS})
        ]
        for auth_name, auth_kwargs in auth_modes:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=SSH_HOST,
                    port=SSH_PORT,
                    username=SSH_USER,
                    timeout=10,
                    auth_timeout=10,
                    banner_timeout=10,
                    look_for_keys=False,
                    allow_agent=False,
                    **auth_kwargs,
                )
                if auth_name != "clé" and pkey:
                    print("[SSH] Connecté par MOT DE PASSE (clé refusée).")
                return client
            except Exception as exc:
                client.close()
                last_exc = exc
                print(
                    f"[SSH] Tentative {attempt}/{MAX_SSH_ATTEMPTS} ({auth_name}) échouée "
                    f"({type(exc).__name__}: {exc})."
                )
        if attempt < MAX_SSH_ATTEMPTS:
            delay = _ssh_retry_delay(attempt)
            print(f"[SSH] Retry dans {delay:.0f}s…")
            time.sleep(delay)
    raise last_exc


def _with_mysql_retry(fn, *args, label: str = "opération MySQL", **kwargs):
    """Call fn(*args, **kwargs), retrying up to MYSQL_ATTEMPTS times on any exception."""
    last_exc: Exception = RuntimeError("No MySQL attempt made.")
    for attempt in range(1, MYSQL_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < MYSQL_ATTEMPTS:
                delay = _mysql_retry_delay(attempt)
                print(
                    f"[MySQL] {label} tentative {attempt}/{MYSQL_ATTEMPTS} échouée "
                    f"({type(exc).__name__}: {exc}). Retry dans {delay:.0f}s…"
                )
                time.sleep(delay)
    raise RuntimeError(
        f"[MySQL] {label} échouée après {MYSQL_ATTEMPTS} tentatives."
    ) from last_exc


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
    client = _connect_ssh_with_retry()
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
        connect_args={"connect_timeout": 30, "read_timeout": 60, "write_timeout": 60},
    )


def _test_mysql_engine(engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_mysql_engine():
    """Build MySQL engine (caller must manage SSH tunnel if needed)."""
    return _build_mysql_engine(MYSQL_HOST, MYSQL_PORT)


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
                    "type": "deployment",
                    "typePos": "Merchant",
                },
                {"agentId": 1, "createdAt": 1},
            ).batch_size(10000)

            daily_counts = {}
            total_docs = 0
            for doc in cursor:
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
def upsert_deployments(engine, daily_counts: dict, tsa_map: dict, month_start: datetime, now: datetime):
    """
    Upsert deployment counts into deployments_daily.
    tsa_map: {mongo_id: (corporate_num, tsa_full_name, region)}

    NOTE: We first DELETE all records for the current date range to ensure
    stale data (e.g. old Agent/Hybride records) is removed when we switch
    to Merchant-only filtering.
    """
    if not daily_counts:
        print("[MySQL] Aucun déploiement à insérer.")
        return

    # ─── 1. DELETE existing data for the current month range ────────────────────
    start_str = month_start.strftime("%Y-%m-%d")
    end_str = now.strftime("%Y-%m-%d")
    print(f"[MySQL] Suppression des anciennes données ({start_str} → {end_str}) …")
    with engine.connect() as conn:
        delete_sql = (
            f"DELETE FROM `{TABLE_DEPLOYMENTS}` "
            f"WHERE `deployment_date` >= '{start_str}' AND `deployment_date` <= '{end_str}'"
        )
        result = conn.execute(text(delete_sql))
        conn.commit()
        deleted = result.rowcount if hasattr(result, 'rowcount') else -1
        print(f"  → {deleted} ligne(s) supprimée(s).")

    # ─── 2. Build INSERT rows ─────────────────────────────────────────────────
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

    # ─── 3. INSERT (simple, no ON DUPLICATE needed since we deleted first) ─────
    print(f"[MySQL] Insertion de {len(rows)} lignes dans `{TABLE_DEPLOYMENTS}` …")
    with engine.connect() as conn:
        total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            sql = (
                f"INSERT INTO `{TABLE_DEPLOYMENTS}` "
                f"(`corporate_num`, `tsa_full_name`, `region`, `deployment_date`, `deployment_count`) VALUES\n"
                + ",\n".join(batch)
            )
            conn.execute(text(sql))
            conn.commit()
            print(f"  batch {i // BATCH_SIZE + 1}/{total_batches} OK")

    print("[OK] Insertion terminée.")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main(mode: str = "all"):
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"[START] sync_deployments_to_mysql [{mode}] — {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 1. Build MySQL engine
    engine = get_mysql_engine()

    # 2. Test direct connection; fallback to SSH tunnel if needed
    #    On GitHub Actions, always use SSH tunnel (MySQL bound to 127.0.0.1 on VPS)
    tunnel_ctx = None
    force_ssh = os.environ.get('GITHUB_ACTIONS') == 'true'

    if force_ssh:
        print("[GitHub Actions] Tunnel SSH forcé (env détecté).")
        if paramiko is None:
            raise RuntimeError("Paramiko non installé, tunnel SSH impossible sur GitHub Actions.")
        tunnel_ctx = _open_mysql_ssh_tunnel(MYSQL_PORT)
        local_port = tunnel_ctx.__enter__()
        print(f"[SSH] Tunnel actif : 127.0.0.1:{local_port} → {SSH_HOST}:{MYSQL_PORT}")
        engine = _build_mysql_engine("127.0.0.1", local_port)
        if not _test_mysql_engine(engine):
            tunnel_ctx.__exit__(None, None, None)
            raise RuntimeError("MySQL échoué via tunnel SSH.")
        print("[MySQL] Connexion via tunnel SSH OK.")
    elif not _test_mysql_engine(engine):
        engine.dispose()
        if paramiko is None:
            raise RuntimeError("MySQL direct échoué et paramiko absent.")
        print(f"[MySQL] Connexion directe échouée, tentative via tunnel SSH...")
        tunnel_ctx = _open_mysql_ssh_tunnel(MYSQL_PORT)
        local_port = tunnel_ctx.__enter__()
        print(f"[SSH] Tunnel actif : 127.0.0.1:{local_port} → {SSH_HOST}:{MYSQL_PORT}")
        engine = _build_mysql_engine("127.0.0.1", local_port)
        if not _test_mysql_engine(engine):
            tunnel_ctx.__exit__(None, None, None)
            raise RuntimeError("MySQL échoué via tunnel SSH.")
        print("[MySQL] Connexion via tunnel SSH OK.")

    try:
        # 3. Always refresh TSA reference (lightweight, needed by both modes)
        df_tsa = load_tsa_reference()
        _with_mysql_retry(sync_tsa_reference, engine, df_tsa, label="sync_tsa_reference")

        if mode == "transmissions":
            # Transmissions mode: tsa_reference update is all we need here.
            # The actual transmission counts come from Google Form Responses
            # and are read directly in AppsScript — nothing more to do in Python.
            elapsed = (datetime.now() - start_time).total_seconds()
            print(f"\n[DONE] tsa_reference rafraîchi (mode transmissions) en {elapsed:.1f}s.")
            return

        # 4. Build lookup maps (deployments mode or all)
        tsa_map = {}
        for _, row in df_tsa.iterrows():
            tsa_map[row["mongo_id"]] = (row["corporate_num"], row["tsa_full_name"], row["region"])

        valid_mongo_ids = set(tsa_map.keys())
        print(f"[Info] {len(valid_mongo_ids)} mongo_ids valides pour la requête MongoDB.")

        # 5. Monthly reset if applicable
        _with_mysql_retry(maybe_reset_previous_month, engine, label="maybe_reset_previous_month")

        # 6. Date range: start of current month → now
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1, 0, 0, 0)
        print(f"[Info] Plage : {month_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d %H:%M:%S')}")

        # 7. Fetch Merchant deployments from MongoDB
        print("\n[Mongo] Extraction des déploiements Merchant …")
        daily_counts = fetch_mongo_deployments(valid_mongo_ids, month_start, now)

        # 8. Upsert to MySQL
        print(f"\n[MySQL] Écriture des données …")
        _with_mysql_retry(upsert_deployments, engine, daily_counts, tsa_map, month_start, now, label="upsert_deployments")

        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"\n[DONE] Sync terminé en {elapsed:.1f}s.")
    finally:
        if tunnel_ctx is not None:
            tunnel_ctx.__exit__(None, None, None)
            print("[SSH] Tunnel fermé.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TSA MongoDB → MySQL sync")
    parser.add_argument(
        "--mode",
        choices=["all", "deployments", "transmissions"],
        default="all",
        help="Sync mode: all | deployments | transmissions",
    )
    args = parser.parse_args()
    main(mode=args.mode)

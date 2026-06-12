# TSA Transmission & Deployment Sheet Automation

This plan creates an automated system to maintain a Google Sheet tracking TSA transmissions and deployments with hourly updates, monthly reset, and two tabs: monthly summary and daily details.

## Architecture Overview

**Key identifier**: Numéro Corporate (lien entre Form, MongoDB `agentId`, et TSA_List.xlsx)

```
Google Forms (Transmissions) → Forms Responses Sheet (existing)
                                      ↓
MongoDB (pulse_benin)               AppsScript (reads both sources via JDBC)
    ↓ (Python script with retries)        ↓
MySQL (lka_tsa_deployments table)  Google Sheet (Summary + Daily Details tabs)
    ↑                                    ↑
TSA_List.xlsx (mapping)         TSA Full Name displayed
```

## Components

### 1. MySQL Database Setup

**Database**: `lka_tsa_deployments` (new)

**SSH connection with retry from `setup_tsa.py` (lines 56-74)**:
```python
MAX_SSH_RETRIES = 5
client = None
for _attempt in range(1, MAX_SSH_RETRIES + 1):
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
        client.connect(
            SSH_HOST, username=SSH_USER, password=SSH_PASS,
            timeout=20, look_for_keys=False, allow_agent=False
        )
        break
    except Exception as _e:
        client.close()
        if _attempt == MAX_SSH_RETRIES:
            raise
        print(f"  [SSH tentative {_attempt}/{MAX_SSH_RETRIES}] {_e} — retry dans 3s…")
        time.sleep(3)
print(f"[OK] SSH connecté → {SSH_HOST}")
```

**MySQL execution via SSH from `setup_tsa.py` (lines 43-53)**:
```python
def mysql_exec(client, sql, database=""):
    """Exécute du SQL via docker exec mysql."""
    db_flag = database if database else ""
    cmd = f"{MYSQL_CMD} {db_flag} --default-character-set=utf8mb4"
    out, err = ssh_exec(client, cmd, input_data=sql.encode("utf-8"))
    # mysql écrit les warnings sur stderr — on filtre les erreurs réelles
    real_err = "\n".join(
        l for l in err.splitlines()
        if l and not l.startswith("[Warning]") and "password" not in l.lower()
    )
    return out, real_err
```

**Table schema** (based on pattern from `D:\LKA\Perf_commissions\mysql\setup_db.py`):
```sql
CREATE TABLE IF NOT EXISTS `deployments_daily` (
    `corporate_num` VARCHAR(50) NOT NULL,
    `tsa_full_name` VARCHAR(255),
    `region` VARCHAR(100),
    `deployment_date` DATE NOT NULL,
    `deployment_count` INT DEFAULT 0,
    PRIMARY KEY (`corporate_num`, `deployment_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**TSA mapping table** (loaded from TSA_List.xlsx):
```sql
CREATE TABLE IF NOT EXISTS `tsa_reference` (
    `mongo_id` VARCHAR(50) PRIMARY KEY,
    `corporate_num` VARCHAR(50) NOT NULL,
    `tsa_full_name` VARCHAR(255),
    `region` VARCHAR(100),
    UNIQUE KEY `uk_corporate` (`corporate_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**User access**: Root only (no additional users per requirement from project file)

**`MYSQL_CMD` definition** (from `setup_tsa.py` context - mysql via docker):
```python
MYSQL_CMD = "docker exec -i mysql mysql -u root -p'LkaRoot2025Secure!'"
```
*Note: MySQL runs inside a Docker container on the server. The setup script uses SSH + `docker exec` to execute SQL.*

**`sftp_upload_string` function** (from `deploy_server.py`):
```python
def sftp_upload_string(sftp, content: str, remote_path: str, description=""):
    """Create a remote file from a string."""
    if description:
        print(f"  [SFTP] {description}")
    print(f"    [string] → {remote_path}")
    import io
    with sftp.file(remote_path, "w") as f:
        f.write(content.encode("utf-8"))
    print(f"    OK ({len(content)} chars)")
```

**`make_rows_batch` function** (helper for batch inserts):
```python
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
```

**Setup script**: `D:\LKA\TSA flushed\mysql\setup_deployments_db.py` (new, following `setup_tsa.py` pattern)

### 2. MongoDB → MySQL Sync Script

**File**: `D:\LKA\TSA flushed\sync_deployments_to_mysql.py`

**Purpose**: Extract deployment data from MongoDB and store in MySQL table

**Key patterns from reference files**:

**MongoDB connection pattern from `mongo_client.py` (lines 6-13)**:
```python
def get_mongo_client():
    """
    Retourne une instance unique (singleton) du client MongoClient.
    """
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI)
    return _mongo_client
```

**MySQL connection with pool_pre_ping from `connect.py` (lines 21-31)**:
```python
def make_engine(database: str = MYSQL_DATABASE):
    """
    Crée et retourne un engine SQLAlchemy pour la base spécifiée.

    Args:
        database: nom de la base MySQL à cibler (défaut : lka_client_mtn)

    Returns:
        sqlalchemy.engine.Engine
    """
    return create_engine(connection_string(database), pool_pre_ping=True)
```

**MySQL engine with timeout settings from `update_tsa_performance.py` (lines 137-149)**:
```python
def _build_mysql_engine(config_module, port: int):
    db_url = make_url(config_module.connection_string()).set(port=port)
    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 5,
            "read_timeout": 30,
            "write_timeout": 30,
        },
    )
```

**MySQL candidate ports pattern from `update_tsa_performance.py` (lines 99-113)**:
```python
def _mysql_candidate_ports(config_module) -> list[int]:
    base_url = make_url(config_module.connection_string())
    primary_port = base_url.port or 3306
    ports = [primary_port]
    for raw_value in MYSQL_FALLBACK_PORTS_RAW.split(","):
        value = raw_value.strip()
        if not value:
            continue
        try:
            port = int(value)
        except ValueError:
            continue
        if port not in ports:
            ports.append(port)
    return ports
```

**MongoDB query pattern for deployments from `update_tsa_performance.py` (lines 358-390)**:
```python
# MongoDB query pattern for deployments
cursor_pos_m = db['pos'].find(
    {
        "createdAt": {"$gte": query_start, "$lte": query_end},
        "agentId": {"$in": valid_tsa_ids}
    },
    {"agentId": 1, "typePos": 1, "type": 1, "createdAt": 1}
).batch_size(10000)

for doc in cursor_pos_m:
    tsa_id_mongo = str(doc.get('agentId', '')).strip()
    if str(doc.get('type', '')).upper() != 'DEPLOYMENT':
        continue
    # Count deployments by date
```

**Retry pattern** (from `update_tsa_performance.py` lines 70-76):
```python
def _mongo_retry_delay_seconds(attempt_number: int) -> int:
    return min(MONGO_RETRY_MAX_DELAY_S, MONGO_RETRY_BASE_DELAY_S + max(0, attempt_number - 1) * 2)
```

**Connection error detection from `update_tsa_performance.py` (lines 78-96)**:
```python
def _is_connection_like_error(exc: Exception) -> bool:
    message = str(exc).lower()
    tokens = (
        "timed out",
        "timeout",
        "can't connect",
        "cannot connect",
        "connection refused",
        "connection reset",
        "server has gone away",
        "lost connection",
        "network is unreachable",
        "serverselectiontimeout",
        "econnreset",
        "ecconnrefused",
        "ehostunreach",
        "broken pipe",
    )
    return any(token in message for token in tokens)
```

**Full MongoDB retry loop from `update_tsa_performance.py` (lines 348-450)**:
```python
def _load_mongo_stats(mongo_uri, mongo_db_name, valid_tsa_ids, query_start, query_end, month_start, month_end, week_start, week_end):
    last_error = None
    valid_tsa_set = set(valid_tsa_ids)
    for attempt in range(1, MONGO_ATTEMPTS + 1):
        client = None
        try:
            print(f"      -> Mongo tentative {attempt}/{MONGO_ATTEMPTS}...")
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=30000)
            client.admin.command("ping")
            db = client[mongo_db_name]

            # ... data extraction logic ...

            return pos_stats, reports_stats
        except Exception as exc:
            last_error = exc
            print(f"      -> Mongo tentative {attempt}/{MONGO_ATTEMPTS} échouée : {exc}")
            if not _is_connection_like_error(exc) or attempt == MONGO_ATTEMPTS:
                raise RuntimeError(f"ECHEC définitif Mongo après retries. Dernière erreur: {last_error}") from exc
            delay_seconds = _mongo_retry_delay_seconds(attempt)
            print(f"         Retry Mongo dans {delay_seconds}s.")
            time.sleep(delay_seconds)
        finally:
            if client is not None:
                client.close()
```

**Date range calculation for MongoDB query**:
```python
from datetime import datetime, timedelta

now = datetime.now()
month_start = datetime(now.year, now.month, 1, 0, 0, 0)
# Query from start of month to now
query_start = month_start
query_end = now
```

**Data flow**:
1. Load TSA_List.xlsx into MySQL `tsa_reference` table (mapping mongo_id → corporate_num → full_name)
2. Connect to MongoDB with retries
3. Query `pos` collection for deployments WHERE `createdAt` >= start_of_month AND `createdAt` <= now
4. Map MongoDB `agentId` → `corporate_num` via `tsa_reference` table
5. Group by `corporate_num` and date (`createdAt` truncated to date)
6. Upsert to `deployments_daily` table:
   ```sql
   INSERT INTO deployments_daily (corporate_num, tsa_full_name, region, deployment_date, deployment_count)
   VALUES (?, ?, ?, ?, ?)
   ON DUPLICATE KEY UPDATE deployment_count = VALUES(deployment_count)
   ```
7. On 1st of each month: `DELETE FROM deployments_daily WHERE deployment_date < DATE_FORMAT(NOW(), '%Y-%m-01')`

**Upsert pattern for MySQL from `setup_tsa.py` (lines 130-143)**:
```python
# Insérer par batches
total_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
for i in range(0, len(df), BATCH_SIZE):
    batch_rows = make_rows_batch(df.iloc[i:i + BATCH_SIZE])
    sql_batch = (
        f"INSERT INTO `{TARGET_TABLE}` ({col_list}) VALUES\n"
        + ",\n".join(batch_rows) + ";\n"
    )
    out, err = mysql_exec(client, sql_batch, database=TARGET_DB)
    if err:
        print(f"[WARN] batch {i//BATCH_SIZE+1}/{total_batches}: {err}")
```

**Excel loading pattern from `setup_tsa.py` (lines 87-97)**:
```python
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
print(f"[OK] Excel chargé : {len(df)} lignes, {len(df.columns)} colonnes.")
```

**SSH exec pattern from `setup_tsa.py` (lines 32-40)**:
```python
def ssh_exec(client, cmd, input_data=None):
    """Exécute une commande SSH, retourne (stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(cmd)
    if input_data:
        stdin.write(input_data)
        stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err
```

### 3. Google Sheet Structure

**Existing sheet**: https://docs.google.com/spreadsheets/d/1SkdPVEihCZSHspVtYOcSTl_vgbLniaIhbZL208f8Vv8

**Forms Responses tab** (existing):
- Columns: Timestamp, Email Address, Nom du TSA, Numéro corporate, Region, etc.
- Source of transmission data

**Tab 1: Summary (Monthly)**
Columns: TSA (Full Name from TSA_List), Region, Transmissions, Deployments done
- Updated hourly
- Shows cumulative counts from 1st of month to current date
- Column widths auto-adjusted, headers formatted
- **Key**: Numéro Corporate (used to join Form data + MongoDB data)

**Tab 2: Daily Details**
Columns: Date, TSA, Region, Transmissions, Deployments
- One row per TSA per day
- Updated hourly
- Reset monthly

### 4. Google AppsScript

**Type**: Bound script (simpler for accessing active sheet)

**File**: `D:\LKA\TSA flushed\apps_script\update_sheet.gs`

**Functions**:
```javascript
function updateSummarySheet() {
    // 1. Find the Form Responses tab (usually "Form Responses 1" or translated)
    //    Use: spreadsheet.getSheets() and find the one with "Form" in name
    // 2. Read all rows from Form Responses tab
    // 3. Filter rows where Timestamp is in current month
    //    Parse: new Date(row[0]) >= monthStart && new Date(row[0]) <= now
    // 4. Group by "Numéro corporate" (column index 3) and count transmissions
    // 5. Read deployments from MySQL:
    //    SELECT corporate_num, SUM(deployment_count) as total
    //    FROM deployments_daily
    //    WHERE deployment_date >= DATE_FORMAT(NOW(), '%Y-%m-01')
    //    GROUP BY corporate_num
    // 6. Read TSA names from MySQL:
    //    SELECT corporate_num, tsa_full_name, region FROM tsa_reference
    // 7. Merge: For each corporate_num in union(transmissions, deployments):
    //    - tsa_full_name from tsa_reference
    //    - region from tsa_reference
    //    - transmissions count (0 if none)
    //    - deployments count (0 if none)
    // 8. Write to "Summary (Monthly)" tab with headers: TSA, Region, Transmissions, Deployments done
    // 9. Format: bold headers, auto-size columns
}

function updateDailyDetailsSheet() {
    // 1. Read Form Responses rows for current month
    // 2. Group by Date (truncate Timestamp to date) + Numéro corporate, count transmissions
    // 3. Read deployments from MySQL:
    //    SELECT deployment_date, corporate_num, deployment_count
    //    FROM deployments_daily
    //    WHERE deployment_date >= DATE_FORMAT(NOW(), '%Y-%m-01')
    // 4. Merge by date + corporate_num
    // 5. Get tsa_full_name and region from tsa_reference for each corporate_num
    // 6. Write to "Daily Details" tab with: Date, TSA, Region, Transmissions, Deployments
    // 7. Format headers and columns
}

function clearPreviousMonthData() {
    // On 1st of month (new Date().getDate() === 1):
    // Clear all rows except header from Summary and Daily Details tabs
}

function createTrigger() {
    // ScriptApp.newTrigger('updateSummarySheet')
    //   .timeBased().everyHours(1).create();
}
```

**Data sources**:
- **Transmissions**: Read directly from Form Responses tab
  - *How to find the tab name*: The Form creates a tab, usually called "Form Responses 1" (or translated). In AppsScript: iterate `spreadsheet.getSheets()` and find the one whose name contains "Form" or "Responses" or "Réponses".
- **Deployments**: Read from MySQL `deployments_daily` table via JDBC

**Key pattern**: Use `SpreadsheetApp.getActiveSpreadsheet()` for bound script

**MySQL JDBC connection with retries** (following Python retry pattern):
```javascript
function getMySQLConnectionWithRetry(maxAttempts, baseDelayMs) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      var conn = Jdbc.getConnection(
        'jdbc:mysql://' + MYSQL_HOST + ':' + MYSQL_PORT + '/' + MYSQL_DATABASE,
        MYSQL_USER,
        MYSQL_PASSWORD
      );
      return conn;
    } catch (e) {
      if (attempt === maxAttempts) throw e;
      var delay = Math.min(baseDelayMs + (attempt - 1) * 2000, 12000);
      Utilities.sleep(delay);
    }
  }
}

function executeQueryWithRetry(conn, query, maxAttempts, baseDelayMs) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      var stmt = conn.createStatement();
      return stmt.executeQuery(query);
    } catch (e) {
      if (attempt === maxAttempts) throw e;
      var delay = Math.min(baseDelayMs + (attempt - 1) * 2000, 12000);
      Utilities.sleep(delay);
    }
  }
}
```

### 5. Server Deployment

**Server**: 75.119.154.255 (SSH: root / 8i8Jlnuyz~2cKisB)

**Remote directory**: `/opt/tsa_deployments`

**Deployment script**: `D:\LKA\TSA flushed\deploy_server.py`

**No Git repository** - Files transferred directly via SFTP (user request).

**Full deployment pattern from `deploy_server.py` (lines 79-92)**:
```python
def ssh_connect():
    """Etablit une connexion SSH vers le serveur."""
    print(f"  Connexion SSH → {SERVER_USER}@{SERVER_HOST}:{SERVER_PORT}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=SERVER_HOST,
        port=SERVER_PORT,
        username=SERVER_USER,
        password=SERVER_PASS,
        timeout=30,
    )
    print("  Connexion etablie.")
    return client
```

**Remote command execution from `deploy_server.py` (lines 95-118)**:
```python
def run_cmd(client, cmd, description="", check=True):
    """Execute une commande SSH et affiche le resultat."""
    if description:
        print(f"\n  [{description}]")
    print(f"  $ {cmd}")

    stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()

    if out:
        for line in out.split("\n"):
            print(f"    {line}")
    if err and exit_code != 0:
        for line in err.split("\n"):
            print(f"    [stderr] {line}")

    if check and exit_code != 0:
        print(f"  ERREUR: commande echouee (exit {exit_code})")
        if not description.startswith("(optionnel)"):
            raise RuntimeError(f"Commande echouee: {cmd}")

    return exit_code, out, err
```

**Cron job installation from `deploy_server.py` (lines 236-242)**:
```python
cron_line = f"{CRON_SCHEDULE} {REMOTE_BASE}/run_server.sh >> /dev/null 2>&1"

# Lire le crontab actuel, retirer l'ancien job s'il existe, ajouter le nouveau
run_cmd(client,
    f'(crontab -l 2>/dev/null | grep -v "tsa_deployments" ; '
    f'echo "{cron_line}") | crontab -',
    description="Installation du cron job (toutes les heures)")
```

**Deployment flow (SFTP direct, no Git)**:
```python
def deploy():
    client = ssh_connect()
    sftp = client.open_sftp()
    
    # 1. Create remote directory
    run_cmd(client, f"mkdir -p {REMOTE_BASE}")
    
    # 2. SFTP upload all source files
    sftp_upload_file(sftp, "sync_deployments_to_mysql.py", f"{REMOTE_BASE}/sync_deployments_to_mysql.py")
    sftp_upload_file(sftp, "connections/config.py", f"{REMOTE_BASE}/connections/config.py")
    sftp_upload_file(sftp, ".env", f"{REMOTE_BASE}/.env")
    sftp_upload_file(sftp, "TSA_List.xlsx", f"{REMOTE_BASE}/TSA_List.xlsx")
    
    # 3. Create venv + install dependencies
    run_cmd(client, f"cd {REMOTE_BASE} && python3 -m venv venv")
    run_cmd(client, f"cd {REMOTE_BASE} && source venv/bin/activate && pip install pandas pymongo sqlalchemy pymysql python-dotenv openpyxl")
    
    # 4. Create run_server.sh
    run_server_content = f"#!/bin/bash\ncd {REMOTE_BASE} && source venv/bin/activate && python sync_deployments_to_mysql.py"
    sftp_upload_string(sftp, run_server_content, f"{REMOTE_BASE}/run_server.sh")
    run_cmd(client, f"chmod +x {REMOTE_BASE}/run_server.sh")
    
    # 5. Install cron job
    # ... cron installation code ...
```

**Cron schedule**: Hourly (`0 * * * *`)

**SFTP upload pattern from `deploy_server.py` (lines 130-142)**:
```python
def sftp_upload_file(sftp, local_path, remote_path, description=""):
    """Transfere un fichier local vers le serveur."""
    if description:
        print(f"  [SFTP] {description}")
    local_path = Path(local_path)
    if not local_path.exists():
        print(f"    ATTENTION: {local_path} n'existe pas, skip.")
        return False
    print(f"    {local_path} → {remote_path}")
    sftp.put(str(local_path), remote_path)
    size = local_path.stat().st_size
    print(f"    OK ({size} octets)")
    return True
```

**Git pull pattern from `vps_git_pull_only.py` (lines 29-37)**:
```python
command = f"cd {REMOTE_DIR} && git pull"
log(f"Executing: {command}")

exit_status, output, errors = run_remote_command(client, command, timeout=600, stream=True)

print("\n=== GIT PULL OUTPUT ===")
print(output)

if errors.strip():
    print("\n=== ERRORS ===")
    print(errors)
```

**Cron command**: `/opt/tsa_deployments/sync_deployments_to_mysql.py`

**Dependencies**:
```bash
pip install pandas pymongo sqlalchemy pymysql python-dotenv openpyxl paramiko
```

**Required imports for each Python file**:
- `connections/config.py`: `os`, `sqlalchemy.engine.make_url`, `sqlalchemy.create_engine`
- `mysql/setup_deployments_db.py`: `paramiko`, `pandas`, `time`, `pathlib.Path`
- `sync_deployments_to_mysql.py`: `pymongo`, `pandas`, `sqlalchemy`, `datetime`, `connections.config`
- `deploy_server.py`: `paramiko`, `pathlib.Path`

**Note**: `openpyxl` is required for `pd.read_excel()` to read `.xlsx` files.

### 6. Environment Variables

**File**: `.env` (both local and server)

**Content**:
```env
# MongoDB
MONGO_URI=mongodb://lkaBi229:gBLocal24@38.242.195.126:27018/pulse_benin
MONGO_DB_NAME=pulse_benin

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=LkaRoot2025Secure!
MYSQL_DATABASE=lka_tsa_deployments

# SSH (for deployment)
SSH_HOST=75.119.154.255
SSH_USER=root
SSH_PASS=8i8Jlnuyz~2cKisB
```

**Note**: Server uses localhost for MySQL (from `deploy_server.py` line 59)

**MySQL Connectivity Test from AppsScript**:

Create a test function in AppsScript to verify JDBC access:

```javascript
// Test MySQL connectivity from Google AppsScript
// Copy this into your Sheet's AppsScript editor and run testMySQLConnection()
// NOTE: Does NOT require lka_tsa_deployments to exist yet

var MYSQL_HOST = '75.119.154.255';
var MYSQL_PORT = '3306';
var MYSQL_USER = 'root';
var MYSQL_PASSWORD = 'LkaRoot2025Secure!';

function testMySQLConnection() {
  var maxAttempts = 5;
  var baseDelayMs = 2000;
  var lastError = null;
  
  for (var attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      console.log('Attempt ' + attempt + '/' + maxAttempts + '...');
      // Connect WITHOUT specifying a database (works even if lka_tsa_deployments doesn't exist)
      var conn = Jdbc.getConnection(
        'jdbc:mysql://' + MYSQL_HOST + ':' + MYSQL_PORT,
        MYSQL_USER,
        MYSQL_PASSWORD
      );
      
      var stmt = conn.createStatement();
      // Test connection + list available databases
      var rs = stmt.executeQuery('SELECT VERSION() as version, NOW() as server_time');
      
      var version = '';
      var serverTime = '';
      if (rs.next()) {
        version = rs.getString('version');
        serverTime = rs.getString('server_time');
      }
      rs.close();
      
      // List existing databases
      var dbList = [];
      var rs2 = stmt.executeQuery('SHOW DATABASES');
      while (rs2.next()) {
        var dbName = rs2.getString(1);
        if (dbName !== 'information_schema' && dbName !== 'performance_schema' && dbName !== 'mysql' && dbName !== 'sys') {
          dbList.push(dbName);
        }
      }
      rs2.close();
      
      console.log('✅ SUCCESS!');
      console.log('MySQL Version: ' + version);
      console.log('Server Time: ' + serverTime);
      console.log('Existing databases: ' + dbList.join(', '));
      
      // Only show toast if running from a Sheet context
      try {
        var ss = SpreadsheetApp.getActiveSpreadsheet();
        if (ss) {
          ss.toast('MySQL Connected! DBs: ' + dbList.join(', '), 'Test', 10);
        }
      } catch (toastErr) {
        // Running in script editor, no toast available - that's OK
      }
      
      stmt.close();
      conn.close();
      return true;
      
    } catch (e) {
      lastError = e;
      console.log('❌ Attempt ' + attempt + ' failed: ' + e.message);
      
      if (attempt === maxAttempts) {
        console.log('All attempts failed. Last error: ' + lastError.message);
        SpreadsheetApp.getActiveSpreadsheet().toast(
          'MySQL Connection FAILED: ' + lastError.message,
          'Connection Test',
          30
        );
        return false;
      }
      
      var delay = Math.min(baseDelayMs + (attempt - 1) * 2000, 12000);
      console.log('Waiting ' + delay + 'ms before retry...');
      Utilities.sleep(delay);
    }
  }
}
```

**How to run the test**:
1. Open your Google Sheet
2. Extensions → Apps Script
3. Paste the code above
4. Click "Run" (▶️) next to `testMySQLConnection`
5. Check the execution log for results

**✅ JDBC CONFIRMED WORKING** — Test results (June 12, 2026):
- MySQL 8.0.45 accessible via JDBC from Google AppsScript
- Existing databases: `lka_client_mtn`, `lka_client_tsa`, `lka_perf_commissions`, `mobile_care_dw`
- `lka_tsa_deployments` does not exist yet (will be created by setup script)
- Architecture stays simple: AppsScript → MySQL via JDBC directly

### 7. TSA-Region Mapping

**Source**: `D:\LKA\TSA flushed\TSA_List.xlsx`

**TSA_List.xlsx columns** (exact names):
- `TSA ID` → mongo_id (maps to MongoDB `agentId`)
- `Numero Corporate` → corporate_num (join key)
- `TSA Full NAME` → tsa_full_name (display name in Sheet)
- `Region Intern` → region (source of truth for region)
- `Region Operateur` → alternative region (use `Region Intern` as primary)

**Form Responses columns** (exact names from Sheet):
- `Timestamp`
- `Email Address`
- `Nom du TSA`
- `Numéro corporate`
- `Region` (user-entered, may differ from TSA_List - normalize with REGION_NORMALIZATION)

**Usage**: 
- Sync script: Map MongoDB `agentId` (TSA ID) → `corporate_num` via `tsa_reference` table
- AppsScript: Read `tsa_reference` table for `tsa_full_name` and `region`
- **Region source**: Use `Region Intern` from TSA_List (not the Form's `Region` column)

**Sample data** (from project file lines 57-62):
```
TSA ID (mongo_id)       Numero Corporate  TSA Full NAME         Region Intern
65b802cb5f4550e109532d47  69397895        ABDOU WAHABOU OKPE    NORD EST
65b80c8d5f4550e109532eba  69397770        DJACOTO FRANCIS       NORD EST
```

**Why corporate number**: The Google Form has a "Numéro corporate" field, and MongoDB `pos.agentId` maps to TSA ID which links to corporate number via TSA_List.xlsx.

## Implementation Steps

1. **Create `connections/config.py`**
   Complete example based on `D:\LKA\lka_client_pipeline\connections\config.py`:
   ```python
   import os
   from sqlalchemy.engine import make_url
   from sqlalchemy import create_engine

   # MongoDB
   MONGO_URI = os.getenv("MONGO_URI", "mongodb://lkaBi229:gBLocal24@38.242.195.126:27018/pulse_benin")
   MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "pulse_benin")

   # MySQL
   MYSQL_HOST = os.getenv("MYSQL_HOST", "75.119.154.255")
   MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
   MYSQL_USER = os.getenv("MYSQL_USER", "root")
   MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "LkaRoot2025Secure!")
   MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "lka_tsa_deployments")

   # SSH
   SSH_HOST = os.getenv("SSH_HOST", "75.119.154.255")
   SSH_USER = os.getenv("SSH_USER", "root")
   SSH_PASS = os.getenv("SSH_PASS", "8i8Jlnuyz~2cKisB")
   SSH_PORT = int(os.getenv("SSH_PORT", "22"))

   # Tables
   TABLE_DEPLOYMENTS = "deployments_daily"
   TABLE_TSA_REF = "tsa_reference"

   def connection_string(database: str = MYSQL_DATABASE) -> str:
       return (
           f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
           f"@{MYSQL_HOST}:{MYSQL_PORT}/{database}?charset=utf8mb4"
       )

   def make_engine(database: str = MYSQL_DATABASE):
       from sqlalchemy import create_engine
       return create_engine(connection_string(database), pool_pre_ping=True)

   # Region normalization (same as lka_client_pipeline)
   REGION_NORMALIZATION = {
       "NORTH EAST": "NORD EST",
       "NORTH WEST": "NORD OUEST",
       "NORTH-EST":  "NORD EST",
       "NORTH-WEST": "NORD OUEST",
       "NORD-EST":   "NORD EST",
       "NORD-OUEST": "NORD OUEST",
       "NORTH-EAST": "NORD EST",
       "SOUTH WEST": "SUD OUEST",
       "SOUTH-WEST": "SUD OUEST",
       "SOUTH EAST": "SUD EST",
       "SOUTH-EAST": "SUD EST",
       "SUD-EST":    "SUD EST",
       "SUD-OUEST":  "SUD OUEST",
       "Atlantique": "ATLANTIQUE",
       "PLATEAU":    "SUD EST",
   }
   ```
   *Note: For server deployment, `.env` sets `MYSQL_HOST=127.0.0.1` because MySQL runs on localhost there.*

2. **Create MySQL setup script** (`mysql/setup_deployments_db.py`)
   - Follow `setup_tsa.py` pattern with SSH + docker exec
   - Create database `lka_tsa_deployments`
   - Create table `deployments_daily` (keyed by `corporate_num`)
   - Create table `tsa_reference` (loaded from TSA_List.xlsx)
   - Grant root-only access

3. **Create MongoDB→MySQL sync script** (`sync_deployments_to_mysql.py`)
   - Load TSA_List.xlsx into `tsa_reference` table
   - Connect to MongoDB with retries (pattern from `update_tsa_performance.py`)
   - Query `pos` collection, map `agentId` → `corporate_num`
   - Upsert to `deployments_daily` with INSERT ... ON DUPLICATE KEY UPDATE
   - Monthly reset: `DELETE FROM deployments_daily WHERE deployment_date < DATE_FORMAT(NOW(), '%Y-%m-01')`
   - Run this script locally first to test

4. **Create `run_server.sh`**
   ```bash
   #!/bin/bash
   cd /opt/tsa_deployments
   source venv/bin/activate
   python sync_deployments_to_mysql.py >> logs/sync.log 2>&1
   ```

5. **Create deployment script** (`deploy_server.py`)
   - Follow `deploy_server.py` pattern but **no Git** - SFTP direct
   - SSH connect with retries
   - Create remote dir `/opt/tsa_deployments` and `/opt/tsa_deployments/logs`
   - SFTP upload using `Path(__file__).parent` for local paths:
     ```python
     base_dir = Path(__file__).parent
     sftp_upload_file(sftp, base_dir / "sync_deployments_to_mysql.py", f"{REMOTE_BASE}/sync_deployments_to_mysql.py")
     sftp_upload_file(sftp, base_dir / "connections" / "config.py", f"{REMOTE_BASE}/connections/config.py")
     sftp_upload_file(sftp, base_dir / ".env", f"{REMOTE_BASE}/.env")
     sftp_upload_file(sftp, base_dir / "TSA_List.xlsx", f"{REMOTE_BASE}/TSA_List.xlsx")
     ```
   - Create venv + `pip install` dependencies
   - Upload `run_server.sh` + `chmod +x`
   - Install cron job: `0 * * * * /opt/tsa_deployments/run_server.sh`

6. **Create Google AppsScript** (`apps_script/update_sheet.gs`)
   - Bound script to existing sheet
   - Read "Form Responses" tab for transmissions (group by `Numéro corporate`)
   - Read MySQL `deployments_daily` via JDBC with retries
   - Read MySQL `tsa_reference` for TSA full names
   - Update Summary tab: TSA Full Name, Region, Transmissions, Deployments
   - Update Daily Details tab: Date, TSA Full Name, Region, Transmissions, Deployments
   - Create hourly trigger
   - Format columns (widths, headers bold)

7. **Local testing**
   - Test MySQL setup script
   - Test MongoDB sync script
   - Verify `deployments_daily` has data
   - Verify `tsa_reference` loaded correctly
   - Test AppsScript manually (run `updateSummarySheet()`)
   - Check data joins correctly

8. **Deploy to server**
   - Run `deploy_server.py`
   - Verify cron job: `crontab -l`
   - Verify files in `/opt/tsa_deployments/`
   - Monitor logs: `tail -f /opt/tsa_deployments/logs/sync.log`

## Key Reference Patterns

### MySQL Retry Pattern
From `D:\LKA\lka_client_pipeline\connections\connect.py`:
- Use `pool_pre_ping=True` for connection health
- Test connection before operations

### MongoDB Retry Pattern
From `D:\LKA\LKA_Automations\pipelines\tsa_report\update_tsa_performance.py`:
- Exponential backoff: `base_delay + (attempt-1) * 2`
- Max delay cap
- Connection timeout settings

### SSH Deployment Pattern
From `D:\LKA\Perf_commissions\deploy_server.py`:
- Paramiko for SSH/SFTP
- Retry logic for SSH connection
- Cron job management with grep to remove old jobs

### Data Normalization
From `D:\LKA\lka_client_pipeline\connections\config.py`:
```python
REGION_NORMALIZATION = {
    "NORTH EAST": "NORD EST",
    "NORTH WEST": "NORD OUEST",
    "NORTH-EST":  "NORD EST",
    "NORTH-WEST": "NORD OUEST",
    "NORD-EST":   "NORD EST",
    "NORD-OUEST": "NORD OUEST",
    "NORTH-EAST": "NORD EST",
    "SOUTH WEST": "SUD OUEST",
    "SOUTH-WEST": "SUD OUEST",
    "SOUTH EAST": "SUD EST",
    "SOUTH-EAST": "SUD EST",
    "SUD-EST":    "SUD EST",
    "SUD-OUEST":  "SUD OUEST",
    "Atlantique": "ATLANTIQUE",
    "PLATEAU":    "SUD EST",
}
```
- Handle case variations and accents

## File Structure

```
D:\LKA\TSA flushed\
├── project                          # Project documentation
├── TSA_List.xlsx                    # TSA reference data (source of truth for TSA mapping)
├── mysql\
│   └── setup_deployments_db.py     # MySQL setup script (SSH + docker exec)
├── connections\
│   └── config.py                    # Central config (MYSQL_HOST, MONGO_URI, SSH creds)
├── sync_deployments_to_mysql.py     # MongoDB → MySQL sync (runs on server)
├── deploy_server.py                 # SFTP deployment to server (no Git)
├── run_server.sh                    # Wrapper script executed by cron
├── apps_script\
│   └── update_sheet.gs              # Google AppsScript (bound to Sheet)
└── .env                             # Environment variables (local + server)
```

## Success Criteria

- `tsa_reference` table loaded from TSA_List.xlsx (mongo_id → corporate_num → full_name)
- `deployments_daily` table stores daily deployment counts keyed by `corporate_num`
- Sync script runs hourly on server via cron
- Google Sheet Summary tab shows: TSA Full Name, Region, Transmissions, Deployments
- Google Sheet Daily Details tab shows per-day breakdown by TSA Full Name
- Numéro Corporate is the join key between Form data and MongoDB data
- Data resets on 1st of each month
- All connections use retry patterns (MongoDB, MySQL, SSH)
- Root-only access to MySQL database
- No Git repository required for deployment (SFTP direct)

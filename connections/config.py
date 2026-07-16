import os

from dotenv import load_dotenv

load_dotenv()

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
SSH_PASS = os.getenv("SSH_PASS") or "8i8Jlnuyz~2cKisB"
SSH_PORT = int(os.getenv("SSH_PORT") or "22")
# Cle privee ed25519 (contenu PEM complet, secret GitHub SSH_PKEY) - preferee
# au mot de passe : le VPS traverse des fenetres de refus de l'auth mot de
# passe (saturation sshd par brute-force botnet), documentees dans
# D:\LKA\MYSQL_CONNECTION_METHODS.md §6.
SSH_PKEY = os.getenv("SSH_PKEY") or ""

# Tables
TABLE_DEPLOYMENTS = "deployments_daily"
TABLE_TSA_REF = "tsa_reference"

# Region normalization
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


def connection_string(database: str = MYSQL_DATABASE) -> str:
    return (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{database}?charset=utf8mb4"
    )


def make_engine(database: str = MYSQL_DATABASE):
    from sqlalchemy import create_engine
    return create_engine(connection_string(database), pool_pre_ping=True)

"""Configuration centrale : variables d'environnement + règles métier par défaut."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Base de données ---
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'loickaton.db'}")
if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
    # Chemin relatif -> relatif à la racine du projet
    DATABASE_URL = f"sqlite:///{BASE_DIR / DATABASE_URL.removeprefix('sqlite:///')}"

# --- IA (Mistral uniquement) ---
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_API_URL = os.getenv("MISTRAL_API_URL", "https://api.mistral.ai/v1")
AI_MODE = os.getenv("AI_MODE", "auto")  # auto | mock | live
MISTRAL_MODEL_LARGE = os.getenv("MISTRAL_MODEL_LARGE", "mistral-large-latest")
MISTRAL_MODEL_SMALL = os.getenv("MISTRAL_MODEL_SMALL", "mistral-small-latest")
MISTRAL_EMBED_MODEL = os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed")
MISTRAL_MODEL_CHAT = os.getenv("MISTRAL_MODEL_CHAT", MISTRAL_MODEL_LARGE)  # modèle de l'assistant (chatbot)
MISTRAL_TIMEOUT_SECONDS = float(os.getenv("MISTRAL_TIMEOUT_SECONDS", "20"))
# Après un échec en mode auto, on reste en simulé pendant ce délai (évite d'attendre l'API à chaque page)
AI_RETRY_AFTER_SECONDS = int(os.getenv("AI_RETRY_AFTER_SECONDS", "300"))

# --- Règles métier (valeurs par défaut, à ajuster) ---
NOTIFY_DAYS_BEFORE_DEFAULT = 7       # J-x : notification de l'associé
RESPONSE_DELAY_HOURS = 4             # délai de réponse d'un collaborateur avant passage au suivant
MIN_PROPOSALS = 3                    # nombre minimum de collaborateurs choisis par l'associé
DEADLINE_REMINDER_DAYS = 2           # rappel au responsable avant la deadline
TIME_ROUNDING_MINUTES = 6            # arrondi au 1/10e d'heure
INTERN_TIME_BILLABLE = False         # temps stagiaire non facturable par défaut
BUDGET_ALERT_THRESHOLDS = (80, 100)  # alertes de budget en %

# Journée de travail (pour calculer les disponibilités à partir de l'agenda)
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 19
LUNCH_START_HOUR = 13
LUNCH_END_HOUR = 14

# --- Matching (voir labels.ELIMINATORY / MAIN_CRITERIA / COMPLEMENTARY_CRITERIA) ---
# Les critères sont pondérés selon leur rang dans la hiérarchie : dans un groupe de n critères, le 1er pèse n,
# le 2e n-1… Les critères principaux comptent pour MAIN_CRITERIA_SHARE du score, les complémentaires pour le reste.
MAIN_CRITERIA_SHARE = 0.8
# Disponibilité minimale (éliminatoire) : heures libres avant la deadline >= effort estimé x ce ratio
MIN_AVAILABILITY_RATIO = 1.0

# --- Planificateur ---
SCHEDULER_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# --- Email (si SMTP non configuré, les emails vont dans la boîte d'envoi de démo) ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@loickaton.local")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

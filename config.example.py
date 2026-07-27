"""
config.example.py

MODÈLE de configuration -- sert uniquement de DOCUMENTATION pour savoir
quelles variables d'environnement définir avant de lancer l'application.
config.py (non versionné dans ce même esprit, mais lui aussi sans secret
réel grâce à os.environ.get) est le fichier réellement utilisé par l'app.

Ne contient aucune valeur réelle -- sûr à committer sur GitHub.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Connexion Oracle ---
# À définir comme variables d'environnement avant de lancer l'appli :
#   set DB_USERNAME=votre_utilisateur
#   set DB_PASSWORD=votre_mot_de_passe
#   set DB_DSN=host:port/service
DB_USERNAME = os.environ.get("DB_USERNAME", "changeme")
DB_PASSWORD = os.environ.get("DB_PASSWORD")  # obligatoire, pas de valeur par défaut
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/orcl")

# --- Chemins ---
XSD_PATH_TEIF = os.path.join(BASE_DIR, "data", "xsd", "facture_INVOIC_V1.8.8_withSig.xsd")
XSD_PATH_TCE = os.path.join(BASE_DIR, "data", "xsd", "tce.xsd")

XML_A_TRAITER = os.path.join(BASE_DIR, "data", "xml", "a_traiter")
XML_TRAITES = os.path.join(BASE_DIR, "data", "xml", "traites")
XML_ERREURS = os.path.join(BASE_DIR, "data", "xml", "erreurs")

# --- Scheduler ---
SCHEDULER_INTERVAL_MINUTES = 5
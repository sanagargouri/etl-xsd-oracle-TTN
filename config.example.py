"""
config.example.py

Modèle de configuration — copie ce fichier en config.py et renseigne
tes propres valeurs. config.py n'est jamais commité (voir .gitignore),
donc ce fichier sert de référence pour savoir quoi renseigner.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Connexion Oracle ---
DB_USERNAME = "ton_utilisateur_oracle"
DB_PASSWORD = "ton_mot_de_passe"
DB_DSN = "localhost:1521/ORCL"

# --- Chemins ---
XSD_PATH = os.path.join(BASE_DIR, "data", "xsd", "ton_fichier.xsd")
XML_A_TRAITER = os.path.join(BASE_DIR, "data", "xml", "a_traiter")
XML_TRAITES = os.path.join(BASE_DIR, "data", "xml", "traites")
XML_ERREURS = os.path.join(BASE_DIR, "data", "xml", "erreurs")

# --- Scheduler ---
SCHEDULER_INTERVAL_MINUTES = 5
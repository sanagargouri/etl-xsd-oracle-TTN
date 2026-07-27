"""
config.py

Centralise toute la configuration de l'application web ETL.

Les identifiants sensibles (mot de passe Oracle) sont lus depuis des
variables d'environnement plutôt qu'écrits en clair ici -- indispensable
avant de pousser ce projet sur un dépôt Git, même privé.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Connexion Oracle ---
# Définir ces variables avant de lancer l'appli, par exemple sous Windows (cmd) :
#   set DB_USERNAME=sana
#   set DB_PASSWORD=votre_mot_de_passe
#   set DB_DSN=localhost:1521/orcl2121
#   python app.py
# Ou de façon permanente : Panneau de configuration > Variables d'environnement.
DB_USERNAME = os.environ.get("DB_USERNAME", "sana")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/orcl2121")

if not DB_PASSWORD:
    raise RuntimeError(
        "La variable d'environnement DB_PASSWORD n'est pas définie. "
        "Configurez-la avant de lancer l'application (voir commentaire "
        "ci-dessus) : ne mettez jamais de mot de passe en clair dans ce fichier."
    )

# --- Chemins ---
XSD_PATH_TEIF = os.path.join(BASE_DIR, "data", "xsd", "facture_INVOIC_V1.8.8_withSig.xsd")
XSD_PATH_TCE = os.path.join(BASE_DIR, "data", "xsd", "tce.xsd")

XML_A_TRAITER = os.path.join(BASE_DIR, "data", "xml", "a_traiter")
XML_TRAITES = os.path.join(BASE_DIR, "data", "xml", "traites")
XML_ERREURS = os.path.join(BASE_DIR, "data", "xml", "erreurs")

# --- Scheduler ---
SCHEDULER_INTERVAL_MINUTES = 5
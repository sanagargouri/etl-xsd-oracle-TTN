"""
app.py

Point d'entrée unique de l'application : lance Flask ET le scheduler ETL
dans le même processus. Remplace l'ancien scheduler.py autonome.

Lancement : python app.py
"""

from datetime import datetime

from flask import Flask

import config
from web.scheduler_manager import SchedulerManager
from web.routes import main_bp


def create_app():
    app = Flask(__name__)

    # Heure de démarrage de CE lancement de app.py — sert à filtrer
    # les fichiers "Terminé" affichés sur le dashboard (uniquement ceux
    # traités depuis ce démarrage, pas tout l'historique de ETL_LOG).
    app.config["APP_START_TIME"] = datetime.now()

    # --- Scheduler : une seule instance, créée et démarrée ici ---
    scheduler = SchedulerManager(
        xsd_path=config.XSD_PATH,
        xml_folder=config.XML_A_TRAITER,
        dossier_traites=config.XML_TRAITES,
        dossier_erreurs=config.XML_ERREURS,
        db_username=config.DB_USERNAME,
        db_password=config.DB_PASSWORD,
        db_dsn=config.DB_DSN,
        interval_minutes=config.SCHEDULER_INTERVAL_MINUTES,
    )
    scheduler.start()

    # Stocké sur app.config : accessible depuis les routes via
    # current_app.config["SCHEDULER"], sans variable globale de module.
    app.config["SCHEDULER"] = scheduler

    app.register_blueprint(main_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    # use_reloader=False : évite qu'une modification de code en cours de test
    # ne redémarre le processus et ne tue le scheduler + son minutage en cours.
    app.run(debug=True, use_reloader=False)
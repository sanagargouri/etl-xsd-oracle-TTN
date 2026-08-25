"""
app.py

Point d'entrée unique de l'application : lance Flask, le scheduler ETL,
ET le générateur DDL (ex-app2) dans le même processus.

Lancement : python app.py
"""

from datetime import datetime

from flask import Flask

import config
from web.scheduler_manager import SchedulerManager
from web.routes import main_bp
from web.ddl_generator import ddl_bp


def create_app():
    app = Flask(__name__)
    app.secret_key = "dev-secret-key"  # a changer en prod (utilisee par flash() du module ddl)

    # Heure de démarrage de CE lancement de app.py — sert à filtrer
    # les fichiers "Terminé" affichés sur le dashboard (uniquement ceux
    # traités depuis ce démarrage, pas tout l'historique de ETL_LOG).
    app.config["APP_START_TIME"] = datetime.now()

    # --- Scheduler : une seule instance, créée et démarrée ici ---
    scheduler = SchedulerManager(
        xml_folder=config.XML_A_TRAITER,
        dossier_traites=config.XML_TRAITES,
        dossier_erreurs=config.XML_ERREURS,
        db_username=config.DB_USERNAME,
        db_password=config.DB_PASSWORD,
        db_dsn=config.DB_DSN,
        interval_minutes=config.SCHEDULER_INTERVAL_MINUTES,
    )
    scheduler.start()

    app.config["SCHEDULER"] = scheduler

    app.register_blueprint(main_bp)
    # Generateur DDL (ex-app2), monte sous /ddl/...
    app.register_blueprint(ddl_bp, url_prefix="/ddl")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, use_reloader=False)
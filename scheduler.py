# scheduler.py
import sys
import time
sys.path.append("src")

from apscheduler.schedulers.blocking import BlockingScheduler
from data_loader import DataLoader
from batch_loader import process_batch, print_batch_summary

# --- Configuration à adapter si besoin ---
XSD_PATH = r"data\xsd\facture_INVOIC_V1.8.8_withoutSig.xsd"
XML_FOLDER = r"data\xml\a_traiter"
INTERVALLE_MINUTES = 5  # fréquence d'exécution du traitement


def job():
    """
    Une exécution du traitement : se connecte à Oracle,
    traite tous les fichiers présents dans XML_FOLDER, puis se déconnecte.
    Chaque appel est indépendant (nouvelle connexion à chaque fois),
    pour éviter qu'une connexion Oracle ne reste ouverte trop longtemps
    entre deux passages du scheduler.
    """
    print(f"\n{'=' * 60}")
    print(f"=== Déclenchement du traitement planifié ===")
    print(f"{'=' * 60}")

    loader = DataLoader(
        username="sana",
        password="Oracle123",
        dsn="localhost:1521/orcl2121"
    )

    try:
        loader.connect()
        loader.create_log_table()  # ne fait rien si la table existe déjà
        results = process_batch(XSD_PATH, XML_FOLDER, loader)
        print_batch_summary(results)
    except Exception as e:
        # Une erreur inattendue (ex: Oracle indisponible) ne doit pas
        # arrêter le scheduler : on log et on attend le prochain passage
        print(f"    ERREUR lors de l'exécution planifiée : {e}")
    finally:
        loader.disconnect()


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(job, "interval", minutes=INTERVALLE_MINUTES)

    print(f"Scheduler démarré : traitement toutes les {INTERVALLE_MINUTES} minute(s)")
    print(f"Dossier surveillé : {XML_FOLDER}")
    print("Appuyez sur Ctrl+C pour arrêter.\n")

    # Lance immédiatement un premier passage au démarrage,
    # sans attendre le premier intervalle complet
    job()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler arrêté.")
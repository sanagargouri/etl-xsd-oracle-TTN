"""
web/scheduler_manager.py

Encapsule APScheduler (BackgroundScheduler) pour piloter le job ETL planifié,
et fournit un déclenchement manuel ("Traiter maintenant") qui réutilise
exactement la même logique que le job planifié.

Ce module ne connaît rien de Flask : il expose juste des méthodes simples
que web/routes.py pourra appeler.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.data_loader import DataLoader
from batch_loader import process_batch

JOB_ID = "etl_job"


def run_etl_job(xsd_path, xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn):
    """
    Exécute UN passage complet du pipeline ETL :
    ouvre une connexion Oracle, traite le dossier a_traiter, ferme la connexion.

    Appelée à la fois par le scheduler (passage automatique) et par
    trigger_now (bouton "Traiter maintenant") — c'est le même code dans les
    deux cas, donc un seul endroit à faire évoluer si le comportement change.
    """
    loader = DataLoader(username=db_username, password=db_password, dsn=db_dsn)
    try:
        loader.connect()
        process_batch(
            xsd_path=xsd_path,
            xml_folder=xml_folder,
            loader=loader,
            dossier_traites=dossier_traites,
            dossier_erreurs=dossier_erreurs,
        )
    finally:
        # Le disconnect() se fait même si process_batch lève une exception,
        # pour ne jamais laisser une connexion Oracle orpheline.
        loader.disconnect()


def trigger_now(xsd_path, xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn):
    """
    Déclenchement manuel via le bouton "Traiter maintenant".
    Ne touche pas au planning du scheduler (n'avance ni ne retarde le
    prochain passage automatique) : c'est un passage en plus, pas un
    remplacement.
    """
    run_etl_job(
        xsd_path=xsd_path,
        xml_folder=xml_folder,
        dossier_traites=dossier_traites,
        dossier_erreurs=dossier_erreurs,
        db_username=db_username,
        db_password=db_password,
        db_dsn=db_dsn,
    )


class SchedulerManager:
    """
    Encapsule le BackgroundScheduler et expose des méthodes simples
    pour web/routes.py : start, pause, resume, change_interval, get_status.
    """

    def __init__(self, xsd_path, xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn, interval_minutes):
        self._scheduler = BackgroundScheduler()
        self._job_kwargs = dict(
            xsd_path=xsd_path,
            xml_folder=xml_folder,
            dossier_traites=dossier_traites,
            dossier_erreurs=dossier_erreurs,
            db_username=db_username,
            db_password=db_password,
            db_dsn=db_dsn,
        )
        self._interval_minutes = interval_minutes

    def start(self):
        """À appeler une seule fois, au démarrage de app.py."""
        self._scheduler.add_job(
            func=run_etl_job,
            trigger=IntervalTrigger(minutes=self._interval_minutes),
            id=JOB_ID,
            kwargs=self._job_kwargs,
            replace_existing=True,
        )
        self._scheduler.start()

    def pause(self):
        self._scheduler.pause_job(JOB_ID)

    def resume(self):
        self._scheduler.resume_job(JOB_ID)

    def change_interval(self, hours=0, minutes=0):
        """
        Change l'intervalle du job planifié.
        hours et minutes sont combinés en un seul intervalle
        (ex: hours=1, minutes=30 → passage toutes les 1h30).
        """
        total_minutes = (hours * 60) + minutes
        if total_minutes <= 0:
            raise ValueError("L'intervalle doit être supérieur à 0 minute.")

        self._interval_minutes = total_minutes
        self._scheduler.reschedule_job(
            JOB_ID,
            trigger=IntervalTrigger(minutes=total_minutes),
        )

    def trigger_now(self):
        """Appelé par la route du bouton "Traiter maintenant"."""
        trigger_now(**self._job_kwargs)

    def get_status(self):
        """
        Renvoie un dict prêt à afficher sur le dashboard :
        actif/en pause, intervalle courant, prochaine exécution prévue.
        """
        heures, minutes = divmod(self._interval_minutes, 60)

        job = self._scheduler.get_job(JOB_ID)
        if job is None:
            return {
                "actif": False,
                "intervalle_minutes": self._interval_minutes,
                "intervalle_heures": heures,
                "intervalle_minutes_restantes": minutes,
                "prochaine_execution": None,
            }

        # APScheduler met next_run_time à None quand le job est en pause.
        return {
            "actif": job.next_run_time is not None,
            "intervalle_minutes": self._interval_minutes,
            "intervalle_heures": heures,
            "intervalle_minutes_restantes": minutes,
            "prochaine_execution": job.next_run_time,
        }
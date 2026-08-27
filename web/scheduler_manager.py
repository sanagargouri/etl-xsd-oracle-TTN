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


def run_etl_job(xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn, dates_par_type=None):
    """
    Exécute UN passage complet du pipeline ETL :
    ouvre une connexion Oracle, synchronise TITRE/TCE avec LIASSE.DOSSIER
    (une periode de dates par schema, configuree sur le dashboard),
    traite le dossier a_traiter (chaque fichier utilise le schéma choisi
    manuellement au dépôt, via la file d'attente DDL_XSD_PENDING_FILES),
    ferme la connexion.

    Appelée à la fois par le scheduler (passage automatique) et par
    trigger_now (bouton "Traiter maintenant") — c'est le même code dans les
    deux cas, donc un seul endroit à faire évoluer si le comportement change.
    """
    loader = DataLoader(username=db_username, password=db_password, dsn=db_dsn)
    try:
        loader.connect()
        process_batch(
            xml_folder=xml_folder,
            loader=loader,
            dates_par_type=dates_par_type,
            dossier_traites=dossier_traites,
            dossier_erreurs=dossier_erreurs,
        )
    finally:
        loader.disconnect()


def trigger_now(xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn, dates_par_type=None):
    """
    Déclenchement manuel via le bouton "Traiter maintenant".
    Ne touche pas au planning du scheduler (n'avance ni ne retarde le
    prochain passage automatique) : c'est un passage en plus, pas un
    remplacement. Utilise la meme periode de dates que le passage
    automatique (configuree sur le dashboard).
    """
    run_etl_job(
        xml_folder=xml_folder,
        dossier_traites=dossier_traites,
        dossier_erreurs=dossier_erreurs,
        db_username=db_username,
        db_password=db_password,
        db_dsn=db_dsn,
        dates_par_type=dates_par_type,
    )


class SchedulerManager:
    """
    Encapsule le BackgroundScheduler et expose des méthodes simples
    pour web/routes.py : start, pause, resume, change_interval,
    change_dates, get_status.
    """

    def __init__(self, xml_folder, dossier_traites, dossier_erreurs,
                 db_username, db_password, db_dsn, interval_minutes):
        self._scheduler = BackgroundScheduler()
        self._dates_par_type = {}  # ex: {"TITRE": ("2010-01-01","2010-12-31"), "TCE": (...)}
        self._job_kwargs = dict(
            xml_folder=xml_folder,
            dossier_traites=dossier_traites,
            dossier_erreurs=dossier_erreurs,
            db_username=db_username,
            db_password=db_password,
            db_dsn=db_dsn,
            dates_par_type=self._dates_par_type,
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
        total_minutes = (hours * 60) + minutes
        if total_minutes <= 0:
            raise ValueError("L'intervalle doit être supérieur à 0 minute.")

        self._interval_minutes = total_minutes
        self._scheduler.reschedule_job(
            JOB_ID,
            trigger=IntervalTrigger(minutes=total_minutes),
        )

    def change_dates(self, dates_par_type):
        """
        Met a jour la periode de dates par schema (TITRE/TCE), utilisee
        aussi bien par le prochain passage automatique que par le bouton
        "Traiter maintenant". dates_par_type : dict
        { "TITRE": (date_debut, date_fin), "TCE": (date_debut, date_fin) }
        (un type absent = pas de synchronisation LIASSE pour ce type).
        """
        self._dates_par_type = dates_par_type
        self._job_kwargs["dates_par_type"] = self._dates_par_type
        self._scheduler.modify_job(JOB_ID, kwargs=self._job_kwargs)

    def get_dates(self):
        """Pour repeupler le formulaire du dashboard avec les valeurs actuelles."""
        return self._dates_par_type

    def trigger_now(self):
        """Appelé par la route du bouton "Traiter maintenant"."""
        trigger_now(**self._job_kwargs)

    def get_status(self):
        heures, minutes = divmod(self._interval_minutes, 60)

        job = self._scheduler.get_job(JOB_ID)
        if job is None:
            return {
                "actif": False,
                "intervalle_minutes": self._interval_minutes,
                "intervalle_heures": heures,
                "intervalle_minutes_restantes": minutes,
                "prochaine_execution": None,
                "dates_par_type": self._dates_par_type,
            }

        return {
            "actif": job.next_run_time is not None,
            "intervalle_minutes": self._interval_minutes,
            "intervalle_heures": heures,
            "intervalle_minutes_restantes": minutes,
            "prochaine_execution": job.next_run_time,
            "dates_par_type": self._dates_par_type,
        }
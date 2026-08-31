"""
web/stats.py

Requêtes de lecture sur ETL_LOG (fichiers XML) et TRACE_EXECUTION
(synchro LIASSE.DOSSIER par dossier), pour le dashboard et /historique.

Chaque fonction accepte un paramètre optionnel `loader` :
- si fourni (connexion déjà ouverte, ex. depuis la route dashboard),
  elle est réutilisée et n'est PAS fermée ici.
- si absent, la fonction ouvre et ferme sa propre connexion comme avant
  (comportement inchangé pour tout code qui l'appelle isolément).
"""

import os

from src.data_loader import DataLoader
import config


def _connect():
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )
    loader.connect()
    return loader


def get_log_entries(statut=None, limit=100, loader=None):
    """
    Retourne les entrées de ETL_LOG, les plus récentes en premier.

    statut : 'OK', 'ERREUR', ou None pour ne pas filtrer.
    limit  : nombre maximum d'entrées retournées.
    loader : connexion Oracle déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = _connect()
    try:
        if statut:
            loader.cursor.execute(
                """
                SELECT nom_fichier, date_traitement, statut,
                       lignes_chargees, message_erreur, duree_secondes
                FROM ETL_LOG
                WHERE statut = :1
                ORDER BY date_traitement DESC
                FETCH FIRST :2 ROWS ONLY
                """,
                [statut, limit],
            )
        else:
            loader.cursor.execute(
                """
                SELECT nom_fichier, date_traitement, statut,
                       lignes_chargees, message_erreur, duree_secondes
                FROM ETL_LOG
                ORDER BY date_traitement DESC
                FETCH FIRST :1 ROWS ONLY
                """,
                [limit],
            )

        columns = ["nom_fichier", "date_traitement", "statut",
                   "lignes_chargees", "message_erreur", "duree_secondes"]
        return [dict(zip(columns, row)) for row in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_trace_execution_entries(statut=None, limit=100, loader=None):
    """
    Retourne les entrées de TRACE_EXECUTION (synchro LIASSE.DOSSIER par
    dossier, une ligne par NUMERO_DOSSIER traité), les plus récentes en
    premier. Table distincte de ETL_LOG (qui reste dediee aux fichiers
    XML) -- rien n'est supprime, /historique affiche desormais celle-ci.

    statut : 'OK', 'ERREUR', ou None pour ne pas filtrer.
    limit  : nombre maximum d'entrées retournées.
    loader : connexion Oracle déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = _connect()
    try:
        if statut:
            loader.cursor.execute(
                """
                SELECT NUMERO_DOSSIER, DATE_EXECUTION, DUREE,
                       LIGNES_CHARGEES, STATUT, ERREUR
                FROM TRACE_EXECUTION
                WHERE STATUT = :1
                ORDER BY DATE_EXECUTION DESC
                FETCH FIRST :2 ROWS ONLY
                """,
                [statut, limit],
            )
        else:
            loader.cursor.execute(
                """
                SELECT NUMERO_DOSSIER, DATE_EXECUTION, DUREE,
                       LIGNES_CHARGEES, STATUT, ERREUR
                FROM TRACE_EXECUTION
                ORDER BY DATE_EXECUTION DESC
                FETCH FIRST :1 ROWS ONLY
                """,
                [limit],
            )

        columns = ["numero_dossier", "date_execution", "duree",
                   "lignes_chargees", "statut", "erreur"]
        return [dict(zip(columns, row)) for row in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_completed_since(start_time, loader=None):
    """
    Retourne les fichiers traités avec succès (statut OK) depuis start_time
    uniquement — pas tout l'historique de ETL_LOG, seulement ce qui a été
    traité pendant CE lancement de app.py.

    loader : connexion Oracle déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = _connect()
    try:
        loader.cursor.execute(
            """
            SELECT nom_fichier, date_traitement, statut,
                   lignes_chargees, message_erreur, duree_secondes
            FROM ETL_LOG
            WHERE statut = 'OK' AND date_traitement >= :1
            ORDER BY date_traitement DESC
            """,
            [start_time],
        )
        columns = ["nom_fichier", "date_traitement", "statut",
                   "lignes_chargees", "message_erreur", "duree_secondes"]
        return [dict(zip(columns, row)) for row in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_dashboard_stats(loader=None):
    """
    Retourne les stats affichées sur le dashboard :
    fichiers traités et erreurs des dernières 24h, taux de conformité,
    et nombre de fichiers actuellement en attente dans a_traiter.

    loader : connexion Oracle déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = _connect()
    try:
        loader.cursor.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN statut = 'OK' THEN 1 ELSE 0 END),
                SUM(CASE WHEN statut = 'ERREUR' THEN 1 ELSE 0 END)
            FROM ETL_LOG
            WHERE date_traitement >= SYSTIMESTAMP - INTERVAL '24' HOUR
            """
        )
        total, ok, erreurs = loader.cursor.fetchone()
        total = total or 0
        ok = ok or 0
        erreurs = erreurs or 0

        taux_conformite = round((ok / total) * 100, 1) if total > 0 else None

        return {
            "fichiers_traites_24h": total,
            "erreurs_24h": erreurs,
            "taux_conformite": taux_conformite,
            "en_attente": _count_pending_files(),
        }
    finally:
        if own_connection:
            loader.disconnect()


def _count_pending_files():
    """Compte les .xml actuellement dans a_traiter (pas encore traités)."""
    if not os.path.isdir(config.XML_A_TRAITER):
        return 0
    return len([
        f for f in os.listdir(config.XML_A_TRAITER)
        if f.lower().endswith(".xml")
    ])


def get_pending_files():
    """
    Liste les fichiers .xml actuellement présents dans a_traiter,
    donc pas encore repris par un passage du scheduler.
    Triés par date de dépôt (les plus anciens en premier), avec leur
    taille pour affichage.

    Ne touche pas Oracle (lecture disque uniquement) : pas de paramètre
    loader ici, rien à réutiliser.
    """
    if not os.path.isdir(config.XML_A_TRAITER):
        return []

    files = []
    for f in os.listdir(config.XML_A_TRAITER):
        if not f.lower().endswith(".xml"):
            continue
        full_path = os.path.join(config.XML_A_TRAITER, f)
        try:
            stat = os.stat(full_path)
            files.append({
                "nom_fichier": f,
                "taille_ko": round(stat.st_size / 1024, 1),
                "depose_le": stat.st_mtime,
            })
        except OSError:
            # Le fichier a pu être déplacé entre-temps par un passage
            # du scheduler qui tourne en parallèle : on l'ignore simplement.
            continue

    files.sort(key=lambda x: x["depose_le"])
    return files
"""
web/routes.py

Toutes les routes de l'application web ETL.
"""

import os
from datetime import datetime

from flask import (
    Blueprint, current_app, render_template, redirect, url_for, request
)
from werkzeug.utils import secure_filename

import config
from web import stats
from web import schema_info
from web import table_browser
from web import schema_manager

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def dashboard():
    scheduler = current_app.config["SCHEDULER"]
    status = scheduler.get_status()
    dashboard_stats = stats.get_dashboard_stats()
    recent_logs = stats.get_log_entries(limit=6)

    # Fusion : fichiers en attente (a_traiter) + fichiers terminés depuis
    # le démarrage de CE lancement de app.py (pas tout l'historique).
    pending = stats.get_pending_files()
    completed = stats.get_completed_since(current_app.config["APP_START_TIME"])

    lot_files = (
        [{"nom_fichier": f["nom_fichier"], "statut": "EN_ATTENTE"} for f in pending]
        + [{"nom_fichier": f["nom_fichier"], "statut": "TERMINE"} for f in completed]
    )

    schema_summary = schema_info.get_schema_summary()

    return render_template(
        "dashboard.html",
        status=status,
        stats=dashboard_stats,
        recent_logs=recent_logs,
        lot_files=lot_files,
        schema_summary=schema_summary,
    )


@main_bp.route("/traiter-maintenant", methods=["POST"])
def traiter_maintenant():
    scheduler = current_app.config["SCHEDULER"]
    scheduler.trigger_now()
    return redirect(url_for("main.dashboard"))


@main_bp.route("/scheduler/intervalle", methods=["POST"])
def modifier_intervalle():
    scheduler = current_app.config["SCHEDULER"]

    try:
        heures = int(request.form.get("heures", 0))
        minutes = int(request.form.get("minutes", 0))
        scheduler.change_interval(hours=heures, minutes=minutes)
    except (ValueError, TypeError):
        pass

    return redirect(url_for("main.dashboard"))


@main_bp.route("/scheduler/pause-reprise", methods=["POST"])
def pause_reprise_scheduler():
    scheduler = current_app.config["SCHEDULER"]
    status = scheduler.get_status()

    if status["actif"]:
        scheduler.pause()
    else:
        scheduler.resume()

    return redirect(url_for("main.dashboard"))


@main_bp.route("/retirer-du-lot", methods=["POST"])
def retirer_du_lot():
    """
    Retire un fichier XML du dossier a_traiter avant qu'il ne soit pris
    en compte par le prochain passage du scheduler. Le fichier est déplacé
    vers un dossier 'retires' (pas supprimé définitivement).
    """
    filename = request.form.get("filename")

    if not filename:
        return redirect(url_for("main.dashboard"))

    filename = secure_filename(filename)
    source = os.path.join(config.XML_A_TRAITER, filename)

    if os.path.exists(source) and source.lower().endswith(".xml"):
        dossier_retires = os.path.join(
            os.path.dirname(config.XML_A_TRAITER.rstrip("\\/")), "retires"
        )
        os.makedirs(dossier_retires, exist_ok=True)

        destination = os.path.join(dossier_retires, filename)
        if os.path.exists(destination):
            name, ext = os.path.splitext(filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            destination = os.path.join(dossier_retires, f"{name}_{timestamp}{ext}")

        try:
            os.replace(source, destination)
        except OSError as e:
            current_app.logger.warning(f"Impossible de retirer {filename} : {e}")

    return redirect(url_for("main.dashboard"))


@main_bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    uploaded_files = [f for f in request.files.getlist("xml_file") if f and f.filename]

    if not uploaded_files:
        return render_template("upload.html", error="Aucun fichier sélectionné.")

    deposited = []
    errors = []

    for uploaded_file in uploaded_files:
        filename = secure_filename(uploaded_file.filename)

        if not filename.lower().endswith(".xml"):
            errors.append(f"{uploaded_file.filename} (extension refusée)")
            continue

        destination = os.path.join(config.XML_A_TRAITER, filename)

        if os.path.exists(destination):
            name, ext = os.path.splitext(filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{name}_{timestamp}{ext}"
            destination = os.path.join(config.XML_A_TRAITER, filename)

        uploaded_file.save(destination)
        deposited.append(filename)

    return render_template(
        "upload.html",
        success=deposited if deposited else None,
        error=("Fichier(s) refusé(s) : " + ", ".join(errors)) if errors else None,
    )


@main_bp.route("/historique")
def historique():
    statut_filtre = request.args.get("statut")
    if statut_filtre not in (None, "OK", "ERREUR"):
        statut_filtre = None

    entries = stats.get_log_entries(statut=statut_filtre)
    return render_template(
        "historique.html", entries=entries, statut_filtre=statut_filtre
    )


@main_bp.route("/tables-oracle")
def tables_oracle():
    schema_filter = request.args.get("schema") or None
    schemas = table_browser.list_schemas()
    tables = table_browser.list_tables(schema_key=schema_filter)

    selected_table = request.args.get("table")
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1

    table_data = None
    table_ddl = None
    if selected_table:
        table_data = table_browser.get_table_page(selected_table, page=page)
        table_ddl = table_browser.get_table_ddl(selected_table)

    return render_template(
        "tables_oracle.html",
        tables=tables,
        schemas=schemas,
        schema_filter=schema_filter,
        selected_table=selected_table,
        table_data=table_data,
        table_ddl=table_ddl,
    )


@main_bp.route("/schemas", methods=["GET", "POST"])
def schemas():
    """
    Page "Gérer les schémas" : crée les tables Oracle pour un XSD connu,
    avec un nom personnalisé optionnel pour sa table racine. Ce choix est
    ensuite réutilisé automatiquement par le scheduler pour tous les
    futurs dépôts XML du même type (cf. batch_loader._load_custom_root_names).
    """
    message = None
    error = None

    if request.method == "POST":
        schema_key = request.form.get("schema_key")
        action = request.form.get("action")  # "create" ou "rename"

        try:
            if action == "rename":
                new_name = (request.form.get("custom_name") or "").strip()
                schema_manager.rename_schema_root(schema_key, new_name)
                message = f"Table racine de {schema_key} renommée en {new_name.upper()}"
            else:
                schema_manager.create_tables_for_schema(schema_key)
                message = f"Tables créées/vérifiées pour {schema_key}"
        except Exception as e:
            error = str(e)

    xsd_list = schema_manager.list_available_xsd()
    return render_template("schemas.html", xsd_list=xsd_list, message=message, error=error)
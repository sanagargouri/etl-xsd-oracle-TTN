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
from web import ddl_oracle

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
    # Liste des XSD deja utilises dans le generateur DDL (pour la deuxieme
    # section de cette page : inserer un XML dans des tables existantes).
    xsd_historique = ddl_oracle.list_historique()

    if request.method == "GET":
        return render_template("upload.html", xsd_historique=xsd_historique)

    uploaded_files = [f for f in request.files.getlist("xml_file") if f and f.filename]

    if not uploaded_files:
        return render_template("upload.html", error="Aucun fichier sélectionné.", xsd_historique=xsd_historique)

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
        xsd_historique=xsd_historique,
    )


@main_bp.route("/upload/inserer-donnees", methods=["POST"])
def inserer_donnees():
    """
    Flux de la page "Deposer un fichier" : l'utilisateur choisit un XSD
    deja utilise dans le generateur DDL (liste deroulante alimentee par
    DDL_XSD_HISTORIQUE), et depose un ou plusieurs XML. Les fichiers
    rejoignent le dossier a_traiter comme avant (traites par le scheduler
    / bouton "Traiter maintenant"), mais chacun est associe au schema
    choisi ici -- plus de detection automatique du type de document,
    c'est l'utilisateur qui decide au moment du depot.
    """
    xsd_historique = ddl_oracle.list_historique()

    id_historique = request.form.get("id_historique")
    xml_files = [f for f in request.files.getlist("xml_file_insertion") if f and f.filename]

    if not id_historique:
        return render_template(
            "upload.html", error="Choisis un schema deja utilise avant de deposer le(s) XML.",
            xsd_historique=xsd_historique,
        )
    if not xml_files:
        return render_template(
            "upload.html", error="Merci de deposer au moins un fichier XML.",
            xsd_historique=xsd_historique,
        )

    deposited = []
    errors = []

    for xml_file in xml_files:
        filename = secure_filename(xml_file.filename)

        if not filename.lower().endswith(".xml"):
            errors.append(f"{xml_file.filename} (extension refusee)")
            continue

        destination = os.path.join(config.XML_A_TRAITER, filename)

        if os.path.exists(destination):
            name, ext = os.path.splitext(filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{name}_{timestamp}{ext}"
            destination = os.path.join(config.XML_A_TRAITER, filename)

        xml_file.save(destination)
        ddl_oracle.queue_file_for_schema(filename, int(id_historique))
        deposited.append(filename)

    success_insertion = None
    if deposited:
        success_insertion = (
            f"Fichier(s) depose(s) : {', '.join(deposited)}. Ils seront inseres dans les "
            f"tables Oracle au prochain passage du scheduler, ou via 'Traiter maintenant'."
        )

    return render_template(
        "upload.html",
        success_insertion=success_insertion,
        error=("Fichier(s) refuse(s) : " + ", ".join(errors)) if errors else None,
        xsd_historique=xsd_historique,
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
    table_comments = None
    if selected_table:
        table_data = table_browser.get_table_page(selected_table, page=page)
        table_ddl = table_browser.get_table_ddl(selected_table)
        table_comments = table_browser.get_table_comments(selected_table)

    return render_template(
        "tables_oracle.html",
        tables=tables,
        schemas=schemas,
        schema_filter=schema_filter,
        selected_table=selected_table,
        table_data=table_data,
        table_ddl=table_ddl,
        table_comments=table_comments,
    )


@main_bp.route("/tables-oracle/export")
def tables_oracle_export():
    """
    Télécharge le contenu complet d'une table (pas juste la page affichée)
    en CSV ou en instructions INSERT INTO, selon ?format=csv|sql.
    Le nom de table est revalidé côté table_browser avant toute requête SQL.
    """
    from flask import Response

    table_name = request.args.get("table")
    export_format = request.args.get("format", "csv")

    if not table_name:
        return redirect(url_for("main.tables_oracle"))

    if export_format == "sql":
        content = table_browser.build_insert_export(table_name)
        mimetype = "text/plain"
        extension = "sql"
    else:
        content = table_browser.build_csv_export(table_name)
        mimetype = "text/csv"
        extension = "csv"

    if content is None:
        return redirect(url_for("main.tables_oracle", table=table_name))

    filename = f"{table_name}.{extension}"
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@main_bp.route("/schemas", methods=["GET", "POST"])
def schemas():
    """
    Page "Gérer les schémas" : liste les schémas créés via le Générateur
    DDL et permet de renommer la table racine d'un schéma déjà créé
    (renommage en cascade sur toutes ses tables filles).
    """
    message = None
    error = None

    if request.method == "POST":
        id_historique = request.form.get("id_historique")
        new_name = (request.form.get("custom_name") or "").strip()
        try:
            schema_manager.rename_schema_root(int(id_historique), new_name)
            message = f"Schéma renommé en {new_name.upper()}"
        except Exception as e:
            error = str(e)

    schemas_list = schema_manager.list_schemas()
    return render_template(
        "schemas.html",
        schemas_list=schemas_list,
        message=message,
        error=error,
    )
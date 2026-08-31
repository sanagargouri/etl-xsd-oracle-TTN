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

    # Une seule connexion SANA et une seule connexion LIASSE pour toute
    # la page, au lieu d'une connexion par fonction (cause principale de
    # la lenteur au chargement du dashboard).
    sana_loader = ddl_oracle.connect()
    liasse_loader = ddl_oracle.connect_liasse()
    try:
        dashboard_stats = stats.get_dashboard_stats(loader=sana_loader)
        recent_logs = stats.get_log_entries(limit=6, loader=sana_loader)

        pending = stats.get_pending_files()
        completed = stats.get_completed_since(
            current_app.config["APP_START_TIME"], loader=sana_loader
        )

        lot_files = (
            [{"nom_fichier": f["nom_fichier"], "statut": "EN_ATTENTE"} for f in pending]
            + [{"nom_fichier": f["nom_fichier"], "statut": "TERMINE"} for f in completed]
        )

        schema_summary = schema_info.get_schema_summary(loader=sana_loader)
        schema_types = ddl_oracle.get_schema_types(loader=sana_loader)
        xsd_historique = ddl_oracle.list_historique(loader=sana_loader)

        all_root_names = {h["root_name"] for h in xsd_historique}
        root_table_columns = {
            rn: ddl_oracle.get_root_table_columns(rn, loader=sana_loader) for rn in all_root_names
        }
        schema_column_mappings = {
            rn: ddl_oracle.get_schema_column_mappings(rn, loader=sana_loader) for rn in schema_types
        }

        code_types_dossier = ddl_oracle.get_distinct_code_types_dossier(loader=liasse_loader)
        activites_dossier = ddl_oracle.get_distinct_activites_dossier(loader=liasse_loader)
        dossier_columns = ddl_oracle.get_dossier_columns(loader=liasse_loader)
    finally:
        sana_loader.disconnect()
        liasse_loader.disconnect()

    return render_template(
        "dashboard.html",
        status=status,
        stats=dashboard_stats,
        recent_logs=recent_logs,
        lot_files=lot_files,
        schema_summary=schema_summary,
        schema_types=schema_types,
        xsd_historique=xsd_historique,
        code_types_dossier=code_types_dossier,
        activites_dossier=activites_dossier,
        dossier_columns=dossier_columns,
        root_table_columns=root_table_columns,
        schema_column_mappings=schema_column_mappings,
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

    entries = stats.get_trace_execution_entries(statut=statut_filtre)
    return render_template(
        "historique.html", entries=entries, statut_filtre=statut_filtre
    )


@main_bp.route("/tables-oracle")
def tables_oracle():
    """
    Une seule connexion SANA partagee pour toute la page (schemas, liste
    des tables, page de la table selectionnee, commentaires) au lieu
    d'une connexion par fonction -- meme optimisation que le dashboard.

    Le DDL (DBMS_METADATA.GET_DDL) n'est calcule QUE si demande
    explicitement (?show_ddl=1) -- cette fonction Oracle est lente
    (4-7s observes en test) et n'est pas utile a chaque clic sur une
    table, seulement quand l'utilisateur veut vraiment copier le
    CREATE TABLE.
    """
    schema_filter = request.args.get("schema") or None
    selected_table = request.args.get("table")
    show_ddl = request.args.get("show_ddl") == "1"
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1

    sana_loader = table_browser.connect()
    try:
        schemas = table_browser.list_schemas(loader=sana_loader)
        tables = table_browser.list_tables(schema_key=schema_filter, loader=sana_loader)

        table_data = None
        table_ddl = None
        table_comments = None
        if selected_table:
            table_data = table_browser.get_table_page(selected_table, page=page, loader=sana_loader)
            table_comments = table_browser.get_table_comments(selected_table, loader=sana_loader)
            if show_ddl:
                table_ddl = table_browser.get_table_ddl(selected_table, loader=sana_loader)
    finally:
        sana_loader.disconnect()

    return render_template(
        "tables_oracle.html",
        tables=tables,
        schemas=schemas,
        schema_filter=schema_filter,
        selected_table=selected_table,
        table_data=table_data,
        table_ddl=table_ddl,
        table_comments=table_comments,
        show_ddl=show_ddl,
    )


@main_bp.route("/tables-oracle/export")
def tables_oracle_export():
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


@main_bp.route("/schemas")
def schemas():
    """Page "Gérer les schémas" : liste en lecture seule (renommage retiré)."""
    schemas_list = schema_manager.list_schemas()
    return render_template(
        "schemas.html",
        schemas_list=schemas_list,
    )


@main_bp.route("/schema-types", methods=["POST"])
def modifier_schema_type():
    """
    Cree ou met a jour une correspondance schema <-> CODE_TYPE_DOSSIER /
    ACTIVITE / periode de dates, ainsi que la liste de correspondances de
    colonnes (source LIASSE.DOSSIER -> cible table racine).
    """
    root_name = (request.form.get("root_name") or "").strip()
    code_type_dossier = (request.form.get("code_type_dossier") or "").strip()
    activite_raw = (request.form.get("activite") or "").strip()
    date_debut = request.form.get("date_debut")
    date_fin = request.form.get("date_fin")

    source_columns = request.form.getlist("source_column")
    target_columns = request.form.getlist("target_column")
    mappings = [
        (s.strip(), t.strip())
        for s, t in zip(source_columns, target_columns)
        if s.strip() and t.strip()
    ]

    if root_name and code_type_dossier:
        ddl_oracle.update_schema_type(
            root_name, code_type_dossier, activite_raw, date_debut, date_fin
        )
        ddl_oracle.save_schema_column_mappings(root_name, mappings)

    return redirect(url_for("main.dashboard"))


@main_bp.route("/schema-types/supprimer", methods=["POST"])
def supprimer_schema_type():
    root_name = request.form.get("root_name")
    if root_name:
        ddl_oracle.delete_schema_type(root_name)
    return redirect(url_for("main.dashboard"))
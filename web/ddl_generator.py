"""
web/ddl_generator.py

Blueprint regroupant l'ancienne application "xsd_ddl-ttn-app2"
(generateur de CREATE TABLE generique a partir d'un XSD + XML,
sans base de donnees). Monte dans app.py sous le prefixe /ddl.

Flux (inchange par rapport a l'app2 d'origine) :
  1. GET  /ddl/            -> formulaire : upload XSD + XML + nom table racine
  2. POST /ddl/analyser    -> parse XSD+XML, affiche la liste des tables
                               detectees avec une case a cocher devant chacune
  3. POST /ddl/generer     -> affiche les tables COCHEES remplies avec les
                               vraies valeurs extraites du XML, + le DDL

Les fichiers deposes sont stockes temporairement (dossier local a ce
blueprint, distinct de data/xml/a_traiter/ de l'ETL principal), jamais
dans une base. Le resultat de l'analyse est garde en memoire serveur
(dict Python), identifie par un token, le temps que l'utilisateur coche
ses tables et clique sur "Create table".
"""

import os
import uuid
from collections import Counter

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash
)

from src.xsd_xml_to_ddl import get_available_tables
from web import ddl_oracle

ddl_bp = Blueprint("ddl", __name__)

# Dossier de depot temporaire propre a ce module, pour ne pas melanger
# avec data/xml/a_traiter/ (qui appartient au pipeline ETL principal).
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "uploads_ddl")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Stockage temporaire en memoire (pas de base de donnees) : token -> liste de AvailableTable
ANALYSES = {}


@ddl_bp.route("/", methods=["GET"])
def index():
    return render_template("ddl/index.html")


@ddl_bp.route("/analyser", methods=["POST"])
def analyser():
    xsd_file = request.files.get("xsd_file")
    xml_file = request.files.get("xml_file")
    root_name = (request.form.get("root_name") or "").strip()

    if not xsd_file or xsd_file.filename == "":
        flash("Merci de deposer un fichier XSD.")
        return redirect(url_for("ddl.index"))
    if not xml_file or xml_file.filename == "":
        flash("Merci de deposer un fichier XML.")
        return redirect(url_for("ddl.index"))
    if not root_name:
        flash("Merci de donner un nom a la table racine avant l'extraction.")
        return redirect(url_for("ddl.index"))

    token = uuid.uuid4().hex
    xml_path = os.path.join(UPLOAD_DIR, f"{token}_{xml_file.filename}")
    xml_file.save(xml_path)

    # Copie durable du XSD, sauvegardee UNE SEULE FOIS ici (un objet
    # FileStorage ne peut etre sauvegarde qu'une fois : son flux est
    # entierement consomme apres le premier .save(), un deuxieme .save()
    # ecrirait un fichier vide -- bug corrige ici). Necessaire pour
    # pouvoir reparser ce meme XSD plus tard depuis "Deposer un fichier"
    # / le scheduler, sans le redemander.
    xsd_stored_path = ddl_oracle.store_xsd_permanently(xsd_file, root_name)

    try:
        tables = get_available_tables(xsd_stored_path, xml_path, root_name)
    except Exception as exc:
        flash(f"Erreur lors de l'analyse : {exc}")
        return redirect(url_for("ddl.index"))
    finally:
        os.remove(xml_path)

    if not tables:
        flash("Aucune table detectee (XSD/XML vides ou incompatibles).")
        return redirect(url_for("ddl.index"))

    ANALYSES[token] = {
        "tables": tables,
        "xsd_filename": xsd_file.filename,
        "xsd_stored_path": xsd_stored_path,
        "root_name": root_name,
    }

    table_rows = []
    for i, t in enumerate(tables):
        parent_columns = []
        if t.parent:
            label_counts = Counter(t.parent.column_labels.values())
            for col_name in t.parent.column_types.keys():
                short_label = t.parent.column_labels.get(col_name, col_name)
                display_label = short_label if label_counts[short_label] == 1 else col_name
                parent_columns.append({"value": col_name, "label": display_label})

        # colonnes LOCALES (propres a cette table), pour completer une cle composite
        own_columns = []
        if t.parent:
            own_label_counts = Counter(t.column_labels.values())
            for col_name in t.column_types.keys():
                short_label = t.column_labels.get(col_name, col_name)
                display_label = short_label if own_label_counts[short_label] == 1 else col_name
                own_columns.append({"value": col_name, "label": display_label})

        table_rows.append({
            "index": i,
            "display_name": t.display_name,
            "nb_columns": t.nb_columns,
            "nb_lignes": len(t.rows),
            "has_parent": t.parent is not None,
            "default_fk": f"ID_{t.parent.sql_name}" if t.parent else "",
            "parent_columns": parent_columns,
            "own_columns": own_columns,
        })
    return render_template("ddl/selection.html", token=token, tables=table_rows, root_name=root_name)


@ddl_bp.route("/generer", methods=["POST"])
def generer():
    token = request.form.get("token")
    selected = request.form.getlist("selected_tables")

    analysis = ANALYSES.get(token)
    if analysis is None:
        flash("Session d'analyse expiree, merci de redeposer les fichiers.")
        return redirect(url_for("ddl.index"))
    tables = analysis["tables"]

    if not selected:
        flash("Coche au moins une table avant de generer.")
        return redirect(url_for("ddl.index"))

    selected_idx = {int(i) for i in selected}
    chosen = [t for i, t in enumerate(tables) if i in selected_idx]

    # Cle naturelle choisie pour le LIEN vers le parent (id(parent) -> colonne)
    pk_override_by_parent = {}
    for i in selected_idx:
        t = tables[i]
        if t.parent is None:
            continue
        typed = (request.form.get(f"fk_{i}") or "").strip().upper()
        default = f"ID_{t.parent.sql_name}"
        if typed and typed != default and typed in t.parent.column_types:
            pk_override_by_parent.setdefault(id(t.parent), typed)

    # Colonne LOCALE choisie pour completer la cle composite de chaque table (index -> colonne)
    local_pk_choice = {}
    for i in selected_idx:
        t = tables[i]
        if t.parent is None:
            continue
        typed_local = (request.form.get(f"pklocal_{i}") or "").strip().upper()
        if typed_local and typed_local in t.column_types:
            local_pk_choice[i] = typed_local

    def fk_for(table):
        if table.parent is None:
            return None
        return pk_override_by_parent.get(id(table.parent))

    def own_pk_columns_for(i, table):
        # cas racine : cle naturelle choisie par un enfant pour LUI (le parent)
        if table.parent is None:
            natural = pk_override_by_parent.get(id(table))
            return [natural] if natural else []
        # cas table fille : cle composite = colonne heritee du lien + colonne locale choisie
        local_col = local_pk_choice.get(i)
        if not local_col:
            return []
        inherited = fk_for(table) or f"ID_{table.parent.sql_name}"
        return [inherited, local_col]

    ddl_text = "\n\n".join(
        tables[i].to_ddl(fk_column_name=fk_for(tables[i]), own_pk_columns=own_pk_columns_for(i, tables[i]))
        for i in sorted(selected_idx)
    )

    # Tables remplies avec les vraies valeurs extraites du XML
    tables_data = []
    for i in sorted(selected_idx):
        t = tables[i]
        fk_col = fk_for(t)
        own_pk_cols = own_pk_columns_for(i, t)
        col_names = list(t.column_types.keys())

        key_cols = own_pk_cols if own_pk_cols else (["ID_" + t.sql_name] if not t.parent or not own_pk_cols else [])
        if not own_pk_cols:
            key_cols = ["ID_" + t.sql_name]

        if t.parent:
            fk_header = fk_col if fk_col else "ID_" + t.parent.sql_name
        else:
            fk_header = None

        # eviter de repeter la colonne FK/cle si deja incluse dans key_cols
        remaining_cols = [c for c in col_names if c not in key_cols]
        headers = key_cols + ([fk_header] if (fk_header and fk_header not in key_cols) else []) + remaining_cols

        rows_out = []
        for row in t.rows:
            parent_row_pk = row.get("ID_PARENT")
            parent_row = (t.parent.rows[parent_row_pk - 1] if (t.parent and parent_row_pk) else {})

            line = []
            for kc in key_cols:
                if kc.startswith("ID_") and kc not in t.column_types:
                    line.append(row.get("ID", ""))
                elif t.parent and fk_col and kc == fk_col:
                    # colonne heritee du parent (fait partie de la cle composite) :
                    # sa valeur vient du PARENT, pas des donnees propres de cette ligne
                    line.append(parent_row.get(fk_col, ""))
                else:
                    line.append(row.get(kc, ""))
            if t.parent and fk_header and fk_header not in key_cols:
                if fk_col:
                    line.append(parent_row.get(fk_col, ""))
                else:
                    line.append(row.get("ID_PARENT", ""))
            for c in remaining_cols:
                line.append(row.get(c, ""))
            rows_out.append(line)
        tables_data.append({"nom": t.sql_name, "headers": headers, "rows": rows_out})

    # --- Creation reelle des tables dans Oracle ---
    # Le texte DDL et les tables affichees ci-dessus restent calcules
    # exactement comme avant (aucun changement a la logique de
    # xsd_xml_to_ddl.py) ; on execute simplement ce meme DDL contre la base.
    selected_tables_with_config = []
    for i in sorted(selected_idx):
        t = tables[i]
        fk_col = fk_for(t)
        pk_cols = own_pk_columns_for(i, t)
        table_ddl = t.to_ddl(fk_column_name=fk_col, own_pk_columns=pk_cols)
        parent_name = t.parent.sql_name if t.parent else None
        selected_tables_with_config.append((i, t, table_ddl, fk_col, pk_cols, parent_name))

    loader = ddl_oracle.connect()
    try:
        ddl_oracle.create_tables_and_record(
            loader,
            xsd_filename=analysis["xsd_filename"],
            xsd_stored_path=analysis["xsd_stored_path"],
            root_name=analysis["root_name"],
            selected_tables_with_config=selected_tables_with_config,
        )
    except Exception as exc:
        flash(
            f"Le DDL a bien ete genere et affiche ci-dessous, mais la creation "
            f"des tables dans Oracle a echoue : {exc}"
        )
    finally:
        loader.disconnect()

    del ANALYSES[token]

    return render_template("ddl/resultat.html", ddl_text=ddl_text, tables_data=tables_data)
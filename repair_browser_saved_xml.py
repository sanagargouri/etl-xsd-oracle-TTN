"""
repair_browser_saved_xml.py

Certains fichiers XML ont été enregistrés depuis l'affichage "view-source"
du navigateur plutôt que le fichier brut d'origine. Le navigateur les a
alors enveloppés dans <article lang=""><para>...</para></article>, une
ligne par <para>, avec les caractères spéciaux (<, >, &) parfois échappés
en entités HTML (&lt;, &gt;, &amp;).

Ce script :
1. Détecte ces fichiers (racine <article> au lieu de <TEIF> ou <DOCUMENT>).
2. Extrait le contenu de chaque <para>, déséchappe les entités HTML,
   et reconstruit le XML original ligne par ligne.
3. Vérifie que le résultat est un XML valide.
4. Écrit le fichier réparé (soit à côté avec suffixe _repare, soit en
   écrasant l'original si --overwrite est passé).

Usage :
    python repair_browser_saved_xml.py data\\xml\\a_traiter
    python repair_browser_saved_xml.py data\\xml\\a_traiter --overwrite
"""

import os
import re
import sys
import html
from lxml import etree


def is_browser_wrapped(file_path):
    """Détecte rapidement si un fichier est de ce format corrompu,
    sans le parser entièrement (juste les premières lignes)."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(500)
    return "<article" in head and "<para>" in head


def extract_original_xml(file_path):
    """
    Reconstruit le XML original à partir du format <article><para>...</para></article>.
    Chaque <para>...</para> correspond à une ligne (ou un fragment de ligne)
    du fichier XML original.
    """
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Extrait le contenu de chaque <para>...</para>, dans l'ordre d'apparition
    lines = re.findall(r"<para>(.*?)</para>", content, re.DOTALL)

    reconstructed = []
    for line in lines:
        # Déséchappe les entités HTML (&lt; -> <, &amp; -> &, etc.)
        unescaped = html.unescape(line)
        # Le navigateur ajoute parfois un espace final artificiel
        unescaped = unescaped.rstrip()
        if unescaped:
            reconstructed.append(unescaped)

    return "\n".join(reconstructed)


def repair_file(file_path, overwrite=False):
    try:
        xml_text = extract_original_xml(file_path)

        # Vérifie que le résultat est un XML bien formé avant d'écrire quoi que ce soit
        etree.fromstring(xml_text.encode("utf-8"))

    except etree.XMLSyntaxError as e:
        return False, f"Échec : le contenu reconstruit n'est pas un XML valide ({e})"
    except Exception as e:
        return False, f"Échec inattendu : {e}"

    if overwrite:
        output_path = file_path
    else:
        base, ext = os.path.splitext(file_path)
        output_path = f"{base}_repare{ext}"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_text)

    return True, output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python repair_browser_saved_xml.py <dossier> [--overwrite]")
        sys.exit(1)

    folder = sys.argv[1]
    overwrite = "--overwrite" in sys.argv

    xml_files = [
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(".xml")
    ]

    corrupted = [f for f in xml_files if is_browser_wrapped(f)]

    if not corrupted:
        print("Aucun fichier au format <article><para> détecté dans ce dossier.")
        return

    print(f"{len(corrupted)} fichier(s) corrompu(s) détecté(s) :\n")

    for file_path in corrupted:
        success, result = repair_file(file_path, overwrite=overwrite)
        name = os.path.basename(file_path)
        if success:
            print(f"  OK  : {name} -> {os.path.basename(result)}")
        else:
            print(f"  ECHEC : {name} -> {result}")


if __name__ == "__main__":
    main()

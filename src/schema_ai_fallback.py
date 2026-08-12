"""
src/schema_ai_fallback.py

Fallback IA (Gemini, tier gratuit) utilisé UNIQUEMENT quand un document XML
arrive avec une racine totalement inconnue et qu'aucun XSD dans data/xsd/
ne définit cette racine (document_router._find_xsd_candidates renvoie une
liste vide).

Ce module n'est JAMAIS appelé pour TEIF ou DOCUMENT (ceux-ci restent
100% déterministes, cf. document_router._resolve_known), ni pour un XML
dont la racine correspond déjà à un XSD présent sur disque (le score de
similarité déterministe suffit dans ce cas).

Principe :
    1. On construit un extrait du XML (balises présentes, quelques valeurs)
    2. On construit la liste des schémas connus (TEIF, DOCUMENT, + tout XSD
       présent dans data/xsd/) avec leurs balises principales
    3. On demande à Gemini si ça ressemble à l'un d'eux, ou si c'est un
       type de document totalement nouveau
    4. La réponse est enregistrée dans ETL_SCHEMA_SUGGESTIONS pour
       validation humaine dans /schemas -- AUCUN traitement automatique
       n'est déclenché à partir de la seule réponse du LLM.
"""

import os
import json
import glob
import xml.etree.ElementTree as PyET
from lxml import etree

XS_NS = "{http://www.w3.org/2001/XMLSchema}"

# Import paresseux : si google-genai n'est pas installé ou si la clé
# n'est pas configurée, le module reste utilisable (les fonctions
# retournent simplement "IA indisponible") plutôt que de faire planter
# tout le pipeline de traitement des fichiers.
#
# Package google-genai (remplace l'ancien google-generativeai, qui est
# officiellement en fin de vie depuis 2026 -- cf. avertissement affiché
# par l'ancien package).
_GEMINI_READY = False
_GEMINI_MODEL_NAME = "gemini-3.5-flash"
try:
    from google import genai
    import config
    if getattr(config, "GEMINI_API_KEY", None):
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
        _GEMINI_READY = True
except Exception:
    _GEMINI_READY = False


class AIFallbackUnavailable(Exception):
    """Levée quand l'appel IA ne peut pas être tenté (pas de clé, pas de
    connexion...). Ne bloque jamais le pipeline : appelant doit juste
    traiter ce cas comme 'aucune suggestion possible'."""
    pass


# -----------------------------------------------------------------------
# Extraction d'un résumé du XML reçu (pas le fichier entier -- on ne
# transmet à l'IA que la structure, jamais les valeurs sensibles si
# possible, pour limiter ce qui quitte le réseau local)
# -----------------------------------------------------------------------
def _extract_xml_sample(xml_path, max_tags=60):
    """Retourne un résumé texte des balises présentes dans le XML,
    avec leur profondeur, pour donner à l'IA une idée de la structure
    sans lui envoyer le contenu complet (données potentiellement
    sensibles dans un contexte de facturation)."""
    lines = []
    depth = 0
    count = 0
    for event, elem in etree.iterparse(xml_path, events=("start", "end")):
        if count >= max_tags:
            break
        local_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if event == "start":
            lines.append("  " * depth + f"<{local_name}>")
            depth += 1
            count += 1
        else:
            depth = max(0, depth - 1)
    return "\n".join(lines)


def _get_known_schemas_tags():
    """Construit {schema_key: {tags principales}} pour TEIF, DOCUMENT, et
    tout XSD présent dans data/xsd/ -- réutilisé pour donner le contexte
    des schémas connus à l'IA."""
    import config as cfg

    schemas = {}

    for key, path in (("TEIF", cfg.XSD_PATH_TEIF), ("DOCUMENT", cfg.XSD_PATH_TCE)):
        try:
            root = PyET.parse(path).getroot()
            tags = {e.get("name") for e in root.findall(f".//{XS_NS}element") if e.get("name")}
            schemas[key] = sorted(tags)[:40]  # limite la taille du prompt
        except Exception:
            continue

    fallback_dir = os.path.dirname(cfg.XSD_PATH_TCE)
    for xsd_path in glob.glob(os.path.join(fallback_dir, "*.xsd")):
        try:
            root = PyET.parse(xsd_path).getroot()
            top = root.find(f"{XS_NS}element")
            root_name = top.get("name") if top is not None else None
            if not root_name or root_name in ("TEIF", "DOCUMENT"):
                continue
            tags = {e.get("name") for e in root.findall(f".//{XS_NS}element") if e.get("name")}
            schemas[f"AUTO_{root_name}"] = sorted(tags)[:40]
        except Exception:
            continue

    return schemas


def _build_prompt(xml_sample, known_schemas):
    schemas_desc = "\n".join(
        f"- {key}: balises principales = {', '.join(tags)}"
        for key, tags in known_schemas.items()
    )
    return f"""Tu es un assistant qui aide à classifier des documents XML de facturation
électronique selon des schémas XSD déjà connus dans un pipeline ETL.

Schémas connus dans le système :
{schemas_desc}

Structure du document XML reçu (balises, sans les valeurs) :
{xml_sample}

Ce document ne correspond à AUCUN schéma connu de façon certaine (sinon il
aurait déjà été traité automatiquement). Ta tâche : dire s'il ressemble
suffisamment à l'un des schémas listés ci-dessus (même avec un vocabulaire
de balises différent, si le concept semble être le même), ou si c'est un
type de document réellement nouveau.

SI et SEULEMENT SI tu juges que c'est un type de document réellement
nouveau (is_new_type = true), propose aussi une ébauche de structure de
table Oracle pour ce document : une liste de colonnes plausibles, une par
balise de donnée significative (ignore les balises purement structurelles
qui ne font que grouper d'autres balises). Pour chaque colonne, déduis un
type SQL Oracle plausible à partir du nom/contexte de la balise :
    - NUMBER(15,2) pour des montants/quantités
    - DATE pour des dates
    - VARCHAR2(255) pour du texte court (noms, identifiants...)
    - VARCHAR2(4000) pour du texte potentiellement long (descriptions...)
Ne propose PAS de clé primaire ni de clé étrangère -- seulement les
colonnes de données. Limite-toi à 30 colonnes maximum.

Réponds UNIQUEMENT en JSON valide, sans aucun texte avant ou après, sans
balises markdown, avec exactement ce format :
{{
  "matched_schema": "<clé exacte d'un schéma ci-dessus, ou null si aucun ne correspond>",
  "confidence": <nombre entre 0 et 1>,
  "justification": "<1-2 phrases expliquant le rapprochement ou l'absence de rapprochement>",
  "is_new_type": <true si ça semble être un type de document totalement nouveau, false sinon>,
  "proposed_root_name": "<nom de table racine suggéré en MAJUSCULES, ex: FACTURE_SIMPLE, uniquement si is_new_type est true, sinon null>",
  "proposed_columns": [
    {{"name": "<NOM_COLONNE en MAJUSCULES>", "sql_type": "<type SQL Oracle>", "source_tag": "<balise XML d'origine>"}}
  ]
}}
Le champ "proposed_columns" doit être une liste vide [] si is_new_type est false."""


def suggest_schema_for_unknown_xml(xml_path):
    """
    Point d'entrée principal. Retourne un dict :
        {
          "matched_schema": str|None,
          "confidence": float,
          "justification": str,
          "is_new_type": bool,
        }
    ou lève AIFallbackUnavailable si l'IA ne peut pas être appelée.
    Ne lève jamais d'autre exception : une erreur de parsing JSON ou
    d'appel réseau est convertie en AIFallbackUnavailable, pour ne
    jamais faire planter le traitement du fichier à cause de l'IA.
    """
    if not _GEMINI_READY:
        raise AIFallbackUnavailable(
            "Clé GEMINI_API_KEY absente de config.py ou package "
            "google-genai non installé (pip install google-genai)."
        )

    try:
        xml_sample = _extract_xml_sample(xml_path)
        known_schemas = _get_known_schemas_tags()
        prompt = _build_prompt(xml_sample, known_schemas)

        response = _gemini_client.models.generate_content(
            model=_GEMINI_MODEL_NAME,
            contents=prompt,
        )

        text = response.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)

        # Validation minimale de la forme attendue -- si Gemini renvoie
        # un JSON malformé ou incomplet, on préfère échouer proprement
        # plutôt que de propager des clés manquantes plus loin.
        proposed_columns = result.get("proposed_columns") or []
        # Filtre défensif : ne garde que des dicts avec les 3 clés
        # attendues, ignore silencieusement toute entrée malformée
        # plutôt que de faire planter tout le traitement pour ça.
        clean_columns = []
        for col in proposed_columns[:30]:
            if isinstance(col, dict) and col.get("name") and col.get("sql_type"):
                clean_columns.append({
                    "name": str(col["name"])[:30],
                    "sql_type": str(col["sql_type"])[:50],
                    "source_tag": str(col.get("source_tag", ""))[:100],
                })

        return {
            "matched_schema": result.get("matched_schema"),
            "confidence": float(result.get("confidence", 0.0)),
            "justification": result.get("justification", ""),
            "is_new_type": bool(result.get("is_new_type", False)),
            "proposed_root_name": result.get("proposed_root_name"),
            "proposed_columns": clean_columns,
        }

    except Exception as e:
        raise AIFallbackUnavailable(f"Appel IA impossible ou réponse invalide : {e}")


# -----------------------------------------------------------------------
# Enregistrement en base pour validation humaine dans /schemas
# -----------------------------------------------------------------------
def save_suggestion(source_file, xml_sample, suggestion, root_tag=None):
    """Enregistre une suggestion IA dans ETL_SCHEMA_SUGGESTIONS pour
    validation manuelle -- ne modifie jamais les tables de données.
    root_tag est la balise racine du XML qui a déclenché ce fallback --
    indispensable pour pouvoir créer un alias root_tag -> schema_key si
    la suggestion est validée plus tard (cf. schema_manager.validate_suggestion)."""
    import config as cfg
    from table_generator import TableGenerator

    proposed_structure = None
    if suggestion.get("proposed_columns"):
        proposed_structure = json.dumps({
            "proposed_root_name": suggestion.get("proposed_root_name"),
            "proposed_columns": suggestion.get("proposed_columns"),
        }, ensure_ascii=False)

    generator = TableGenerator(
        username=cfg.DB_USERNAME, password=cfg.DB_PASSWORD, dsn=cfg.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()
        generator.cursor.execute("""
            INSERT INTO ETL_SCHEMA_SUGGESTIONS
                (source_file, detected_root_tag, xml_sample, suggested_schema_key,
                 confidence_score, justification, proposed_structure)
            VALUES (:source_file, :detected_root_tag, :xml_sample, :suggested_schema_key,
                    :confidence_score, :justification, :proposed_structure)
        """,
            source_file=os.path.basename(source_file),
            detected_root_tag=root_tag,
            xml_sample=xml_sample[:4000] if xml_sample else None,
            suggested_schema_key=suggestion.get("matched_schema"),
            confidence_score=suggestion.get("confidence"),
            justification=suggestion.get("justification", "")[:4000],
            proposed_structure=proposed_structure,
        )
        generator.connection.commit()
    finally:
        generator.disconnect()
# document_router.py
"""
Détecte automatiquement, pour un fichier XML donné, quel XSD et quel
parseur utiliser.

Deux niveaux de détection :

1. CAS CONNUS (déterministe, garanti) :
   - racine <TEIF>      -> XSDParser + XSD facture (fixe)
   - racine <DOCUMENT>  -> XSDParserTCE + tce.xsd (fixe)
   Ces deux cas sont testés en premier et ne dépendent d'aucune heuristique :
   simple comparaison de nom de racine, comme avant. AUCUN appel IA n'a
   jamais lieu pour ces deux cas.

2. CAS INCONNUS (fallback automatique, meilleur effort) :
   Si la racine du XML n'est ni TEIF ni DOCUMENT (un nouveau type de
   document arrivé un jour), on scanne tous les XSD présents dans
   data/xsd/ pour trouver celui dont l'élément racine porte le même nom.
   - Si UN SEUL XSD candidat correspond -> on l'utilise.
   - Si PLUSIEURS candidats portent le même nom de racine -> on les
     départage par score de similarité structurelle (Jaccard), et on
     refuse de deviner si l'écart est trop faible entre les meilleurs
     candidats (fichier vers erreurs/ avec message explicite).
   - Si AUCUN candidat -> avant de rejeter, on tente un appel IA
     (Gemini, gratuit) pour voir si le document ressemble malgré tout à
     un schéma connu avec un vocabulaire de balises différent. La
     suggestion IA n'est JAMAIS appliquée automatiquement : elle est
     seulement enregistrée pour validation humaine dans /schemas
     (table ETL_SCHEMA_SUGGESTIONS). Le fichier reste rejeté (vers
     erreurs/) dans tous les cas -- seul le prochain fichier similaire,
     une fois la suggestion validée par un opérateur, sera traité
     automatiquement.
   Le style structurel du XSD (complexType nommés vs imbrication anonyme
   façon tce.xsd) est détecté automatiquement pour choisir XSDParser ou
   XSDParserTCE.

Noms de table personnalisés :
   Un dict {schema_key: "NOUVEAU_NOM"} peut être fourni au constructeur
   (custom_root_names), typiquement chargé depuis la table Oracle
   ETL_SCHEMA_CONFIG par batch_loader.py avant d'instancier ce routeur.
   Si présent pour un schema_key donné, la table RACINE (et uniquement
   elle) est renommée avant d'être retournée -- les tables enfants
   gardent toujours leur nom automatique.
"""

import os
import glob
import hashlib
from lxml import etree
import xml.etree.ElementTree as PyET

from xsd_parser import XSDParser
from xsd_parser_tce import XSDParserTCE

XS_NS = "{http://www.w3.org/2001/XMLSchema}"


class UnknownDocumentTypeError(Exception):
    """Levée quand la racine du XML ne correspond à aucun schéma connu
    ou identifiable sans ambiguïté."""
    pass


class DocumentRouter:
    def __init__(self, xsd_teif_path, xsd_tce_path, xsd_fallback_dir=None, custom_root_names=None):
        self.xsd_teif_path = xsd_teif_path
        self.xsd_tce_path = xsd_tce_path
        # Dossier scanné pour les types inconnus (par défaut : celui qui
        # contient déjà tce.xsd)
        self.xsd_fallback_dir = xsd_fallback_dir or os.path.dirname(xsd_tce_path)
        self._cache = {}  # {schema_key: (tables, tag_map)}
        # {schema_key: "NOUVEAU_NOM"} -- chargé depuis ETL_SCHEMA_CONFIG
        # par batch_loader.py avant de construire le routeur.
        self.custom_root_names = custom_root_names or {}

    # -----------------------------------------------------------------
    # Lecture de la racine du XML
    # -----------------------------------------------------------------
    def _get_root_tag(self, xml_path):
        try:
            for event, elem in etree.iterparse(xml_path, events=("start",)):
                return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        except etree.XMLSyntaxError as e:
            raise UnknownDocumentTypeError(f"XML mal formé : {e}")
        raise UnknownDocumentTypeError("Fichier XML vide ou sans élément racine")

    # -----------------------------------------------------------------
    # Renommage de la table racine (noms personnalisés)
    # -----------------------------------------------------------------
    def _apply_custom_root_name(self, schema_key, tables, tag_map):
        """
        Renomme UNIQUEMENT la table racine (celle sans 'parent_table')
        selon custom_root_names[schema_key], et répare les FK des tables
        enfants directes qui la référencent dans leur sql_type
        ("... REFERENCES ANCIEN_NOM(...)") et leur parent_table.
        Les tables enfants gardent leur propre nom -- seule la racine change.
        """
        new_name = self.custom_root_names.get(schema_key)
        if not new_name:
            return tables, tag_map

        root = next((t for t in tables if "parent_table" not in t), None)
        if root is None:
            return tables, tag_map

        old_name = root["table_name"]
        if old_name == new_name:
            return tables, tag_map

        root["table_name"] = new_name
        # La colonne PK id_<ancien_nom> garde son nom historique -- seule
        # la table change de nom, pas le schéma de colonnes.

        for table in tables:
            if table.get("parent_table") == old_name:
                table["parent_table"] = new_name
                for col in table["columns"]:
                    if f"REFERENCES {old_name}(" in col.get("sql_type", ""):
                        col["sql_type"] = col["sql_type"].replace(
                            f"REFERENCES {old_name}(", f"REFERENCES {new_name}("
                        )

        if old_name in tag_map:
            tag_map[new_name] = tag_map.pop(old_name)

        return tables, tag_map

    # -----------------------------------------------------------------
    # Point d'entrée public
    # -----------------------------------------------------------------
    def resolve(self, xml_path):
        """
        Retourne (schema_key, tables, tag_map) pour le fichier XML donné.
        Lève UnknownDocumentTypeError si la racine n'est pas reconnue ou
        si plusieurs XSD candidats sont ambigus.
        """
        root_tag = self._get_root_tag(xml_path)

        # --- Cas connus : déterministe, prioritaire, jamais de doute ---
        if root_tag == "TEIF":
            return self._resolve_known("TEIF", self.xsd_teif_path, XSDParser)

        if root_tag == "DOCUMENT":
            return self._resolve_known("DOCUMENT", self.xsd_tce_path, XSDParserTCE)

        # --- Alias validé manuellement (racine différente confirmée
        # équivalente à TEIF/DOCUMENT via une suggestion IA validée dans
        # /schemas) : évite de repasser par l'IA à chaque fois pour un
        # type de document déjà reconnu et confirmé une première fois. ---
        alias_schema_key = self._check_root_alias(root_tag)
        if alias_schema_key == "TEIF":
            print(f"[document_router] Racine <{root_tag}> reconnue via alias validé -> TEIF")
            return self._resolve_known("TEIF", self.xsd_teif_path, XSDParser)
        if alias_schema_key == "DOCUMENT":
            print(f"[document_router] Racine <{root_tag}> reconnue via alias validé -> DOCUMENT")
            return self._resolve_known("DOCUMENT", self.xsd_tce_path, XSDParserTCE)

        # --- Cas inconnu : fallback automatique ---
        return self._resolve_fallback(root_tag, xml_path)

    def _resolve_known(self, schema_key, xsd_path, parser_cls):
        if schema_key not in self._cache:
            print(f"[document_router] Premier fichier de type {schema_key} "
                  f"-> parsing de {os.path.basename(xsd_path)}")
            parser = parser_cls(xsd_path)
            tables, tag_map = parser.parse()
            tables, tag_map = self._apply_custom_root_name(schema_key, tables, tag_map)
            self._cache[schema_key] = (tables, tag_map)
        tables, tag_map = self._cache[schema_key]
        return schema_key, tables, tag_map

    def _resolve_fallback(self, root_tag, xml_path):
        schema_key = f"AUTO_{root_tag}"

        if schema_key in self._cache:
            tables, tag_map = self._cache[schema_key]
            return schema_key, tables, tag_map

        candidates = self._find_xsd_candidates(root_tag)

        if not candidates:
            # Avant de rejeter définitivement, on tente le fallback IA --
            # gratuit (Gemini), et purement consultatif : la suggestion
            # est enregistrée pour validation humaine dans /schemas, mais
            # CE fichier reste rejeté dans tous les cas (vers erreurs/).
            self._try_ai_suggestion(root_tag, xml_path)
            raise UnknownDocumentTypeError(
                f"Racine XML inconnue : <{root_tag}>. Aucun XSD dans "
                f"{self.xsd_fallback_dir} ne définit cet élément racine. "
                f"Ajoutez le XSD correspondant dans ce dossier pour le "
                f"prendre en charge. Une suggestion IA a été enregistrée "
                f"si disponible -- consultez /schemas."
            )

        if len(candidates) > 1:
            best_xsd, best_score, ranking = self._disambiguate_by_signature(candidates, xml_path)

            if best_xsd is None:
                noms = ", ".join(f"{os.path.basename(x)} (score={s:.2f})" for x, s in ranking)
                raise UnknownDocumentTypeError(
                    f"Racine XML ambiguë : <{root_tag}> correspond à plusieurs "
                    f"XSD dont les scores de similarité sont trop proches pour "
                    f"trancher sans risque ({noms}). Traitement refusé. "
                    f"Ajoutez un critère de distinction explicite (namespace, "
                    f"attribut de version...) dans document_router.py."
                )

            print(f"[document_router] Ambiguïté résolue par signature pour "
                  f"<{root_tag}> -> {os.path.basename(best_xsd)} "
                  f"(score={best_score:.2f}, candidats testés : "
                  f"{[os.path.basename(x) for x, _ in ranking]})")
            candidates = [best_xsd]

        xsd_path = candidates[0]
        parser_cls = self._detect_parser_style(xsd_path)

        print(f"[document_router] Nouveau type détecté automatiquement : "
              f"<{root_tag}> -> {os.path.basename(xsd_path)} "
              f"(parseur : {parser_cls.__name__})")

        parser = parser_cls(xsd_path)
        tables, tag_map = parser.parse()
        tables, tag_map = self._apply_custom_root_name(schema_key, tables, tag_map)
        self._cache[schema_key] = (tables, tag_map)
        return schema_key, tables, tag_map

    def _check_root_alias(self, root_tag):
        """
        Consulte ETL_ROOT_ALIASES pour voir si cette racine a déjà été
        confirmée manuellement comme équivalente à TEIF ou DOCUMENT via
        une suggestion IA validée dans /schemas. Retourne le schema_key
        correspondant, ou None si aucun alias n'existe.

        Connexion Oracle légère et indépendante (pas de dépendance à
        TableGenerator ici pour éviter un import circulaire) -- toute
        erreur (table absente, pas encore de connexion possible...) est
        avalée : ce mécanisme est un bonus, jamais un point de blocage
        pour le traitement normal des fichiers.
        """
        try:
            import oracledb
            import config
            conn = oracledb.connect(
                user=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
            )
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ETL_ROOT_ALIASES'"
                )
                if cursor.fetchone()[0] == 0:
                    return None
                cursor.execute(
                    "SELECT schema_key FROM ETL_ROOT_ALIASES WHERE root_tag = :1",
                    [root_tag],
                )
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()
        except Exception as e:
            print(f"[document_router] Vérification d'alias ignorée (erreur) : {e}")
            return None

    # -----------------------------------------------------------------
    # Fallback IA (Gemini) -- purement consultatif, jamais de décision
    # automatique. Ne fait jamais échouer le traitement du fichier : une
    # erreur ici (pas de clé API, pas de réseau, quota dépassé...) est
    # avalée et journalisée, le fichier est rejeté normalement comme
    # avant l'ajout de cette fonctionnalité.
    # -----------------------------------------------------------------
    def _try_ai_suggestion(self, root_tag, xml_path):
        try:
            from schema_ai_fallback import (
                suggest_schema_for_unknown_xml,
                save_suggestion,
                _extract_xml_sample,
                AIFallbackUnavailable,
            )
        except ImportError:
            print("[document_router] Module schema_ai_fallback introuvable "
                  "-- fallback IA ignoré.")
            return

        try:
            suggestion = suggest_schema_for_unknown_xml(xml_path)
            xml_sample = _extract_xml_sample(xml_path)
            save_suggestion(xml_path, xml_sample, suggestion, root_tag=root_tag)
            print(f"[document_router] Suggestion IA enregistrée pour <{root_tag}> : "
                  f"{suggestion.get('matched_schema')} "
                  f"(confiance={suggestion.get('confidence'):.2f})")
        except AIFallbackUnavailable as e:
            print(f"[document_router] Fallback IA indisponible : {e}")
        except Exception as e:
            # Ne doit jamais faire planter le traitement du fichier --
            # l'IA est un bonus, pas une dépendance critique.
            print(f"[document_router] Erreur inattendue du fallback IA "
                  f"(ignorée) : {e}")

    # -----------------------------------------------------------------
    # Désambiguïsation par signature structurelle (SHA + score Jaccard)
    # -----------------------------------------------------------------
    def _xsd_tag_signature(self, xsd_path):
        """
        Extrait l'ensemble de tous les noms de balises déclarés n'importe où
        dans le XSD (peu importe le style d'imbrication : complexType nommés
        ou anonymes, xs:all ou xs:sequence -> .//  capture tout, à toute
        profondeur, sans avoir à connaître la structure).

        Retourne (tag_set, sha256_hex) : l'ensemble sert au score de
        similarité, le SHA256 sert uniquement de preuve d'audit (traçabilité
        de la décision dans les logs), pas de mécanisme de correspondance.
        """
        tree = PyET.parse(xsd_path)
        root = tree.getroot()
        tags = set()
        for elem in root.findall(f".//{XS_NS}element"):
            name = elem.get("name")
            if name:
                tags.add(name)

        signature_text = "|".join(sorted(tags))
        sha256_hex = hashlib.sha256(signature_text.encode("utf-8")).hexdigest()
        return tags, sha256_hex

    def _xml_tag_signature(self, xml_path):
        """
        Extrait l'ensemble des noms de balises réellement présentes dans le
        fichier XML reçu (peu importe la profondeur ou l'ordre).
        """
        tags = set()
        for event, elem in etree.iterparse(xml_path, events=("start",)):
            local_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            tags.add(local_name)
        return tags

    def _disambiguate_by_signature(self, candidates, xml_path):
        """
        Calcule un score de similarité de Jaccard entre les balises
        déclarées par chaque XSD candidat et les balises réellement
        présentes dans le XML reçu, puis choisit le candidat au score le
        plus haut -- mais UNIQUEMENT si l'écart avec le deuxième meilleur
        est net (sinon ambiguïté réelle -> on ne devine pas).

        Retourne (meilleur_xsd_ou_None, meilleur_score, liste_classée).
        """
        SCORE_MIN = 0.5      # score minimum pour considérer un candidat valable
        MARGIN_MIN = 0.15    # écart minimum requis avec le 2e meilleur score

        xml_tags = self._xml_tag_signature(xml_path)

        scored = []
        for xsd_path in candidates:
            xsd_tags, sha256_hex = self._xsd_tag_signature(xsd_path)
            if not xsd_tags or not xml_tags:
                score = 0.0
            else:
                intersection = xsd_tags & xml_tags
                union = xsd_tags | xml_tags
                score = len(intersection) / len(union)

            print(f"[document_router] Signature {os.path.basename(xsd_path)} : "
                  f"sha256={sha256_hex[:12]}... score={score:.3f}")
            scored.append((xsd_path, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        best_xsd, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0.0

        if best_score >= SCORE_MIN and (best_score - second_score) >= MARGIN_MIN:
            return best_xsd, best_score, scored

        return None, best_score, scored

    # -----------------------------------------------------------------
    # Recherche de XSD candidats par nom de racine
    # -----------------------------------------------------------------
    def _find_xsd_candidates(self, root_tag):
        """
        Scanne tous les .xsd du dossier fallback et retourne ceux dont
        l'élément racine (top-level <xs:element name="...">) porte le
        même nom que root_tag.
        """
        candidates = []
        for xsd_path in glob.glob(os.path.join(self.xsd_fallback_dir, "*.xsd")):
            # On exclut les XSD déjà gérés explicitement, pour ne pas les
            # reproposer comme "candidats automatiques".
            if os.path.abspath(xsd_path) in (
                os.path.abspath(self.xsd_teif_path),
                os.path.abspath(self.xsd_tce_path),
            ):
                continue
            try:
                tree = PyET.parse(xsd_path)
                root = tree.getroot()
                top_level_elem = root.find(f"{XS_NS}element")
                if top_level_elem is not None and top_level_elem.get("name") == root_tag:
                    candidates.append(xsd_path)
            except ET_ParseError:
                continue
        return candidates

    def _detect_parser_style(self, xsd_path):
        """
        Devine quel parseur utiliser en inspectant le style du XSD :
        - présence de <xs:complexType name="..."> (types nommés,
          réutilisables) -> style TEIF -> XSDParser
        - sinon (imbrication anonyme) -> style tce.xsd -> XSDParserTCE
        """
        tree = PyET.parse(xsd_path)
        root = tree.getroot()
        has_named_complex_types = root.find(f".//{XS_NS}complexType[@name]") is not None
        return XSDParser if has_named_complex_types else XSDParserTCE


# xml.etree.ElementTree lève ParseError, pas une exception importée par
# défaut sous ce nom -> on la référence proprement ici.
from xml.etree.ElementTree import ParseError as ET_ParseError
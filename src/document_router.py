# document_router.py
"""
Détecte automatiquement, pour un fichier XML donné, quel XSD et quel
parseur utiliser.

Deux niveaux de détection :

1. CAS CONNUS (déterministe, garanti) :
   - racine <TEIF>      -> XSDParser + XSD facture (fixe)
   - racine <DOCUMENT>  -> XSDParserTCE + tce.xsd (fixe)
   Ces deux cas sont testés en premier et ne dépendent d'aucune heuristique :
   simple comparaison de nom de racine, comme avant.

2. CAS INCONNUS (fallback automatique, meilleur effort) :
   Si la racine du XML n'est ni TEIF ni DOCUMENT (un nouveau type de
   document arrivé un jour), on scanne tous les XSD présents dans
   data/xsd/ pour trouver celui dont l'élément racine porte le même nom.
   - Si UN SEUL XSD candidat correspond -> on l'utilise.
   - Si AUCUN candidat -> erreur explicite (fichier vers erreurs/).
   - Si PLUSIEURS candidats portent le même nom de racine -> on refuse
     de deviner (fichier vers erreurs/ avec message explicite), plutôt
     que de risquer de charger des données avec le mauvais schéma.
   Le style structurel du XSD (complexType nommés vs imbrication anonyme
   façon tce.xsd) est détecté automatiquement pour choisir XSDParser ou
   XSDParserTCE.
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
    def __init__(self, xsd_teif_path, xsd_tce_path, xsd_fallback_dir=None):
        self.xsd_teif_path = xsd_teif_path
        self.xsd_tce_path = xsd_tce_path
        # Dossier scanné pour les types inconnus (par défaut : celui qui
        # contient déjà tce.xsd)
        self.xsd_fallback_dir = xsd_fallback_dir or os.path.dirname(xsd_tce_path)
        self._cache = {}  # {schema_key: (tables, tag_map)}

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

        # --- Cas inconnu : fallback automatique ---
        return self._resolve_fallback(root_tag, xml_path)

    def _resolve_known(self, schema_key, xsd_path, parser_cls):
        if schema_key not in self._cache:
            print(f"[document_router] Premier fichier de type {schema_key} "
                  f"-> parsing de {os.path.basename(xsd_path)}")
            parser = parser_cls(xsd_path)
            tables, tag_map = parser.parse()
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
            raise UnknownDocumentTypeError(
                f"Racine XML inconnue : <{root_tag}>. Aucun XSD dans "
                f"{self.xsd_fallback_dir} ne définit cet élément racine. "
                f"Ajoutez le XSD correspondant dans ce dossier pour le "
                f"prendre en charge."
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
        self._cache[schema_key] = (tables, tag_map)
        return schema_key, tables, tag_map

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
# diagnose_schema.py
#
# Affiche l'erreur de validation EXACTE entre un XML et un XSD donne,
# pour comprendre pourquoi detect_schema() rejette un fichier.
#
# Usage : python diagnose_schema.py data\xsd\facture_INVOIC_V1.8.8_withoutSig.xsd data\xml\a_traiter\facture_001.xml

import sys
import xmlschema

if len(sys.argv) < 3:
    print("Usage: python diagnose_schema.py <xsd> <xml>")
    sys.exit(1)

xsd_path = sys.argv[1]
xml_path = sys.argv[2]

print(f"Chargement du schema : {xsd_path}")
schema = xmlschema.XMLSchema11(xsd_path)

print(f"Validation de : {xml_path}\n")
try:
    schema.validate(xml_path)
    print("VALIDE")
except xmlschema.XMLSchemaValidationError as e:
    print("INVALIDE - erreur de validation :")
    print(e)

from lxml import etree
import os

dossier = 'data/xml/traites'
tags = [
    'ARTICLE_COMMERCE_OBSERVATION',
    'ARTICLE_BANQUE_OBSERVATION',
    'ARTICLE_TECHNIQUE_OBSERVATION',
    'BANQUE_OBSERVATION',
    'ORGANISME_OBSERVATION'
]

found_any = False
for f in sorted(os.listdir(dossier)):
    if not f.endswith('.xml'):
        continue
    try:
        tree = etree.parse(f'{dossier}/{f}')
        root = tree.getroot()
        for tag in tags:
            found = root.findall(f'.//{tag}')
            if found:
                print(f'{f} → {tag} : {len(found)}')
                found_any = True
    except Exception as e:
        print(f'Erreur sur {f} : {e}')

if not found_any:
    print('Aucune observation article trouvée dans les XML traités.')
    print('Ces tables sont vides car vos données ne contiennent pas ces balises.')

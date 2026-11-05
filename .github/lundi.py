#!/usr/bin/env python3
"""Exécuté par le workflow .github/workflows/lundi.yml du dépôt public (vignoscope.github.io).

  python3 .github/lundi.py appliquer   publie les pages préparées pour la semaine en cours
                                        (_prochains/AAAA-MM-JJ/, le dossier le plus récent dont la date est passée)
  python3 .github/lundi.py indexnow    signale à IndexNow les pages HTML modifiées par le dernier commit

Aucune dépendance hors bibliothèque standard. LUNDI_DATE=AAAA-MM-JJ remplace la date du jour (tests).
"""
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zoneinfo

RACINE = pathlib.Path(__file__).resolve().parent.parent
BASE = 'https://vignoscope.github.io/'
HOTE = 'vignoscope.github.io'


def aujourdhui():
    return os.environ.get('LUNDI_DATE') or datetime.datetime.now(zoneinfo.ZoneInfo('Europe/Paris')).date().isoformat()


def sortie(cle, valeur):
    f = os.environ.get('GITHUB_OUTPUT')
    if f:
        with open(f, 'a', encoding='utf-8') as o:
            o.write(f'{cle}={valeur}\n')
    print(f'{cle}={valeur}')


def appliquer():
    jour = aujourdhui()
    prep = RACINE / '_prochains'
    dues = sorted(d for d in prep.iterdir() if d.is_dir() and d.name <= jour) if prep.is_dir() else []
    if not dues:
        sortie('change', '0')
        return
    # Chaque dossier contient l’état complet des fichiers concernés à sa date : le plus récent suffit.
    shutil.copytree(dues[-1], RACINE, dirs_exist_ok=True)
    for d in dues:
        shutil.rmtree(d)
    if prep.is_dir() and not any(prep.iterdir()):
        prep.rmdir()
    sortie('change', '1')
    sortie('semaine', dues[-1].name)


def cle_indexnow():
    for f in RACINE.glob('*.txt'):
        t = f.read_text(encoding='utf-8').strip()
        if f.stem == t and len(t) == 32 and all(c in '0123456789abcdef' for c in t):
            return t
    return None


def urls_modifiees():
    noms = subprocess.run(['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'], cwd=RACINE, capture_output=True, text=True).stdout.split()
    urls = []
    for n in noms:
        if not n.endswith('.html') or n.startswith(('_prochains/', '.github/')) or n in ('404.html', 'artifact.html') or n.startswith('google'):
            continue
        if not (RACINE / n).exists():
            continue
        chemin = n[:-len('index.html')] if n.endswith('index.html') else n
        urls.append(BASE + chemin)
    return urls[:10000]


def indexnow():
    cle = cle_indexnow()
    if not cle:
        print('IndexNow : pas de clé publiée, rien à signaler.')
        return
    urls = urls_modifiees()
    if not urls:
        print('IndexNow : aucune page modifiée.')
        return
    corps = json.dumps({'host': HOTE, 'key': cle, 'keyLocation': f'{BASE}{cle}.txt', 'urlList': urls}).encode()
    req = urllib.request.Request('https://api.indexnow.org/indexnow', data=corps, headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f'IndexNow : {len(urls)} adresse(s) signalée(s), réponse {r.status}.')
    except urllib.error.HTTPError as e:
        # 202 : clé en cours de validation ; 403 : clé introuvable ; 422 : adresses hors du site ; 429 : trop de requêtes
        print(f'IndexNow : réponse {e.code} pour {len(urls)} adresse(s).')
    except Exception as e:  # le signalement ne doit jamais bloquer la publication
        print(f'IndexNow : échec ({e}).')


if __name__ == '__main__':
    {'appliquer': appliquer, 'indexnow': indexnow}[sys.argv[1]]()

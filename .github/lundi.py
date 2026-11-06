#!/usr/bin/env python3
"""Exécuté par le workflow .github/workflows/lundi.yml du dépôt public (vignoscope.github.io).

  python3 .github/lundi.py appliquer   publie, dans l’ordre, les semaines préparées dont le lundi est passé
                                        (_prochains/AAAA-MM-JJ/, voir tools/preparer-lundis.cjs) ; un lundi manqué
                                        est rattrapé à l’exécution suivante, une seconde exécution ne change rien
  python3 .github/lundi.py indexnow    signale à IndexNow les pages HTML modifiées par le dernier commit
                                        (toutes celles du plan du site si le message du commit contient [indexnow-tout])
  python3 .github/lundi.py signaler    ouvre (ou complète) une issue « Publication du lundi en échec » avec le rapport

Une semaine ne remplace un fichier que s’il est encore dans l’état où la préparation l’a trouvé. S’il a changé
entre-temps (nouvelle publication manuelle), les deux versions sont fusionnées (git merge-file) ; en cas de conflit,
la semaine n’est pas appliquée et l’échec est signalé. Le plan du site est fusionné entrée par entrée : une page
ajoutée après la préparation y reste.

Aucune dépendance hors bibliothèque standard (et git). LUNDI_DATE=AAAA-MM-JJ remplace la date du jour (tests) ;
LUNDI_RAPPORT=fichier reçoit le rapport d’échec.
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zoneinfo

RACINE = pathlib.Path(__file__).resolve().parent.parent
BASE = 'https://vignoscope.github.io/'
HOTE = 'vignoscope.github.io'
TITRE_ISSUE = 'Publication du lundi en échec'


def aujourdhui():
    return os.environ.get('LUNDI_DATE') or datetime.datetime.now(zoneinfo.ZoneInfo('Europe/Paris')).date().isoformat()


def sortie(cle, valeur):
    f = os.environ.get('GITHUB_OUTPUT')
    if f:
        with open(f, 'a', encoding='utf-8') as o:
            o.write(f'{cle}={valeur}\n')
    print(f'{cle}={valeur}')


def resume(texte):
    f = os.environ.get('GITHUB_STEP_SUMMARY')
    if f:
        with open(f, 'a', encoding='utf-8') as o:
            o.write(texte + '\n')


class Echec(Exception):
    pass


def chemin_rapport():
    """Fichier du rapport d’échec : LUNDI_RAPPORT, sinon le dossier temporaire du runner GitHub (RUNNER_TEMP)."""
    f = os.environ.get('LUNDI_RAPPORT')
    if not f and os.environ.get('RUNNER_TEMP'):
        f = os.path.join(os.environ['RUNNER_TEMP'], 'lundi-rapport.md')
    return f


def empreinte(b):
    return hashlib.sha256(b).hexdigest()


def lire(p):
    return p.read_bytes() if p.exists() else None


def fusionner(actuel, avant, apres, nom):
    """Fusion à trois versions (git merge-file). Renvoie le contenu fusionné ou lève Echec en cas de conflit."""
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        (t / 'actuel').write_bytes(actuel)
        (t / 'avant').write_bytes(avant)
        (t / 'apres').write_bytes(apres)
        r = subprocess.run(['git', 'merge-file', '-p', '-L', 'publié', '-L', 'préparation', '-L', 'semaine',
                            str(t / 'actuel'), str(t / 'avant'), str(t / 'apres')], capture_output=True)
    if r.returncode != 0:
        raise Echec(f'{nom} : modifié depuis la préparation, fusion impossible ({r.returncode if r.returncode > 0 else "erreur"} conflit(s))')
    return r.stdout


def fusionner_plan(xml, plan):
    """Plan du site : retire et ajoute ou remplace des entrées <url> par adresse, sans doublon."""
    if xml is None:
        raise Echec('sitemap.xml absent : plan du site introuvable')
    lignes = xml.decode('utf-8').split('\n')
    retraits = set(plan.get('retraits') or [])
    ajouts = {a['loc']: a['ligne'] for a in plan.get('ajouts') or []}
    sortie_l, vus = [], set()
    for l in lignes:
        m = re.search(r'<loc>([^<]+)</loc>', l)
        if m and '<url>' in l:
            loc = m.group(1)
            if loc in retraits or loc in vus:
                continue
            vus.add(loc)
            if loc in ajouts:
                l = '  ' + ajouts[loc]
        sortie_l.append(l)
    nouveaux = ['  ' + ligne for loc, ligne in ajouts.items() if loc not in vus]
    if nouveaux:
        try:
            i = max(k for k, l in enumerate(sortie_l) if '</urlset>' in l)
        except ValueError:
            raise Echec('sitemap.xml illisible : balise </urlset> introuvable')
        sortie_l[i:i] = nouveaux
    return '\n'.join(sortie_l).encode('utf-8')


def planifier(dossier):
    """Contenus à écrire pour une semaine (chemin relatif → octets), sans rien écrire. Lève Echec."""
    mf = dossier / 'manifeste.json'
    if not mf.exists():
        raise Echec(f'{dossier.name} : manifeste.json absent (préparation incomplète ou ancienne)')
    man = json.loads(mf.read_text(encoding='utf-8'))
    ecrits, erreurs = {}, []
    for chemin, e in sorted(man.get('fichiers', {}).items()):
        apres = lire(dossier / 'apres' / chemin)
        if apres is None or empreinte(apres) != e['apres']:
            erreurs.append(f'{chemin} : version préparée manquante ou altérée')
            continue
        actuel = lire(RACINE / chemin)
        h = empreinte(actuel) if actuel is not None else None
        if h == e['apres']:
            continue                                   # déjà en place
        if h == e['avant']:
            ecrits[chemin] = apres                     # cas normal : rien n’a bougé depuis la préparation
            continue
        avant = lire(dossier / 'avant' / chemin)
        if actuel is None or avant is None:
            erreurs.append(f'{chemin} : {"supprimé" if actuel is None else "créé"} depuis la préparation')
            continue
        try:
            ecrits[chemin] = fusionner(actuel, avant, apres, chemin)
        except Echec as x:
            erreurs.append(str(x))
    if man.get('plan'):
        try:
            nouveau = fusionner_plan(lire(RACINE / 'sitemap.xml'), man['plan'])
            if nouveau != lire(RACINE / 'sitemap.xml'):
                ecrits['sitemap.xml'] = nouveau
        except Echec as x:
            erreurs.append(str(x))
    if erreurs:
        raise Echec(f'semaine du {dossier.name} : ' + ' ; '.join(erreurs))
    return ecrits


def appliquer():
    jour = aujourdhui()
    prep = RACINE / '_prochains'
    dues = sorted(d for d in prep.iterdir() if d.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d.name) and d.name <= jour) if prep.is_dir() else []
    appliquees, change, echec = [], False, None
    for d in dues:                                     # dans l’ordre : chaque semaine part de la précédente
        try:
            ecrits = planifier(d)
        except Echec as x:
            echec = str(x)
            break
        for chemin, contenu in ecrits.items():
            f = RACINE / chemin
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(contenu)
            change = True
        shutil.rmtree(d)
        appliquees.append(d.name)
    if prep.is_dir() and not any(prep.iterdir()):
        prep.rmdir()
    sortie('change', '1' if change else '0')
    if appliquees:
        sortie('semaine', appliquees[-1])
        print('Semaines publiées : ' + ', '.join(appliquees))
        resume('Semaines publiées : ' + ', '.join(appliquees))
    if echec:
        rapport = (f'La publication automatique du {jour} n’a pas pu appliquer toutes les semaines préparées.\n\n'
                   f'- {echec}\n'
                   f'- Semaines appliquées avant l’échec : {", ".join(appliquees) or "aucune"}\n'
                   f'- Semaines restantes : {", ".join(d.name for d in dues[len(appliquees):])}\n\n'
                   'Rien n’a été écrit pour la semaine en échec. Pour corriger : republier depuis les sources '
                   '(`tools/deployer.sh`), ce qui reconstruit les pages et refait la préparation.')
        f = chemin_rapport()
        if f:
            pathlib.Path(f).write_text(rapport, encoding='utf-8')
        print('ÉCHEC : ' + echec, file=sys.stderr)
        resume('**Échec** : ' + echec)
        sortie('echec', '1')


def signaler():
    """Rend l’échec visible : issue ouverte (ou complétée) dans le dépôt, avec le rapport et le lien du journal."""
    f = chemin_rapport()
    rapport = pathlib.Path(f).read_text(encoding='utf-8') if f and pathlib.Path(f).exists() else \
        'Une étape du workflow « Lundi et IndexNow » a échoué (enregistrement, envoi ou redéploiement).'
    run = os.environ.get('GITHUB_RUN_ID')
    if run:
        rapport += f'\n\nJournal : {os.environ.get("GITHUB_SERVER_URL", "https://github.com")}/{os.environ.get("GITHUB_REPOSITORY", "")}/actions/runs/{run}'
    resume(rapport)
    try:
        n = subprocess.run(['gh', 'issue', 'list', '--state', 'open', '--search', f'"{TITRE_ISSUE}" in:title', '--json', 'number', '--jq', '.[0].number'],
                           capture_output=True, text=True, check=True).stdout.strip()
        if n:
            subprocess.run(['gh', 'issue', 'comment', n, '--body', rapport], check=True, capture_output=True)
        else:
            subprocess.run(['gh', 'issue', 'create', '--title', TITRE_ISSUE, '--body', rapport], check=True, capture_output=True)
        print('Échec signalé dans les issues du dépôt.')
    except Exception as e:  # le journal du workflow reste la trace si l’issue ne peut pas être créée
        print(f'Issue impossible ({e}) ; rapport :\n{rapport}', file=sys.stderr)
    sys.exit(1)


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


def urls_plan():
    plan = (RACINE / 'sitemap.xml').read_text(encoding='utf-8') if (RACINE / 'sitemap.xml').exists() else ''
    return re.findall(r'<loc>([^<]+)</loc>', plan)[:10000]


def cle_en_ligne(cle, essais=10, pause=30):
    """Attend que GitHub Pages serve le fichier de clé : IndexNow le vérifie et répond 403 s’il est absent."""
    import time
    for i in range(essais):
        try:
            with urllib.request.urlopen(f'{BASE}{cle}.txt?v={int(time.time())}', timeout=20) as r:
                if r.read().decode('utf-8', 'replace').strip() == cle:
                    return True
        except Exception:
            pass
        if i < essais - 1:
            time.sleep(pause)
    return False


def indexnow():
    cle = cle_indexnow()
    if not cle:
        print('IndexNow : pas de clé publiée, rien à signaler.')
        return
    message = subprocess.run(['git', 'log', '-1', '--format=%B'], cwd=RACINE, capture_output=True, text=True).stdout
    urls = urls_plan() if '[indexnow-tout]' in message else urls_modifiees()
    if urls and not cle_en_ligne(cle):
        print('IndexNow : le fichier de clé n’est pas encore en ligne, signalement reporté.')
        return
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
    {'appliquer': appliquer, 'indexnow': indexnow, 'signaler': signaler}[sys.argv[1]]()

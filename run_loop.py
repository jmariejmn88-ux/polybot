#!/usr/bin/env python3
"""
Fait tourner PolyBot en continu pendant une session GitHub Actions.

Pourquoi : sur le plan gratuit, GitHub ne lance pas un workflow « toutes les
5 minutes » de façon fiable (parfois une fois toutes les 1 à 2 heures). Ici,
chaque lancement garde le bot actif ~5h45 : un passage par minute, et une
sauvegarde des résultats dans le dépôt toutes les 5 minutes (ou dès qu'un pari
est pris ou réglé). Le module sport (sports.py) tourne dans la même boucle.

Usage : python run_loop.py --minutes 345 --every 60 --save-every 300
"""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback

import bot
import sports

ROOT = os.path.dirname(os.path.abspath(__file__))


def git(*args, check=False):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def nb_paris():
    total = []
    for f in (bot.STATE_FILE, sports.STATE_FILE):
        try:
            with open(f) as fh:
                st = json.load(fh)
            total += [len(st.get("open", [])), len(st.get("closed", []))]
        except (OSError, ValueError):
            total += [0, 0]
    return tuple(total)


def sauvegarder(motif):
    git("add", "data/", "learning.md")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", f"PolyBot: {motif} {time.strftime('%H:%M', time.gmtime())}")
    for essai in range(3):
        # En cas de conflit sur data/ (ex. une ancienne session a sauvegardé entre-temps),
        # on garde la version de cette session, qui est la plus récente.
        if git("pull", "--rebase", "-X", "theirs", "--autostash").returncode != 0:
            git("rebase", "--abort")
        if git("push").returncode == 0:
            print(f"💾 résultats sauvegardés ({motif})", flush=True)
            return
        time.sleep(5 * (essai + 1))
    print("⚠️ sauvegarde impossible pour l'instant, nouvel essai au prochain cycle", flush=True)


def main():
    # Repartir de la dernière version du dépôt (une session précédente a pu sauvegarder
    # après le démarrage de celle-ci) et nettoyer un éventuel rebase resté bloqué.
    git("rebase", "--abort")
    git("pull", "--ff-only")
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=345)
    ap.add_argument("--every", type=float, default=60)
    ap.add_argument("--save-every", type=float, default=300)
    a = ap.parse_args()

    # Le résumé GitHub n'est écrit qu'une fois, en fin de session (sinon 345 copies)
    resume_github = os.environ.pop("GITHUB_STEP_SUMMARY", None)
    fin = time.time() + a.minutes * 60
    derniere_sauvegarde = 0.0
    avant = nb_paris()
    tours = 0
    while time.time() < fin:
        debut = time.time()
        try:
            bot.main()
        except Exception:  # noqa: BLE001 — le bot ne doit jamais s'arrêter sur une erreur ponctuelle
            traceback.print_exc()
        if os.environ.get("SPORTS", "1") == "1":
            try:
                sports.main()  # règle les paris finis ; cherche de nouveaux paris toutes les 10 min
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        tours += 1
        apres = nb_paris()
        if apres != avant:
            sauvegarder("nouveau pari ou résultat")
            derniere_sauvegarde = time.time()
        elif time.time() - derniere_sauvegarde >= a.save_every:
            sauvegarder("mise à jour")
            derniere_sauvegarde = time.time()
        avant = apres
        time.sleep(max(1.0, a.every - (time.time() - debut)))
    sauvegarder("fin de session")
    if resume_github:
        os.environ["GITHUB_STEP_SUMMARY"] = resume_github
        bot.write_summary(bot.load_state())
    print(f"Session terminée après {tours} passages.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

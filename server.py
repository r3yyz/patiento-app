#!/usr/bin/env python3
"""Patiento – serveur de l'application.

Un seul fichier, uniquement la bibliothèque standard Python (3.9+).
- Les appels (noms des patients) restent en mémoire vive et s'effacent
  automatiquement : ils ne sont jamais écrits sur disque ni dans les journaux.
- La configuration (salles, messages, réglages, diaporama) est enregistrée
  dans DATA/config.json.

Variables d'environnement :
  APPEL_PORT       port d'écoute (défaut 8080)
  APPEL_HOST       adresse d'écoute (défaut 0.0.0.0)
  APPEL_DATA       dossier des données (défaut ./data)
  APPEL_TTS_MODEL  chemin d'une voix Piper (.onnx) pour l'annonce vocale
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("APPEL_DATA", APP_DIR / "data")).resolve()
SLIDES_DIR = DATA_DIR / "diapos"
CONFIG_FILE = DATA_DIR / "config.json"
PORT = int(os.environ.get("APPEL_PORT", "8080"))
HOST = os.environ.get("APPEL_HOST", "0.0.0.0")
TTS_MODEL = os.environ.get("APPEL_TTS_MODEL", "")

MAX_JSON = 64 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
FORMATS_PRESENTATION = {".pptx", ".ppt", ".ppsx", ".pps", ".odp"}
FORMATS_IMAGE = {".png", ".jpg", ".jpeg"}
CIVILITES = {"Mme": ("Madame", "attendue"), "M.": ("Monsieur", "attendu")}
NOM_VALIDE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿŒœ' \-]{1,40}$")
HEURE_VALIDE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_CONFIG = {
    "cabinet": "Maison de santé",
    # Une salle = un cabinet de consultation, rattaché à un pôle. À adapter dans la page Contenus.
    "salles": (
        [{"salle": str(i), "pole": "Médecine", "praticien": ""} for i in range(1, 8)]      # 6 généralistes + 1 spécialistes
        + [{"salle": str(i), "pole": "Dentaire", "praticien": ""} for i in range(8, 10)]
        + [{"salle": str(i), "pole": "Kinésithérapie", "praticien": ""} for i in range(10, 12)]
        + [{"salle": "12", "pole": "Infirmiers", "praticien": ""}]
    ),
    "messages": [],
    "duree_appel": 25,        # secondes d'affichage plein écran par appel
    "duree_diapo": 10,        # secondes par diapositive
    "duree_historique": 5,    # minutes pendant lesquelles un appel reste listé
    "horaires": {"debut": "08:00", "fin": "21:00"},
    "diaporama": {"version": "", "fichiers": [], "source": "", "date": ""},
}


# --------------------------------------------------------------------------
# Synthèse vocale (facultative)
# --------------------------------------------------------------------------
class Voix:
    """Voix Piper chargée en mémoire ; l'audio n'est jamais écrit sur disque."""

    def __init__(self, modele):
        self.voice = None
        self.lock = threading.Lock()
        if not modele:
            return
        try:
            from piper import PiperVoice  # type: ignore
            self.voice = PiperVoice.load(modele)
            print(f"Voix chargée : {Path(modele).name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Voix indisponible ({exc}) : la synthèse du navigateur sera utilisée.",
                  flush=True)

    @property
    def active(self):
        return self.voice is not None

    def synthetiser(self, texte):
        buf = io.BytesIO()
        with self.lock, wave.open(buf, "wb") as wav:
            if hasattr(self.voice, "synthesize_wav"):
                self.voice.synthesize_wav(texte, wav)
            else:  # anciennes versions de piper-tts
                self.voice.synthesize(texte, wav)
        return buf.getvalue()


# --------------------------------------------------------------------------
# État de l'application
# --------------------------------------------------------------------------
def charger_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"config.json illisible ({exc}), valeurs par défaut utilisées.", flush=True)
    return config


def normaliser_nom(nom):
    nom = re.sub(r"\s+", " ", nom.replace("’", "'").strip())
    if nom.islower() or nom.isupper():
        nom = re.sub(r"[^\s\-']+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), nom)
    return nom


class Store:
    def __init__(self, voix):
        self.cond = threading.Condition()
        self.version = 0
        self.config = charger_config()
        self.voix = voix
        self.attente = []        # appels en attente d'affichage
        self.actuel = None       # appel affiché en plein écran
        self.recents = []        # appels déjà affichés (plus récent en premier)
        self.audio = {}          # cache audio par appel, en mémoire uniquement
        self.conversion = {"etat": "inactif", "message": ""}

    # -- utilitaires -------------------------------------------------------
    def _change(self):
        self.version += 1
        self.cond.notify_all()

    def _enregistrer(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)

    def _salle(self, numero):
        for s in self.config["salles"]:
            if s["salle"] == numero:
                return s
        return None

    def _appel_public(self, appel):
        if appel is None:
            return None
        salle = self._salle(appel["salle"]) or {}
        public = {k: appel[k] for k in ("id", "salle", "civilite", "nom", "precision",
                                         "statut", "cree", "affiche_a", "passage")}
        public["praticien"] = salle.get("praticien", "")
        public["pole"] = salle.get("pole", "")
        public["identite"] = appel["identite"]
        public["phrase"] = appel["phrase"]
        public["vocal"] = appel["vocal"]
        if appel["statut"] == "affiche":
            public["fin"] = appel["affiche_a"] + self.config["duree_appel"]
        return public

    def _trouver(self, appel_id):
        if self.actuel and self.actuel["id"] == appel_id:
            return self.actuel
        for liste in (self.attente, self.recents):
            for a in liste:
                if a["id"] == appel_id:
                    return a
        return None

    def etat_public(self):
        d = self.config["diaporama"]
        images = [f"/diapos/{d['version']}/{f}" for f in d.get("fichiers", [])] if d.get("version") else []
        return {
            "version": self.version,
            "maintenant": time.time(),
            "cabinet": self.config["cabinet"],
            "salles": self.config["salles"],
            "messages": self.config["messages"],
            "duree_appel": self.config["duree_appel"],
            "duree_diapo": self.config["duree_diapo"],
            "duree_historique": self.config["duree_historique"],
            "horaires": self.config["horaires"],
            "diaporama": {"version": d.get("version", ""), "images": images,
                          "source": d.get("source", ""), "date": d.get("date", "")},
            "conversion": self.conversion,
            "actuel": self._appel_public(self.actuel),
            "attente": [self._appel_public(a) for a in self.attente],
            "recents": [self._appel_public(a) for a in self.recents],
            "voix_serveur": self.voix.active,
        }

    # -- boucle de planification ---------------------------------------------
    def boucle(self):
        while True:
            time.sleep(0.5)
            with self.cond:
                maintenant = time.time()
                change = False
                if self.actuel and maintenant - self.actuel["affiche_a"] >= self.config["duree_appel"]:
                    self.actuel["statut"] = "termine"
                    self.recents.insert(0, self.actuel)
                    self.actuel = None
                    change = True
                if self.actuel is None and self.attente:
                    appel = self.attente.pop(0)
                    appel["statut"] = "affiche"
                    appel["affiche_a"] = maintenant
                    appel["passage"] += 1
                    self.actuel = appel
                    change = True
                limite = maintenant - self.config["duree_historique"] * 60
                garder = [a for a in self.recents if a["affiche_a"] >= limite]
                if len(garder) != len(self.recents):
                    for a in self.recents:
                        if a not in garder:
                            self.audio.pop(a["id"], None)
                    self.recents = garder
                    change = True
                if change:
                    self._change()

    # -- actions des praticiens ----------------------------------------------
    def appeler(self, salle, civilite, nom, precision):
        salle = str(salle).strip()
        nom = normaliser_nom(str(nom))
        precision = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]", "", str(precision or ""))[:1].upper()
        if civilite not in CIVILITES:
            raise ValueError("Choisissez M. ou Mme.")
        if not NOM_VALIDE.match(nom):
            raise ValueError("Nom invalide (lettres, espaces, tirets et apostrophes, 40 caractères maximum).")
        with self.cond:
            if self._salle(salle) is None:
                raise ValueError("Salle inconnue.")
            for a in self.attente:
                if a["salle"] == salle and a["nom"] == nom and a["civilite"] == civilite:
                    return self._appel_public(a)
            identite = f"{civilite} {nom}" + (f" {precision}." if precision else "")
            mot, accord = CIVILITES[civilite]
            appel = {
                "id": uuid.uuid4().hex[:12],
                "salle": salle,
                "civilite": civilite,
                "nom": nom,
                "precision": precision,
                "identite": identite,
                "phrase": f"{identite} est {accord} en salle {salle}",
                "vocal": f"{mot} {nom} est {accord} en salle {salle}.",
                "statut": "attente",
                "cree": time.time(),
                "affiche_a": 0,
                "passage": 0,
            }
            self.attente.append(appel)
            self._change()
            public = self._appel_public(appel)
        if self.voix.active:
            threading.Thread(target=self.audio_appel, args=(appel["id"],), daemon=True).start()
        return public

    def rappeler(self, appel_id):
        with self.cond:
            appel = self._trouver(appel_id)
            if appel is None:
                raise KeyError("Appel introuvable.")
            if appel["statut"] == "affiche":
                appel["affiche_a"] = time.time()
                appel["passage"] += 1
            elif appel["statut"] == "termine":
                self.recents.remove(appel)
                appel["statut"] = "attente"
                self.attente.append(appel)
            self._change()
            return self._appel_public(appel)

    def annuler(self, appel_id):
        with self.cond:
            appel = self._trouver(appel_id)
            if appel is None:
                raise KeyError("Appel introuvable.")
            if appel is self.actuel:
                self.actuel = None
            elif appel in self.attente:
                self.attente.remove(appel)
            else:
                self.recents.remove(appel)
            self.audio.pop(appel_id, None)
            self._change()

    def audio_appel(self, appel_id):
        with self.cond:
            appel = self._trouver(appel_id)
            if appel is None or not self.voix.active:
                return None
            if appel_id in self.audio:
                return self.audio[appel_id]
            texte = appel["vocal"]
        donnees = self.voix.synthetiser(texte)
        with self.cond:
            if self._trouver(appel_id) is not None:
                self.audio[appel_id] = donnees
        return donnees

    # -- contenus et réglages -------------------------------------------------
    def ajouter_message(self, texte, jusqu_au):
        texte = re.sub(r"\s+", " ", str(texte)).strip()
        if not 1 <= len(texte) <= 200:
            raise ValueError("Le message doit faire entre 1 et 200 caractères.")
        jusqu_au = str(jusqu_au or "").strip()
        if jusqu_au:
            date.fromisoformat(jusqu_au)  # lève ValueError si invalide
        with self.cond:
            aujourdhui = date.today().isoformat()
            self.config["messages"] = [m for m in self.config["messages"]
                                       if not m.get("jusqu_au") or m["jusqu_au"] >= aujourdhui]
            self.config["messages"].append({"id": uuid.uuid4().hex[:8], "texte": texte,
                                            "jusqu_au": jusqu_au})
            self._enregistrer()
            self._change()

    def supprimer_message(self, message_id):
        with self.cond:
            self.config["messages"] = [m for m in self.config["messages"] if m["id"] != message_id]
            self._enregistrer()
            self._change()

    def definir_salles(self, salles):
        propres, vues = [], set()
        for s in salles:
            numero = re.sub(r"\s+", " ", str(s.get("salle", ""))).strip()[:10]
            praticien = re.sub(r"\s+", " ", str(s.get("praticien", ""))).strip()[:60]
            pole = re.sub(r"\s+", " ", str(s.get("pole", ""))).strip()[:40]
            if not numero:
                continue
            if numero in vues:
                raise ValueError(f"La salle « {numero} » apparaît deux fois.")
            vues.add(numero)
            propres.append({"salle": numero, "pole": pole, "praticien": praticien})
        if not propres:
            raise ValueError("Il faut au moins une salle.")
        with self.cond:
            self.config["salles"] = propres
            self._enregistrer()
            self._change()

    def definir_reglages(self, donnees):
        with self.cond:
            c = self.config
            if "cabinet" in donnees:
                c["cabinet"] = re.sub(r"\s+", " ", str(donnees["cabinet"])).strip()[:80]
            for cle, mini, maxi in (("duree_appel", 5, 120), ("duree_diapo", 3, 120),
                                    ("duree_historique", 1, 30)):
                if cle in donnees:
                    c[cle] = max(mini, min(maxi, int(donnees[cle])))
            if "horaires" in donnees:
                debut = str(donnees["horaires"].get("debut", ""))
                fin = str(donnees["horaires"].get("fin", ""))
                if not (HEURE_VALIDE.match(debut) and HEURE_VALIDE.match(fin)):
                    raise ValueError("Horaires invalides (format HH:MM).")
                c["horaires"] = {"debut": debut, "fin": fin}
            self._enregistrer()
            self._change()

    def supprimer_diaporama(self):
        with self.cond:
            self.config["diaporama"] = dict(DEFAULT_CONFIG["diaporama"])
            self._enregistrer()
            self._change()
        nettoyer_diapos(garder="")

    def etat_conversion(self, etat, message):
        with self.cond:
            self.conversion = {"etat": etat, "message": message}
            self._change()


def nettoyer_diapos(garder):
    if not SLIDES_DIR.exists():
        return
    for dossier in SLIDES_DIR.iterdir():
        if dossier.name != garder:
            shutil.rmtree(dossier, ignore_errors=True)


def convertir_diaporama(store, source, nom_origine):
    """Convertit une présentation, un PDF ou une image en diapositives PNG 1920 px."""
    travail = source.parent
    try:
        ext = source.suffix.lower()
        sortie = travail / "sortie"
        sortie.mkdir()
        if ext in FORMATS_IMAGE:
            shutil.copy(source, sortie / f"diapo-1{ext}")
        else:
            pdf = source
            if ext in FORMATS_PRESENTATION:
                store.etat_conversion("en_cours", "Conversion de la présentation…")
                profil = (travail / "profil").as_uri()
                subprocess.run(
                    ["soffice", f"-env:UserInstallation={profil}", "--headless",
                     "--convert-to", "pdf", "--outdir", str(travail), str(source)],
                    check=True, timeout=600, capture_output=True)
                pdf = travail / (source.stem + ".pdf")
                if not pdf.exists():
                    raise RuntimeError("LibreOffice n'a pas produit de PDF.")
            store.etat_conversion("en_cours", "Création des images…")
            subprocess.run(["pdftoppm", "-png", "-scale-to-x", "1920", "-scale-to-y", "-1",
                            str(pdf), str(sortie / "diapo")],
                           check=True, timeout=600, capture_output=True)
        fichiers = sorted((f.name for f in sortie.iterdir()),
                          key=lambda n: int(re.findall(r"\d+", n)[-1]))
        if not fichiers:
            raise RuntimeError("Aucune diapositive trouvée dans le fichier.")
        version = time.strftime("%Y%m%d-%H%M%S")
        SLIDES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sortie), SLIDES_DIR / version)
        with store.cond:
            store.config["diaporama"] = {"version": version, "fichiers": fichiers,
                                         "source": nom_origine,
                                         "date": time.strftime("%d/%m/%Y à %H:%M")}
            store._enregistrer()
            store.conversion = {"etat": "ok", "message": f"{len(fichiers)} diapositive(s) en ligne."}
            store._change()
        nettoyer_diapos(garder=version)
    except FileNotFoundError as exc:
        store.etat_conversion("erreur", f"Outil manquant sur le serveur : {exc.filename}")
    except subprocess.TimeoutExpired:
        store.etat_conversion("erreur", "La conversion a pris trop de temps.")
    except Exception as exc:  # noqa: BLE001
        store.etat_conversion("erreur", f"Échec de la conversion : {exc}")
    finally:
        shutil.rmtree(travail, ignore_errors=True)


# --------------------------------------------------------------------------
# Serveur HTTP
# --------------------------------------------------------------------------
PAGES = {"/": "accueil.html", "/ecran": "ecran.html", "/appel": "appel.html",
         "/contenus": "contenus.html"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
         ".ico": "image/x-icon", ".woff2": "font/woff2"}


def fichier_sur(base, relatif):
    chemin = (base / relatif).resolve()
    if base.resolve() not in chemin.parents or not chemin.is_file():
        return None
    return chemin


class Handler(BaseHTTPRequestHandler):
    server_version = "Patiento/1.0"
    store: Store = None  # type: ignore

    # Journal minimal : jamais de corps de requête, donc jamais de nom de patient.
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        sys.stderr.write(f"{self.command} {urlparse(self.path).path} : {fmt % args}\n")

    # -- réponses -----------------------------------------------------------
    def envoyer(self, code, corps, type_mime="application/json; charset=utf-8", entetes=None):
        if isinstance(corps, (dict, list)):
            corps = json.dumps(corps, ensure_ascii=False).encode()
        elif isinstance(corps, str):
            corps = corps.encode()
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for cle, valeur in (entetes or {}).items():
            self.send_header(cle, valeur)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corps)

    def erreur(self, code, message):
        self.envoyer(code, {"erreur": message})

    def fichier(self, chemin, cache="no-cache"):
        self.envoyer(200, chemin.read_bytes(),
                     TYPES.get(chemin.suffix.lower(), "application/octet-stream"),
                     {"Cache-Control": cache})

    def json_recu(self):
        taille = int(self.headers.get("Content-Length") or 0)
        if taille > MAX_JSON:
            raise ValueError("Requête trop volumineuse.")
        donnees = json.loads(self.rfile.read(taille) or b"{}")
        if not isinstance(donnees, dict):
            raise ValueError("Format de requête invalide.")
        return donnees

    # -- GET ---------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        chemin = url.path.rstrip("/") or "/"
        if chemin in PAGES:
            return self.fichier(STATIC_DIR / PAGES[chemin])
        if chemin == "/evenements":
            return self.flux()
        if chemin == "/api/etat":
            with self.store.cond:
                return self.envoyer(200, self.store.etat_public())
        if chemin.startswith("/api/voix/"):
            return self.voix(chemin.rsplit("/", 1)[-1])
        if chemin.startswith("/raccourci/") and chemin.endswith(".url"):
            return self.raccourci(unquote(chemin[len("/raccourci/"):-4]))
        if chemin.startswith("/static/"):
            f = fichier_sur(STATIC_DIR, chemin[len("/static/"):])
            return self.fichier(f, "max-age=300") if f else self.erreur(404, "Introuvable.")
        if chemin.startswith("/diapos/"):
            f = fichier_sur(SLIDES_DIR, chemin[len("/diapos/"):])
            return self.fichier(f, "max-age=31536000, immutable") if f else self.erreur(404, "Introuvable.")
        self.erreur(404, "Page introuvable.")

    def flux(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        vu = -1
        try:
            while True:
                with self.store.cond:
                    if self.store.version == vu:
                        self.store.cond.wait(timeout=15)
                    donnees = None
                    if self.store.version != vu:
                        vu = self.store.version
                        donnees = json.dumps(self.store.etat_public(), ensure_ascii=False)
                self.wfile.write(f"data: {donnees}\n\n".encode() if donnees else b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass

    def voix(self, appel_id):
        if not self.store.voix.active:
            return self.erreur(404, "Synthèse vocale serveur non installée.")
        donnees = self.store.audio_appel(appel_id)
        if donnees is None:
            return self.erreur(404, "Appel introuvable.")
        self.envoyer(200, donnees, "audio/wav", {"Cache-Control": "no-store"})

    def raccourci(self, salle):
        with self.store.cond:
            if self.store._salle(salle) is None:
                return self.erreur(404, "Salle inconnue.")
        hote = self.headers.get("Host") or f"localhost:{PORT}"
        contenu = f"[InternetShortcut]\r\nURL=http://{hote}/appel?salle={salle}\r\n"
        nom = re.sub(r"[^\w-]", "_", f"Appel salle {salle}")
        self.envoyer(200, contenu, "application/octet-stream",
                     {"Content-Disposition": f'attachment; filename="{nom}.url"'})

    # -- POST ----------------------------------------------------------------------
    def do_POST(self):
        chemin = urlparse(self.path).path.rstrip("/")
        s = self.store
        try:
            if chemin == "/api/diaporama":
                return self.televerser()
            d = self.json_recu()
            if chemin == "/api/appel":
                return self.envoyer(201, s.appeler(d.get("salle", ""), d.get("civilite", ""),
                                                   d.get("nom", ""), d.get("precision", "")))
            if chemin == "/api/rappel":
                return self.envoyer(200, s.rappeler(str(d.get("id", ""))))
            if chemin == "/api/annulation":
                s.annuler(str(d.get("id", "")))
            elif chemin == "/api/messages":
                s.ajouter_message(d.get("texte", ""), d.get("jusqu_au", ""))
            elif chemin == "/api/messages/suppression":
                s.supprimer_message(str(d.get("id", "")))
            elif chemin == "/api/salles":
                s.definir_salles(d.get("salles") or [])
            elif chemin == "/api/reglages":
                s.definir_reglages(d)
            elif chemin == "/api/diaporama/suppression":
                s.supprimer_diaporama()
            else:
                return self.erreur(404, "Action inconnue.")
            self.envoyer(200, {"ok": True})
        except KeyError as exc:
            self.erreur(404, str(exc.args[0]) if exc.args else "Introuvable.")
        except (ValueError, TypeError) as exc:
            self.erreur(400, str(exc) or "Requête invalide.")

    def televerser(self):
        nom = unquote(self.headers.get("X-Nom-Fichier", "fichier"))
        ext = Path(nom).suffix.lower()
        if ext not in FORMATS_PRESENTATION | FORMATS_IMAGE | {".pdf"}:
            return self.erreur(400, "Format non pris en charge : PowerPoint, PDF, PNG ou JPG.")
        taille = int(self.headers.get("Content-Length") or 0)
        if not 0 < taille <= MAX_UPLOAD:
            return self.erreur(400, "Fichier vide ou trop volumineux (200 Mo maximum).")
        with self.store.cond:
            if self.store.conversion["etat"] == "en_cours":
                return self.erreur(409, "Une conversion est déjà en cours.")
            self.store.conversion = {"etat": "en_cours", "message": "Réception du fichier…"}
            self.store._change()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        travail = Path(tempfile.mkdtemp(prefix="conversion-", dir=DATA_DIR))
        source = travail / f"source{ext}"
        try:
            with open(source, "wb") as f:
                reste = taille
                while reste:
                    bloc = self.rfile.read(min(reste, 1 << 20))
                    if not bloc:
                        raise ValueError("Envoi interrompu.")
                    f.write(bloc)
                    reste -= len(bloc)
        except (OSError, ValueError) as exc:
            shutil.rmtree(travail, ignore_errors=True)
            self.store.etat_conversion("erreur", f"Envoi interrompu : {exc}")
            return self.erreur(400, "Envoi interrompu.")
        threading.Thread(target=convertir_diaporama, args=(self.store, source, Path(nom).name),
                         daemon=True).start()
        self.envoyer(202, {"ok": True})


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for reste in DATA_DIR.glob("conversion-*"):  # conversions interrompues
        shutil.rmtree(reste, ignore_errors=True)
    store = Store(Voix(TTS_MODEL))
    Handler.store = store
    threading.Thread(target=store.boucle, daemon=True).start()
    try:
        serveur = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        sys.exit(f"Impossible d'écouter sur le port {PORT} ({exc.strerror}). "
                 f"Le port est peut-être déjà utilisé : essayer APPEL_PORT={PORT + 1} python3 server.py")
    serveur.daemon_threads = True
    print(f"Patiento en écoute sur http://{HOST}:{PORT}  (données : {DATA_DIR})", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()

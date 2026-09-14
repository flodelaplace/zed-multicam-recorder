#!/usr/bin/env python3
"""
patient_store.py — registre patients ANONYMISE et CHIFFRE.

- Chaque patient -> un code alphanumerique (ex: 007ABC). Les enregistrements
  vont dans data/<CODE>/ : aucune info nominative dans les dossiers.
- La table de correspondance code -> {nom, prenom, ...} est chiffree (Fernet,
  cle derivee du mot de passe via scrypt). Le mot de passe n'est JAMAIS stocke.
- Sans le mot de passe, le fichier registry est illisible.

Le mot de passe est fourni a l'ouverture (unlock) ou a l'initialisation (setup),
sert a deriver la cle en memoire, puis est oublie. La cle Fernet reste en memoire
le temps de la session (jusqu'a lock()).
"""
import base64
import hashlib
import json
import os
import random
import re
import string
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

CODE_RE = re.compile(r"^\d{3}[A-Z]{3}$")
SCRYPT_N = 2 ** 15  # cout CPU/memoire du KDF


class PatientStore:
    def __init__(self, data_dir="data"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.reg = self.dir / "patients_registry.enc"
        self._fernet = None      # cle de session (apres unlock/setup)
        self._salt = None
        self._data = None        # {code: {info}} en clair en memoire

    # -- etat --
    def initialized(self):
        return self.reg.exists()

    def locked(self):
        return self._fernet is None

    # -- crypto interne --
    def _derive(self, password, salt):
        raw = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=SCRYPT_N, r=8, p=1, dklen=32,
                             maxmem=128 * 1024 * 1024)
        return Fernet(base64.urlsafe_b64encode(raw))

    def _save(self):
        token = self._fernet.encrypt(json.dumps(self._data, ensure_ascii=False).encode("utf-8"))
        payload = {"kdf": "scrypt", "n": SCRYPT_N,
                   "salt": base64.b64encode(self._salt).decode(),
                   "token": token.decode()}
        tmp = self.reg.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(self.reg))  # ecriture atomique

    # -- setup / unlock / lock --
    def setup(self, password):
        """Cree un registre vide chiffre (premiere utilisation)."""
        if self.initialized():
            raise ValueError("registre deja initialise (utilise unlock)")
        if not password:
            raise ValueError("mot de passe vide")
        self._salt = os.urandom(16)
        self._fernet = self._derive(password, self._salt)
        self._data = {}
        self._save()

    def unlock(self, password):
        payload = json.loads(self.reg.read_text())
        salt = base64.b64decode(payload["salt"])
        fernet = self._derive(password, salt)
        try:
            clear = fernet.decrypt(payload["token"].encode())
        except InvalidToken:
            raise ValueError("mot de passe incorrect")
        self._fernet = fernet
        self._salt = salt
        self._data = json.loads(clear.decode("utf-8"))
        return self._data

    def lock(self):
        self._fernet = None
        self._salt = None
        self._data = None

    # -- CRUD (necessite unlock, sauf codes_on_disk) --
    def _gen_code(self):
        existing = set(self._data or {}) | set(self.codes_on_disk())
        while True:
            code = "%03d%s" % (random.randint(0, 999),
                               "".join(random.choice(string.ascii_uppercase) for _ in range(3)))
            if code not in existing:
                return code

    def add(self, info):
        """info = dict (nom, prenom, et champs libres). Retourne le code."""
        if self.locked():
            raise ValueError("registre verrouille")
        code = self._gen_code()
        rec = {k: v for k, v in info.items() if v not in (None, "")}
        rec["code"] = code
        rec["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._data[code] = rec
        self._save()
        (self.dir / code).mkdir(exist_ok=True)
        return code

    def update(self, code, info):
        if self.locked():
            raise ValueError("registre verrouille")
        if code not in self._data:
            raise ValueError("code inconnu")
        self._data[code].update({k: v for k, v in info.items() if v not in (None, "")})
        self._save()

    def all(self):
        if self.locked():
            raise ValueError("registre verrouille")
        return dict(self._data)

    def codes_on_disk(self):
        return sorted(p.name for p in self.dir.iterdir()
                      if p.is_dir() and CODE_RE.match(p.name))

    def folder(self, code):
        d = self.dir / code
        d.mkdir(exist_ok=True)
        return d


# -- autotest (mot de passe jetable, dossier temporaire) --
if __name__ == "__main__":
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        s = PatientStore(tmp)
        assert not s.initialized()
        s.setup("MDP_TEST_jetable")
        assert s.initialized() and not s.locked()
        c1 = s.add({"nom": "Dupont", "prenom": "Jean", "age": 72, "MMSE": 28})
        c2 = s.add({"nom": "Martin", "prenom": "Marie"})
        assert CODE_RE.match(c1) and CODE_RE.match(c2) and c1 != c2
        assert (Path(tmp) / c1).is_dir()
        # le fichier chiffre ne doit PAS contenir les noms en clair
        blob = (Path(tmp) / "patients_registry.enc").read_text()
        assert "Dupont" not in blob and "Martin" not in blob, "PII en clair !"
        # relock + mauvais mot de passe
        s.lock()
        try:
            s.unlock("mauvais"); assert False, "mauvais mdp accepte"
        except ValueError:
            pass
        # bon mot de passe -> on retrouve tout
        s.unlock("MDP_TEST_jetable")
        assert s.all()[c1]["nom"] == "Dupont" and s.all()[c1]["MMSE"] == 28
        assert set(s.codes_on_disk()) == {c1, c2}
        print("AUTOTEST OK :", c1, c2, "| PII chiffree, mauvais mdp rejete, codes/dossiers OK")
    finally:
        shutil.rmtree(tmp)

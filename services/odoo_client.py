"""Client RPC Odoo 19 en lecture : protocole JSON-2 (cle API) ou JSON-RPC (login).

Repris du connecteur d'odoo_migrator (engine/connector.py), reduit a la
lecture et porte sur urllib : NOMAIA est empaquete par PyInstaller et ne
depend pas de requests.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger("nomaia.odoo")

PROTOCOL_JSON2 = "json2"
PROTOCOL_JSONRPC = "jsonrpc"
PROTOCOLS = (PROTOCOL_JSON2, PROTOCOL_JSONRPC)

# Codes HTTP d'une passerelle momentanement indisponible : on retente.
RETRY_STATUS = (502, 503, 504)


class OdooRpcError(Exception):
    """Erreur renvoyee par le serveur Odoo, ou serveur injoignable."""


@dataclass
class OdooConfig:
    """Parametres de connexion.

    `secret` porte la cle API (json2) ou la cle API / mot de passe (jsonrpc).
    """

    url: str
    database: str
    secret: str = field(repr=False)
    protocol: str = PROTOCOL_JSON2
    login: str = ""
    timeout: int = 60
    max_retries: int = 3


class OdooClient:
    """Acces RPC en lecture a une base Odoo, meme interface quel que soit le protocole."""

    def __init__(self, config):
        if config.protocol not in PROTOCOLS:
            raise OdooRpcError(f"Protocole inconnu : {config.protocol}")
        self.config = config
        self.base_url = config.url.rstrip("/")
        self.uid = None
        self._rpc_id = 0

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _post(self, url, payload, headers=None):
        """POST JSON avec reprise sur erreur reseau (pas sur erreur metier).

        :return: (code HTTP, corps decode)
        """
        data = json.dumps(payload).encode("utf-8")
        entetes = {"Content-Type": "application/json", "User-Agent": "NOMAIA/1.0"}
        entetes.update(headers or {})
        derniere = None
        for essai in range(1, self.config.max_retries + 1):
            requete = urllib.request.Request(url, data=data, headers=entetes, method="POST")
            try:
                with urllib.request.urlopen(requete, timeout=self.config.timeout) as reponse:
                    if "/web/login" in reponse.geturl() and "/web/login" not in url:
                        # Route inconnue du serveur : Odoo redirige vers la page de
                        # connexion, qu'urllib suit et renvoie en 200 HTML.
                        raise OdooRpcError(
                            "Le serveur redirige vers sa page de connexion : JSON-2 exige"
                            " Odoo 19. Pour une version anterieure, choisir JSON-RPC"
                            " (login + cle API)."
                        )
                    return reponse.status, self._decode(reponse.status, reponse.read())
            except urllib.error.HTTPError as error:
                if error.code in RETRY_STATUS:
                    derniere = f"HTTP {error.code}"
                else:
                    return error.code, self._decode(error.code, error.read())
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                derniere = getattr(error, "reason", error)
            logger.warning("Odoo : erreur reseau (%s/%s) sur %s : %s",
                           essai, self.config.max_retries, url, derniere)
            if essai < self.config.max_retries:
                time.sleep(2 ** essai)
        raise OdooRpcError(f"Serveur Odoo injoignable : {derniere}")

    @staticmethod
    def _decode(status, raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise OdooRpcError(
                f"HTTP {status} : reponse non JSON, verifier l'adresse du serveur"
                " (protocole et domaine seuls, ex. https://base.odoo.com)"
            )

    def _call_json2(self, model, method, ids, kwargs):
        payload = dict(kwargs)
        if ids:
            payload["ids"] = list(ids)
        status, body = self._post(
            f"{self.base_url}/json/2/{model}/{method}",
            payload,
            headers={
                "Authorization": f"Bearer {self.config.secret}",
                "X-Odoo-Database": self.config.database,
            },
        )
        if status != 200:
            message = body.get("message") if isinstance(body, dict) else str(body)
            raise OdooRpcError(message or f"HTTP {status}")
        return body

    def _jsonrpc(self, service, method, *args):
        self._rpc_id += 1
        _status, body = self._post(
            f"{self.base_url}/jsonrpc",
            {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": service, "method": method, "args": list(args)},
                "id": self._rpc_id,
            },
        )
        if not isinstance(body, dict):
            raise OdooRpcError("Reponse JSON-RPC inattendue")
        if body.get("error"):
            error = body["error"]
            data = error.get("data") or {}
            raise OdooRpcError(data.get("message") or error.get("message"))
        return body.get("result")

    def _call_jsonrpc(self, model, method, ids, kwargs):
        if self.uid is None:
            self.authenticate()
        args = [list(ids)] if ids else []
        return self._jsonrpc(
            "object", "execute_kw",
            self.config.database, self.uid, self.config.secret, model, method, args, kwargs,
        )

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------
    def authenticate(self):
        """Verifie les identifiants et retourne l'uid de l'utilisateur connecte."""
        if self.config.protocol == PROTOCOL_JSONRPC:
            uid = self._jsonrpc(
                "common", "login", self.config.database, self.config.login, self.config.secret
            )
            if not uid:
                raise OdooRpcError("Authentification refusee (login ou cle invalide)")
            self.uid = uid
        else:
            # En JSON-2 l'utilisateur est celui de la cle API ; context_get renvoie son uid.
            self.uid = (self.call("res.users", "context_get") or {}).get("uid")
            if not self.uid:
                raise OdooRpcError("Cle API refusee ou utilisateur introuvable")
        return self.uid

    def user_info(self):
        """Retourne le nom et le login de l'utilisateur connecte."""
        if self.uid is None:
            self.authenticate()
        rows = self.read("res.users", [self.uid], ["name", "login"])
        return rows[0] if rows else {}

    def call(self, model, method, ids=None, **kwargs):
        """Appelle une methode publique du modele avec des arguments nommes."""
        if self.config.protocol == PROTOCOL_JSON2:
            return self._call_json2(model, method, ids, kwargs)
        return self._call_jsonrpc(model, method, ids, kwargs)

    def fields_get(self, model, attributes=("type", "selection")):
        """Retourne la definition des champs du modele (attributs demandes seulement)."""
        return self.call(model, "fields_get", attributes=list(attributes))

    def search(self, model, domain, context=None, order="id"):
        """Retourne tous les ids correspondant au domaine."""
        return self.call(model, "search", domain=domain, order=order, context=context or {})

    def read(self, model, ids, fields, context=None):
        """Lit les champs demandes pour les ids donnes."""
        if not ids:
            return []
        return self.call(model, "read", ids=ids, fields=fields, context=context or {})

    def search_read(self, model, domain, fields, context=None, order="id"):
        """search_read en un appel : un id supprime ou interdit est ignore, pas en erreur."""
        return self.call(
            model, "search_read", domain=domain, fields=fields, order=order, context=context or {}
        )

    def iter_read(self, model, domain, fields, context=None, batch_size=500):
        """Lit par lots (search des ids puis read par paquets) pour borner la taille des reponses."""
        ids = self.search(model, domain, context=context)
        for start in range(0, len(ids), batch_size):
            yield self.read(model, ids[start:start + batch_size], fields, context=context)

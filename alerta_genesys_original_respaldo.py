#!/usr/bin/env python3
"""
Alertas por Telegram para agentes de Genesys Cloud.

Revisa cada N segundos el estado principal (presencia) y el estado de
enrutamiento de los agentes configurados. Si una regla se cumple de forma
CONTINUA por más del límite, envía un aviso a Telegram (una sola vez por
episodio). Cuando el agente cambia de estado, la regla se reinicia.

Variables de entorno requeridas:
  GENESYS_CLIENT_ID, GENESYS_CLIENT_SECRET, TELEGRAM_BOT_TOKEN
Configuración (región, chat, agentes, reglas): config.yaml
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

try:
    from zoneinfo import ZoneInfo
    TZ_LOCAL = ZoneInfo("America/Caracas")
except Exception:  # Venezuela es UTC-4 todo el año
    TZ_LOCAL = timezone(timedelta(hours=-4))

log = logging.getLogger("alerta_genesys")
RUNNING = True


# ----------------------------------------------------------------- Genesys
class Genesys:
    def __init__(self, region, client_id, client_secret):
        self.api = f"https://api.{region}"
        self.login = f"https://login.{region}"
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_exp = 0.0

    def _auth(self):
        if self.token and time.time() < self.token_exp - 60:
            return
        r = requests.post(
            f"{self.login}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        self.token = d["access_token"]
        self.token_exp = time.time() + int(d.get("expires_in", 86400))
        log.info("Token de Genesys renovado")

    def get(self, path, params=None):
        for _ in range(3):
            self._auth()
            r = requests.get(
                self.api + path,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30,
            )
            if r.status_code == 401:
                self.token = None
                continue
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"No se pudo consultar {path}")


def load_presence_definitions(g):
    """id de presencia -> {system: 'AWAY', labels: {'baño', ...}}"""
    out, page = {}, 1
    while True:
        d = g.get("/api/v2/presencedefinitions", {"pageSize": 100, "pageNumber": page})
        for p in d.get("entities", []):
            labels = {str(p.get("name", "")).lower()}
            labels |= {str(v).lower() for v in (p.get("languageLabels") or {}).values()}
            out[p["id"]] = {"system": p.get("systemPresence"), "labels": labels}
        if page >= d.get("pageCount", 1):
            break
        page += 1
    return out


def resolve_users(g, emails):
    wanted = {e.lower() for e in emails}
    found, page = {}, 1
    while True:
        d = g.get("/api/v2/users", {"pageSize": 100, "pageNumber": page, "state": "active"})
        for u in d.get("entities", []):
            e = (u.get("email") or "").lower()
            if e in wanted:
                found[e] = {"id": u["id"], "name": u.get("name", e)}
        if page >= d.get("pageCount", 1):
            break
        page += 1
    for e in wanted - set(found):
        log.warning("No se encontró el usuario con correo %s", e)
    return found


def fetch_status(g, ids):
    res = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = [("id", x) for x in chunk]
        params += [("expand", "presence"), ("expand", "routingStatus"), ("pageSize", len(chunk))]
        d = g.get("/api/v2/users", params)
        for u in d.get("entities", []):
            res[u["id"]] = u
    return res


# ------------------------------------------------------------------- reglas
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate(rule, user, pdefs):
    """Devuelve (cumple, desde) para una regla y un usuario."""
    cond = rule["cuando"]
    pres = user.get("presence") or {}
    pdef = pres.get("presenceDefinition") or {}
    info = pdefs.get(pdef.get("id"), {})
    system = (pdef.get("systemPresence") or info.get("system") or "").upper()
    labels = info.get("labels", set())
    rs = user.get("routingStatus") or {}
    since_candidates = []

    if "presencia_sistema" in cond:
        if system not in [s.upper() for s in cond["presencia_sistema"]]:
            return False, None
        since_candidates.append(parse_dt(pres.get("modifiedDate")))
    if "etiqueta_presencia" in cond:
        if not labels & {s.lower() for s in cond["etiqueta_presencia"]}:
            return False, None
        since_candidates.append(parse_dt(pres.get("modifiedDate")))
    if "enrutamiento" in cond:
        if (rs.get("status") or "").upper() != cond["enrutamiento"].upper():
            return False, None
        since_candidates.append(parse_dt(rs.get("startTime")))

    since_candidates = [c for c in since_candidates if c]
    return True, (max(since_candidates) if since_candidates else None)


# ----------------------------------------------------------------- Telegram
def send_telegram(token, chat_id, text):
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)))
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Error enviando a Telegram (intento %d): %s", attempt + 1, e)
            time.sleep(3)
    return False


# --------------------------------------------------------------------- main
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    g = Genesys(
        cfg["genesys"]["region"],
        os.environ["GENESYS_CLIENT_ID"],
        os.environ["GENESYS_CLIENT_SECRET"],
    )
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = cfg["telegram"]["chat_id"]
    interval = int(cfg.get("intervalo_segundos", 30))
    state_path = cfg.get("archivo_estado", "estado_alertas.json")

    rules = cfg["reglas"]
    shifts = cfg["turnos"]
    agents_cfg = cfg["agentes"]

    users = resolve_users(g, [a["email"] for a in agents_cfg])
    agent_by_id = {}
    for a in agents_cfg:
        u = users.get(a["email"].lower())
        if u:
            agent_by_id[u["id"]] = {"name": u["name"], "turno": a["turno"]}
    if not agent_by_id:
        log.error("Ningún agente válido en la configuración")
        sys.exit(1)
    log.info("Monitoreando %d agentes", len(agent_by_id))

    pdefs = load_presence_definitions(g)
    alerted = load_state(state_path)  # "uid|regla" -> 'desde' ISO ya alertado
    first_seen = {}                   # respaldo si la API no trae marca de tiempo

    def stop(*_):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while RUNNING:
        try:
            status = fetch_status(g, list(agent_by_id))
            now = datetime.now(timezone.utc)
            for uid, ag in agent_by_id.items():
                user = status.get(uid)
                if not user:
                    continue
                for rname in shifts.get(ag["turno"], []):
                    rule = rules[rname]
                    key = f"{uid}|{rname}"
                    ok, since = evaluate(rule, user, pdefs)
                    if not ok:
                        alerted.pop(key, None)
                        first_seen.pop(key, None)
                        continue
                    if since is None:
                        since = first_seen.setdefault(key, now)
                    elapsed = now - since
                    limit = timedelta(minutes=rule["limite_min"])
                    if elapsed > limit and alerted.get(key) != since.isoformat():
                        mins = int(elapsed.total_seconds() // 60)
                        msg = (
                            f"⚠️ {ag['name']} lleva {mins} min {rule['descripcion']} "
                            f"(límite: {rule['limite_min']} min).\n"
                            f"Desde las {since.astimezone(TZ_LOCAL):%H:%M} (hora Venezuela)."
                        )
                        if send_telegram(tg_token, chat_id, msg):
                            alerted[key] = since.isoformat()
                            log.info("Alerta enviada: %s / %s", ag["name"], rname)
            save_state(state_path, alerted)
        except Exception as e:  # no matar el servicio por un error puntual
            log.exception("Error en el ciclo de monitoreo: %s", e)
            time.sleep(10)
        for _ in range(interval):
            if not RUNNING:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()

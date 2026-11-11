#!/usr/bin/env python3
"""Monitor de alertas Genesys Cloud -> Telegram.

Reglas independientes: OFF_QUEUE >70 min; presencia personalizada Baño >20 min.
La recuperación se notifica únicamente cuando Genesys confirma el regreso
a cola, y solo si hubo una alerta previa sin recuperación notificada.
La API proporciona el inicio de cada episodio. Si falta la fecha, se mide
conservadoramente desde la primera observación, persistida en JSON.
"""
import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

LOG = logging.getLogger('alerta_genesys')
TZ = ZoneInfo('America/Caracas')
RUNNING = True


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, AttributeError):
        return None


class Genesys:
    def __init__(self, region, client_id, client_secret):
        self.api = f'https://api.{region}'
        self.login = f'https://login.{region}'
        self.client_id, self.client_secret = client_id, client_secret
        self.token, self.token_exp = None, 0
        self.session = requests.Session()

    def _auth(self):
        if self.token and time.time() < self.token_exp - 60:
            return
        response = self.session.post(
            f'{self.login}/oauth/token',
            data={'grant_type': 'client_credentials'},
            auth=(self.client_id, self.client_secret), timeout=20)
        response.raise_for_status()
        data = response.json()
        self.token = data['access_token']
        self.token_exp = time.time() + int(data.get('expires_in', 3600))
        LOG.info('Autenticación OAuth Genesys correcta')

    def get(self, path, params=None):
        for attempt in range(4):
            self._auth()
            response = self.session.get(
                self.api + path, params=params,
                headers={'Authorization': f'Bearer {self.token}'}, timeout=30)
            if response.status_code == 401:
                self.token = None
                continue
            if response.status_code == 429:
                try:
                    wait = min(
                        max(float(response.headers.get('Retry-After', '5')), 1), 60)
                except ValueError:
                    wait = 5
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f'No se pudo consultar {path} tras varios intentos')


def load_presence_definitions(g):
    result, page = {}, 1
    while True:
        data = g.get('/api/v2/presencedefinitions',
                     {'pageSize': 100, 'pageNumber': page})
        for p in data.get('entities', []):
            labels = [str(p.get('name') or '').strip()]
            labels.extend(str(v).strip()
                          for v in (p.get('languageLabels') or {}).values())
            result[p['id']] = {
                'system': str(p.get('systemPresence') or '').upper(),
                'labels': [s for s in labels if s],
            }
        if page >= data.get('pageCount', 1):
            break
        page += 1
    return result


def resolve_users(g, emails):
    wanted = {e.strip().lower() for e in emails}
    found, page = {}, 1
    while True:
        data = g.get('/api/v2/users',
                     {'pageSize': 100, 'pageNumber': page, 'state': 'active'})
        for user in data.get('entities', []):
            email = str(user.get('email') or '').lower()
            if email in wanted:
                found[email] = {'id': user['id'],
                                'name': user.get('name') or email}
        if page >= data.get('pageCount', 1):
            break
        page += 1
    for email in sorted(wanted - set(found)):
        LOG.warning('Agente no encontrado: %s', email)
    return found


def fetch_status(g, ids):
    found = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = [('id', x) for x in chunk]
        params.extend([('expand', 'presence'), ('expand',
                      'routingStatus'), ('pageSize', len(chunk))])
        data = g.get('/api/v2/users', params)
        for user in data.get('entities', []):
            found[user['id']] = user
    return found


def presence_label(user, definitions):
    presence = user.get('presence') or {}
    definition = presence.get('presenceDefinition') or {}
    info = definitions.get(definition.get('id'), {})
    labels = info.get('labels', [])
    # La definición personalizada puede contener traducciones y una etiqueta vacía.
    # Preferimos español si está presente, sin asumir que AWAY equivale a Baño.
    preferred = next((x for x in labels if x.casefold()
                     in ('baño', 'bano')), None)
    name = preferred or next((x for x in labels if x), None)
    return name or definition.get('systemPresence') or info.get('system') or 'Desconocida'


def observe(user, definitions):
    """Devuelve condición y fecha para cada regla, o None si no es evaluable.

    None = respuesta incompleta; False = condición comprobablemente inactiva.
    """
    routing = user.get('routingStatus') or {}
    presence = user.get('presence') or {}
    pdef = presence.get('presenceDefinition') or {}
    info = definitions.get(pdef.get('id'), {})
    routing_status = str(routing.get('status') or '').upper()
    if not routing_status:
        off_queue = None
    else:
        off_queue = (routing_status == 'OFF_QUEUE',
                     parse_dt(routing.get('startTime')))

    labels = {str(x).casefold() for x in info.get('labels', [])}
    if not pdef.get('id') or pdef.get('id') not in definitions:
        bathroom = None  # No inferir desde AWAY: hay otros subestados.
    else:
        bathroom = (bool(labels & {'baño', 'bano', 'bathroom'}),
                    parse_dt(presence.get('modifiedDate')))
    return {'fuera_cola': off_queue, 'bano': bathroom}


def load_state(path):
    """Lee estado v3 o migra el v2 anterior sin perder alertas ya enviadas.

    pending_queue: uid -> nombres de reglas que generaron alerta y aún
    no tienen confirmación de retorno a cola.
    """
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('episodes'), dict):
            raise ValueError('Estructura inválida')
        if data.get('version') == 3:
            pending = data.get('pending_queue', {})
            if not isinstance(pending, dict):
                raise ValueError('Pendientes inválidos')
            return data['episodes'], pending
        if data.get('version') == 2:
            pending = {}
            for key, episode in data['episodes'].items():
                if not isinstance(episode, dict) or not episode.get('alerted'):
                    continue
                if '|' not in key:
                    continue
                uid, rule = key.rsplit('|', 1)
                if rule in ('fuera_cola', 'bano'):
                    pending.setdefault(uid, [])
                    if rule not in pending[uid]:
                        pending[uid].append(rule)
            LOG.info('Estado v2 leído; se migrará a v3 conservando alertas previas')
            return data['episodes'], pending
    except (OSError, ValueError):
        LOG.exception('No se pudo leer el estado guardado')
    raise RuntimeError(
        'Archivo de estado incompatible o dañado. Revisar antes de continuar: ' + str(path))


def save_state(path, episodes, pending_queue):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'version': 3, 'episodes': episodes,
                               'pending_queue': pending_queue}, indent=2,
                              ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def is_on_queue(user, definitions):
    """Exige evidencia de presencia On Queue y enrutamiento no OFF_QUEUE.

    No confundir IDLE (esperando interacción) con OFF_QUEUE. Si la API
    omite cualquiera de los campos necesarios, no afirmar retorno.
    """
    routing = user.get('routingStatus') or {}
    routing_status = str(routing.get('status') or '').upper()
    presence = user.get('presence') or {}
    definition = presence.get('presenceDefinition') or {}
    info = definitions.get(definition.get('id'), {})
    system_presence = str(definition.get('systemPresence')
                          or info.get('system') or '')
    normalized = system_presence.upper().replace(' ', '_')
    return bool(routing_status and routing_status != 'OFF_QUEUE'
                and normalized == 'ON_QUEUE')


def format_return_message(name, rules, now, current_presence, routing_status):
    labels = {'fuera_cola': 'Fuera de cola (70 min)',
              'bano': 'Baño (20 min)'}
    previous_alerts = ', '.join(labels[r] for r in rules if r in labels)
    time_local = now.astimezone(TZ).strftime('%d/%m/%Y %I:%M %p')
    return (f'✅ GENESYS — REGRESO A COLA\n'
            f'Agente: {name}\n'
            f'Presencia actual: {current_presence}\n'
            f'Enrutamiento: {routing_status}\n'
            f'Alertas previas: {previous_alerts}\n'
            f'Retorno detectado: {time_local} (Venezuela)')


def telegram_send(token, chat_id, message):
    for attempt in range(3):
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': chat_id, 'text': message}, timeout=20)
            if response.status_code == 429:
                try:
                    wait = min(int(response.json().get(
                        'parameters', {}).get('retry_after', 5)), 60)
                except (ValueError, KeyError):
                    wait = 5
                time.sleep(wait)
                continue
            response.raise_for_status()
            if not response.json().get('ok'):
                raise RuntimeError('Telegram no confirmó el envío')
            return True
        except (requests.RequestException, RuntimeError) as exc:
            LOG.error('Error enviando Telegram (intento %s): %s',
                      attempt + 1, exc)
            time.sleep(2)
    return False


def format_message(name, rule, since, now, current_presence):
    minutes = int((now - since).total_seconds() // 60)
    started = since.astimezone(TZ).strftime('%d/%m/%Y %I:%M %p')
    if rule == 'fuera_cola':
        return (f'🚨 ALERTA GENESYS — FUERA DE COLA\n'
                f'Agente: {name}\nEnrutamiento: OFF_QUEUE\n'
                f'Presencia actual: {current_presence}\n'
                f'Tiempo fuera de cola: {minutes} min\nLímite: 70 min\n'
                f'Inicio: {started} (Venezuela)')
    return (f'🚻 ALERTA GENESYS — BAÑO\nAgente: {name}\n'
            f'Presencia: {current_presence}\n'
            f'Tiempo en Baño: {minutes} min\nLímite: 20 min\n'
            f'Inicio: {started} (Venezuela)')


def process_observation(episodes, key, condition, now, limit_minutes):
    """Devuelve (nuevo_estado, debe_alertar, desde).

    Si falta fecha API, se utiliza primera observación persistida. No se
    recupera tiempo previo desconocido. No se resetea por cambiar presencia
    cuando OFF_QUEUE continúa activo.
    """
    if condition is None:
        return episodes.get(key), False, None
    active, api_since = condition
    if not active:
        return None, False, None
    previous = episodes.get(key)
    if api_since and api_since > now + timedelta(minutes=1):
        LOG.warning('Fecha futura ignorada para %s', key)
        api_since = None
    if api_since:
        since = api_since
        # Nueva marca temporal = nuevo episodio, incluso si no vimos la transición.
        episode_id = api_since.isoformat()
    elif previous and previous.get('source') == 'observed':
        since = parse_dt(previous.get('since')) or now
        episode_id = previous.get('episode_id') or since.isoformat()
    else:
        since = now
        episode_id = since.isoformat()
    same = bool(previous and previous.get('episode_id') == episode_id)
    record = {'episode_id': episode_id, 'since': since.isoformat(),
              'source': 'api' if api_since else 'observed',
              'alerted': bool(previous.get('alerted')) if same else False}
    should_alert = (now - since > timedelta(minutes=limit_minutes)
                    and not record['alerted'])
    return record, should_alert, since


def main():
    global RUNNING
    parser = argparse.ArgumentParser(description='Alertas Genesys a Telegram')
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simula mensajes; no envía Telegram ni guarda estado')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    config_path = Path(args.config).resolve()
    load_dotenv(config_path.parent / '.env')
    cfg = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    required = ['GENESYS_CLIENT_ID',
                'GENESYS_CLIENT_SECRET', 'TELEGRAM_BOT_TOKEN']
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit('Faltan variables en .env: ' + ', '.join(missing))
    interval = int(cfg.get('intervalo_segundos', 30))
    if interval < 5:
        raise SystemExit('intervalo_segundos debe ser al menos 5')
    state_path = config_path.parent / \
        cfg.get('archivo_estado', 'estado_alertas_v2.json')
    g = Genesys(cfg['genesys']['region'], os.environ['GENESYS_CLIENT_ID'],
                os.environ['GENESYS_CLIENT_SECRET'])
    token, chat_id = os.environ['TELEGRAM_BOT_TOKEN'], cfg['telegram']['chat_id']
    agents = cfg['agentes']
    users = resolve_users(g, [x['email'] for x in agents])
    by_id = {u['id']: u['name'] for u in users.values()}
    if not by_id:
        raise SystemExit('No se encontraron agentes activos configurados')
    LOG.info('Monitoreando %s agentes', len(by_id))
    definitions = load_presence_definitions(g)
    if not any({'baño', 'bano', 'bathroom'} &
               {x.casefold() for x in info.get('labels', [])} for info in definitions.values()):
        LOG.warning(
            'No se encontró una presencia personalizada Baño en las definiciones')
    episodes, pending_queue = load_state(
        state_path) if not args.dry_run else ({}, {})

    def stop(*_):
        global RUNNING
        RUNNING = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while RUNNING:
        try:
            statuses = fetch_status(g, list(by_id))
            now = datetime.now(timezone.utc)
            for uid, name in by_id.items():
                user = statuses.get(uid)
                if not user:
                    LOG.warning(
                        'Sin datos del agente %s; no se reinician contadores', name)
                    continue
                conditions = observe(user, definitions)
                current_presence = presence_label(user, definitions)
                for rule, limit in [('fuera_cola', 70), ('bano', 20)]:
                    key = f'{uid}|{rule}'
                    record, should_alert, since = process_observation(
                        episodes, key, conditions[rule], now, limit)
                    if record is None:
                        episodes.pop(key, None)
                        continue
                    episodes[key] = record
                    if should_alert:
                        message = format_message(
                            name, rule, since, now, current_presence)
                        if args.dry_run:
                            LOG.info('SIMULACIÓN: %s',
                                     message.replace('\n', ' | '))
                            record['alerted'] = True
                        elif telegram_send(token, chat_id, message):
                            record['alerted'] = True
                            # Guardar de inmediato reduce riesgo de duplicado tras reinicio.
                            if rule not in pending_queue.get(uid, []):
                                pending_queue.setdefault(uid, []).append(rule)
                            save_state(state_path, episodes, pending_queue)
                            LOG.info('Alerta enviada: %s / %s', name, rule)
                # Solo se notifica retorno si hubo alerta de una condición
                # previa. Baño debe haber terminado para declararlo resuelto.
                pending = pending_queue.get(uid, [])
                bathroom = conditions.get('bano')
                bathroom_finished = bathroom is not None and not bathroom[0]
                ready = [r for r in pending
                         if r == 'fuera_cola' or (r == 'bano' and bathroom_finished)]
                if ready and is_on_queue(user, definitions):
                    routing_status = (user.get('routingStatus')
                                      or {}).get('status', '')
                    msg = format_return_message(name, ready, now, current_presence,
                                                routing_status)
                    if args.dry_run:
                        LOG.info('SIMULACIÓN: %s', msg.replace('\n', ' | '))
                        pending_queue[uid] = [
                            r for r in pending if r not in ready]
                    elif telegram_send(token, chat_id, msg):
                        pending_queue[uid] = [
                            r for r in pending if r not in ready]
                        if not pending_queue[uid]:
                            pending_queue.pop(uid, None)
                        save_state(state_path, episodes, pending_queue)
                        LOG.info(
                            'Retorno a cola notificado: %s / %s', name, ready)
            if not args.dry_run:
                save_state(state_path, episodes, pending_queue)
        except Exception:
            LOG.exception('Error en ciclo de monitoreo; se reintentará')
        for _ in range(interval):
            if not RUNNING:
                break
            time.sleep(1)
    LOG.info('Monitor detenido')


if __name__ == '__main__':
    main()

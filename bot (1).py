#!/usr/bin/env python3
"""
Tennis Favorite Alert — Bot de Telegram v2
- The Odds API     → favoritos ATP/WTA (cuota prepartido ≤ 1.55)
- Live Tennis API  → marcadores en vivo ATP, WTA, Challenger, ITF
"""

import os
import time
import logging
from datetime import datetime
import requests

# ── Configuración ─────────────────────────────────────────────────────────────

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID", "")
ODDS_KEY    = os.getenv("ODDS_API_KEY", "")
LTA_KEY     = os.getenv("LTA_API_KEY", "")
THRESHOLD   = float(os.getenv("FAV_THRESHOLD", "1.55"))
POLL_SECS   = int(os.getenv("POLL_INTERVAL_MINS", "5")) * 60
ODDS_SPORTS = [s.strip() for s in os.getenv("SPORTS", "tennis_atp,tennis_wta").split(",")]

ODDS_BASE   = "https://api.the-odds-api.com/v4"
LTA_BASE    = "https://api.livetennisapi.com/api/public/v1"
TG_BASE     = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Estado global ─────────────────────────────────────────────────────────────

alerted     = set()   # IDs ya procesados
fav_map     = {}      # odds_event_id → {name, odds, home, away, sport}
name_to_fav = {}      # apellido.lower() → (ev_id, fav_dict)
tg_offset   = 0
alert_count = 0

# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(method, **kw):
    try:
        return requests.post(f"{TG_BASE}/{method}", json=kw, timeout=10).json()
    except Exception as e:
        log.error(f"Telegram/{method}: {e}")
        return {}

def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML")

def check_commands():
    global tg_offset
    data = tg("getUpdates", offset=tg_offset, timeout=2)
    for upd in data.get("result", []):
        tg_offset = upd["update_id"] + 1
        text = upd.get("message", {}).get("text", "").strip()

        if text.startswith("/start") or text.startswith("/help"):
            send(
                "🎾 <b>Tennis Favorite Alert Bot v2</b>\n\n"
                f"Avisa cuando un favorito (cuota ≤ {THRESHOLD}) pierde el primer set.\n"
                "Cubre ATP, WTA, Challenger e ITF.\n\n"
                "<b>Comandos:</b>\n"
                "/status — Estado y estadísticas\n"
                "/favs — Favoritos en seguimiento\n"
                "/help — Esta ayuda"
            )

        elif text.startswith("/status"):
            tracked = sum(1 for k in fav_map if k not in alerted)
            send(
                f"🎾 <b>Estado del bot</b>\n\n"
                f"Circuitos (odds): {', '.join(ODDS_SPORTS)}\n"
                f"Marcadores: ATP · WTA · Challenger · ITF\n"
                f"Intervalo: cada {POLL_SECS // 60} min\n"
                f"Umbral: cuota ≤ {THRESHOLD}\n"
                f"Favoritos en seguimiento: {tracked}\n"
                f"Alertas enviadas: {alert_count}"
            )

        elif text.startswith("/favs"):
            tracked = [(k, v) for k, v in fav_map.items() if k not in alerted]
            if not tracked:
                send("No hay favoritos claros (≤ 1.55) detectados ahora mismo.")
            else:
                lines = [f"🎾 <b>Favoritos en seguimiento ({len(tracked)})</b>\n"]
                for _, f in tracked[:20]:
                    opp = f["away"] if f["home"] == f["name"] else f["home"]
                    lines.append(f"• <b>{f['name']}</b> @{f['odds']} vs {opp}  [{f['sport']}]")
                send("\n".join(lines))

# ── Helpers de odds ───────────────────────────────────────────────────────────

def best_odds(bookmakers, player):
    best = None
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            for o in mkt["outcomes"]:
                if o["name"] == player and (best is None or o["price"] < best):
                    best = o["price"]
    return best

def find_favorite(bookmakers, home, away):
    fav = None
    for player in (home, away):
        p = best_odds(bookmakers, player)
        if p and p <= THRESHOLD and (not fav or p < fav["odds"]):
            fav = {"name": player, "odds": round(p, 2)}
    return fav

def last_name(full_name):
    """Apellido en minúsculas para cruzar jugadores entre APIs."""
    return full_name.strip().split()[-1].lower() if full_name else ""

# ── Live Tennis API ───────────────────────────────────────────────────────────

def lta_get(path, params=None):
    try:
        r = requests.get(
            f"{LTA_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {LTA_KEY}"},
            timeout=15,
        )
        if r.ok:
            return r.json()
        log.warning(f"LTA {path}: {r.status_code}")
    except Exception as e:
        log.warning(f"LTA {path} error: {e}")
    return None

def parse_lta_score(score_data, fav_is_p1):
    """
    score_data: {"sets": [1,0], "games": [[6,4],[3,1]], ...}
    Devuelve (fav_won, fav_games, opp_games) si el primer set está terminado, o None.
    """
    if not score_data:
        return None
    games = score_data.get("games", [])
    if not games:
        return None

    g1, g2 = games[0][0], games[0][1]
    mx = max(g1, g2)
    complete = (mx >= 6 and abs(g1 - g2) >= 2) or mx == 7
    if not complete:
        return None

    fav_g = g1 if fav_is_p1 else g2
    opp_g = g2 if fav_is_p1 else g1
    return fav_g > opp_g, fav_g, opp_g

# ── Ciclo de comprobación ─────────────────────────────────────────────────────

def poll():
    global fav_map, name_to_fav, alert_count

    new_favs     = dict(fav_map)
    new_name_map = {}

    # 1. The Odds API → actualizar favoritos ATP/WTA
    for sport in ODDS_SPORTS:
        try:
            r = requests.get(
                f"{ODDS_BASE}/sports/{sport}/odds/",
                params={
                    "apiKey": ODDS_KEY, "regions": "eu",
                    "markets": "h2h", "oddsFormat": "decimal",
                },
                timeout=15,
            )
            if r.status_code == 401:
                send("⚠️ ODDS_API_KEY inválida.")
                return
            if r.status_code == 429:
                send("⚠️ Límite de The Odds API alcanzado.")
                return
            rem = r.headers.get("x-requests-remaining", "?")
            log.info(f"[Odds/{sport}] {r.status_code} · {rem} req restantes")
            if r.ok:
                for ev in r.json():
                    if ev["id"] in alerted:
                        continue
                    fav = find_favorite(ev.get("bookmakers", []), ev["home_team"], ev["away_team"])
                    if fav:
                        new_favs[ev["id"]] = {
                            "name":    fav["name"],
                            "odds":    fav["odds"],
                            "home":    ev["home_team"],
                            "away":    ev["away_team"],
                            "sport":   sport,
                        }
        except Exception as e:
            log.warning(f"Odds/{sport}: {e}")

    # Mapa apellido → favorito para cruzar con Live Tennis API
    for ev_id, fav in new_favs.items():
        if ev_id not in alerted:
            new_name_map[last_name(fav["name"])] = (ev_id, fav)

    fav_map     = new_favs
    name_to_fav = new_name_map

    # 2. Live Tennis API → partidos en vivo (todos los circuitos)
    live_matches = lta_get("/matches", params={"status": "live"})
    if live_matches is None:
        log.warning("No se pudo obtener partidos en vivo de Live Tennis API")
        return

    # Manejar respuesta como lista o dict con clave "matches"/"data"
    if isinstance(live_matches, dict):
        live_matches = live_matches.get("matches") or live_matches.get("data") or []

    log.info(f"LTA: {len(live_matches)} partidos en vivo")

    for match in live_matches:
        p1      = match.get("p1") or {}
        p2      = match.get("p2") or {}
        p1_name = p1.get("name", "")
        p2_name = p2.get("name", "")
        mid     = match.get("id")
        tour    = match.get("tour", "").lower()

        if not mid or not p1_name or not p2_name:
            continue

        lta_key = f"lta_{mid}"
        if lta_key in alerted:
            continue

        # Cruzar por apellido con favoritos de The Odds API
        fav_data   = None
        fav_is_p1  = None
        opp_name   = None
        ev_id      = None

        ln1, ln2 = last_name(p1_name), last_name(p2_name)

        if ln1 in name_to_fav:
            ev_id, fav_data = name_to_fav[ln1]
            fav_is_p1, opp_name = True, p2_name
        elif ln2 in name_to_fav:
            ev_id, fav_data = name_to_fav[ln2]
            fav_is_p1, opp_name = False, p1_name

        if not fav_data or (ev_id and ev_id in alerted):
            continue

        # Obtener marcador detallado
        score  = lta_get(f"/matches/{mid}/score")
        result = parse_lta_score(score, fav_is_p1)
        if result is None:
            continue   # Primer set aún en juego

        fav_won, fav_g, opp_g = result

        if not fav_won:
            circuit = tour.upper() if tour else fav_data.get("sport", "")
            msg = (
                f"🎾 <b>Favorito pierde el 1.er set</b>\n\n"
                f"<b>{fav_data['name']}</b>  @{fav_data['odds']}\n"
                f"Cae <b>{opp_g}-{fav_g}</b> ante {opp_name}\n"
                f"[{circuit}] · {datetime.now().strftime('%H:%M')}"
            )
            send(msg)
            alert_count += 1
            log.info(f"ALERTA: {fav_data['name']} pierde 1er set {opp_g}-{fav_g} vs {opp_name}")

        # Marcar como procesado
        alerted.add(lta_key)
        if ev_id:
            alerted.add(ev_id)

# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    if not all([BOT_TOKEN, CHAT_ID, ODDS_KEY, LTA_KEY]):
        print(
            "ERROR: Faltan variables de entorno.\n"
            "Configura BOT_TOKEN, CHAT_ID, ODDS_API_KEY y LTA_API_KEY."
        )
        return

    log.info("Bot v2 iniciado")
    send(
        f"🎾 <b>Tennis Alert Bot v2 iniciado</b>\n\n"
        f"Marcadores: ATP · WTA · Challenger · ITF\n"
        f"Umbral: cuota ≤ {THRESHOLD}\n"
        f"Intervalo: cada {POLL_SECS // 60} min\n\n"
        f"Usa /help para ver los comandos."
    )

    while True:
        try:
            check_commands()
            log.info("── Ciclo de comprobación ──")
            poll()
            log.info(f"Completado. Próxima comprobación en {POLL_SECS // 60} min.")
        except KeyboardInterrupt:
            send("🔴 Bot detenido manualmente.")
            log.info("Detenido.")
            break
        except Exception as e:
            log.error(f"Error inesperado: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()

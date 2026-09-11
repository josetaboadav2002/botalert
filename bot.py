#!/usr/bin/env python3
"""
Tennis Favorite Alert — Bot de Telegram
Avisa cuando un favorito (cuota prepartido <= 1.55) pierde el primer set.

Dependencias: pip install requests
Configuración: variables de entorno BOT_TOKEN, CHAT_ID, ODDS_API_KEY
"""

import os
import re
import time
import logging
from datetime import datetime
import requests

# ── Configuración ─────────────────────────────────────────────────────────────

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")        # Token de @BotFather
CHAT_ID    = os.getenv("CHAT_ID", "")          # Tu ID de Telegram
ODDS_KEY   = os.getenv("ODDS_API_KEY", "")     # API key de the-odds-api.com
THRESHOLD  = float(os.getenv("FAV_THRESHOLD", "1.55"))
POLL_SECS  = int(os.getenv("POLL_INTERVAL_MINS", "5")) * 60
SPORTS     = [s.strip() for s in os.getenv("SPORTS", "tennis_atp,tennis_wta").split(",")]

ODDS_BASE  = "https://api.the-odds-api.com/v4"
TG_BASE    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Estado global ─────────────────────────────────────────────────────────────

alerted   = set()   # event IDs ya procesados (ganó o perdió 1er set)
fav_map   = {}      # event_id → {name, odds, home, away, sport}
tg_offset = 0       # puntero para getUpdates
alert_count = 0     # contador de alertas enviadas

# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f"{TG_BASE}/{method}", json=kwargs, timeout=10)
        return r.json()
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
                "🎾 <b>Tennis Favorite Alert Bot</b>\n\n"
                f"Avisa cuando un favorito (cuota ≤ {THRESHOLD}) pierde el primer set.\n\n"
                "<b>Comandos:</b>\n"
                "/status — Estado y estadísticas\n"
                "/favs — Favoritos en seguimiento ahora\n"
                "/help — Esta ayuda"
            )

        elif text.startswith("/status"):
            tracked = sum(1 for k in fav_map if k not in alerted)
            send(
                f"🎾 <b>Estado del bot</b>\n\n"
                f"Circuitos: {', '.join(SPORTS)}\n"
                f"Intervalo: cada {POLL_SECS // 60} min\n"
                f"Umbral de favorito: cuota ≤ {THRESHOLD}\n"
                f"Favoritos en seguimiento: {tracked}\n"
                f"Alertas enviadas esta sesión: {alert_count}"
            )

        elif text.startswith("/favs"):
            tracked = [(k, v) for k, v in fav_map.items() if k not in alerted]
            if not tracked:
                send("No hay favoritos claros (≤ 1.55) detectados en este momento.")
            else:
                lines = [f"🎾 <b>Favoritos en seguimiento ({len(tracked)})</b>\n"]
                for _, f in tracked[:20]:
                    opp = f["away"] if f["home"] == f["name"] else f["home"]
                    lines.append(f"• <b>{f['name']}</b> @{f['odds']} vs {opp}  [{f['sport']}]")
                send("\n".join(lines))

# ── Lógica de datos ───────────────────────────────────────────────────────────

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

def parse_sets(scores):
    if not scores or len(scores) < 2:
        return None
    p1, p2 = scores[0], scores[1]
    if not p1.get("score") or not p2.get("score"):
        return None

    def extract(s):
        cleaned = re.sub(r"\(.*?\)", "", s)
        vals = []
        for x in cleaned.split(","):
            x = x.strip()
            if x.isdigit():
                vals.append(int(x))
        return vals

    ag, bg = extract(p1["score"]), extract(p2["score"])
    if not ag or not bg:
        return None

    sets = []
    for g1, g2 in zip(ag, bg):
        mx = max(g1, g2)
        complete = (mx >= 6 and abs(g1 - g2) >= 2) or mx == 7
        sets.append({
            "g1": g1, "g2": g2, "complete": complete,
            "winner": (0 if g1 > g2 else 1) if complete else None,
        })
    return {"sets": sets, "p1": p1["name"], "p2": p2["name"]}

# ── Ciclo de comprobación ─────────────────────────────────────────────────────

def poll():
    global fav_map, alert_count
    new_favs = dict(fav_map)

    for sport in SPORTS:
        # 1. Cuotas prepartido → detectar favoritos
        try:
            r = requests.get(
                f"{ODDS_BASE}/sports/{sport}/odds/",
                params={
                    "apiKey": ODDS_KEY, "regions": "eu",
                    "markets": "h2h", "oddsFormat": "decimal",
                },
                timeout=15,
            )
            rem = r.headers.get("x-requests-remaining", "?")
            log.info(f"[{sport}] odds {r.status_code} · {rem} req restantes")
            if r.status_code == 401:
                log.error("ODDS_API_KEY inválida.")
                send("⚠️ Error: ODDS_API_KEY inválida. Verifica tu clave en the-odds-api.com")
                return
            if r.status_code == 429:
                log.warning("Límite de solicitudes alcanzado.")
                send("⚠️ Límite de solicitudes de The Odds API alcanzado.")
                return
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
                            "commence": ev.get("commence_time", ""),
                        }
        except Exception as e:
            log.warning(f"[{sport}] Error odds: {e}")

        # 2. Marcadores en vivo → detectar pérdida de primer set
        try:
            r = requests.get(
                f"{ODDS_BASE}/sports/{sport}/scores/",
                params={"apiKey": ODDS_KEY, "daysFrom": 1},
                timeout=15,
            )
            rem = r.headers.get("x-requests-remaining", "?")
            log.info(f"[{sport}] scores {r.status_code} · {rem} req restantes")
            if not r.ok:
                continue

            for match in r.json():
                mid = match["id"]
                if mid in alerted:
                    continue
                fav = new_favs.get(mid)
                if not fav or not match.get("scores"):
                    continue

                parsed = parse_sets(match["scores"])
                if not parsed or not parsed["sets"]:
                    continue

                first = parsed["sets"][0]
                if not first["complete"]:
                    continue   # Primer set aún en juego

                fav_is_p1 = parsed["p1"] == fav["name"]
                fav_won   = (first["winner"] == 0) if fav_is_p1 else (first["winner"] == 1)

                if not fav_won:
                    opp   = parsed["p2"] if fav_is_p1 else parsed["p1"]
                    fav_g = first["g1"] if fav_is_p1 else first["g2"]
                    opp_g = first["g2"] if fav_is_p1 else first["g1"]

                    msg = (
                        f"🎾 <b>Favorito pierde el 1.er set</b>\n\n"
                        f"<b>{fav['name']}</b>  @{fav['odds']}\n"
                        f"Cae <b>{opp_g}-{fav_g}</b> ante {opp}\n"
                        f"[{fav['sport']}] · {datetime.now().strftime('%H:%M')}"
                    )
                    send(msg)
                    alerted.add(mid)
                    alert_count += 1
                    log.info(f"ALERTA: {fav['name']} pierde 1er set {opp_g}-{fav_g} vs {opp}")
                else:
                    # Favorito ganó el primer set, no es necesario seguir comprobando
                    alerted.add(mid)

        except Exception as e:
            log.warning(f"[{sport}] Error scores: {e}")

    fav_map = new_favs

# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN or not CHAT_ID or not ODDS_KEY:
        print(
            "ERROR: Faltan variables de entorno.\n"
            "Configura BOT_TOKEN, CHAT_ID y ODDS_API_KEY antes de ejecutar."
        )
        return

    log.info("Bot iniciado")
    send(
        f"🎾 <b>Tennis Alert Bot iniciado</b>\n\n"
        f"Circuitos: {', '.join(SPORTS)}\n"
        f"Umbral: cuota ≤ {THRESHOLD}\n"
        f"Intervalo: cada {POLL_SECS // 60} min\n\n"
        f"Usa /help para ver los comandos disponibles."
    )

    while True:
        try:
            check_commands()
            log.info("── Ciclo de comprobación ──")
            poll()
            log.info(f"Completado. Próxima comprobación en {POLL_SECS // 60} min.")
        except KeyboardInterrupt:
            send("🔴 Bot detenido manualmente.")
            log.info("Detenido por el usuario.")
            break
        except Exception as e:
            log.error(f"Error inesperado: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Match notifier -> ntfy

Acompanha: Man City, Arsenal, Real Madrid (todos os jogos)
           Brasil, Noruega (apenas amistosos)

Lembretes por partida (veja REMINDERS):
  1 semana, 2 dias e 1 dia antes (no mesmo horario do jogo)
  3h, 2h e 1h antes (no dia do jogo)

Rode a cada 10 minutos (Agendador de Tarefas do Windows ou cron).

Fonte de dados: TheSportsDB (chave gratuita "123").

Comandos uteis:
  python match_notifier.py --test                 # notificacao de teste
  python match_notifier.py --list                 # proximos jogos
  python match_notifier.py --schedule             # horario de cada lembrete por jogo
  python match_notifier.py --dry-run --now "2026-10-14 15:00"
                                                  # simula uma data/hora (hora de Sao Paulo)
                                                  # sem enviar nada e sem gravar o state
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------- CONFIG ---------------------------------------

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "match_notifier_analima")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")  # so se o seu topico for protegido

TSDB_KEY = os.getenv("TSDB_KEY", "123")
TSDB = f"https://www.thesportsdb.com/api/v1/json/{TSDB_KEY}"

TZ = ZoneInfo("America/Sao_Paulo")
FIXTURE_CACHE_HOURS = 6

# (minutos antes do jogo, texto, prioridade ntfy)
REMINDERS = [
    (7 * 24 * 60, "em 1 semana", "default"),
    (2 * 24 * 60, "em 2 dias", "default"),
    (1 * 24 * 60, "em 1 dia", "default"),
    (3 * 60, "em 3 horas", "high"),
    (2 * 60, "em 2 horas", "high"),
    (1 * 60, "em 1 hora", "high"),
    (1 * 31, "em 1 hora", "high"),
]

# Tolerancia: o lembrete ainda sai se o script rodar ate X minutos DEPOIS do
# horario ideal. Precisa ser maior que o intervalo do agendador (10 min).
GRACE_MINUTES = 20

# Resumo da manha com os jogos do dia (desligado, pois os lembretes ja cobrem).
DIGEST_ENABLED = False
DIGEST_HOUR = 8

TEAMS = [
    {"name": "Manchester City", "friendlies_only": False},
    {"name": "Arsenal", "friendlies_only": False},
    {"name": "Real Madrid", "friendlies_only": False},
    {"name": "Brazil", "friendlies_only": True},
    {"name": "Norway", "friendlies_only": True},
]

STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))

# ----------------------------- STATE ----------------------------------------


def load_state():
    state = {"team_ids": {}, "fixtures": [], "fixtures_at": None,
             "sent": [], "digest_days": []}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ----------------------------- DATA -----------------------------------------


def get(path, **params):
    r = requests.get(f"{TSDB}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def resolve_team_id(name):
    data = get("searchteams.php", t=name)
    for t in data.get("teams") or []:
        if t.get("strTeam", "").lower() == name.lower() and t.get("strSport") == "Soccer":
            return t["idTeam"]
    return None


def parse_kickoff(ev):
    ts = ev.get("strTimestamp")
    try:
        if ts:
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        if ev.get("dateEvent") and ev.get("strTime"):
            return datetime.fromisoformat(
                f"{ev['dateEvent']}T{ev['strTime']}").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None  # sem horario confiavel ainda


def fetch_fixtures(state):
    by_id = {}
    for team in TEAMS:
        name = team["name"]
        tid = state["team_ids"].get(name) or resolve_team_id(name)
        if not tid:
            print(f"[aviso] nao achei o id do time {name}", file=sys.stderr)
            continue
        state["team_ids"][name] = tid

        events = get("eventsnext.php", id=tid).get("events") or []
        for ev in events:
            league = ev.get("strLeague") or ""
            if team["friendlies_only"] and "friendl" not in league.lower():
                continue
            kickoff = parse_kickoff(ev)
            if not kickoff:
                continue
            entry = by_id.setdefault(ev["idEvent"], {
                "id": ev["idEvent"],
                "home": ev.get("strHomeTeam"),
                "away": ev.get("strAwayTeam"),
                "league": league,
                "kickoff": kickoff.isoformat(),
                "followed": [],
            })
            entry["followed"].append(name)

    fixtures = sorted(by_id.values(), key=lambda e: e["kickoff"])
    state["fixtures"] = fixtures
    state["fixtures_at"] = datetime.now(timezone.utc).isoformat()
    return fixtures


def get_fixtures(state, force=False):
    at = state.get("fixtures_at")
    fresh = at and datetime.now(timezone.utc) - datetime.fromisoformat(at) < timedelta(
        hours=FIXTURE_CACHE_HOURS)
    if fresh and not force:
        return state["fixtures"]
    try:
        return fetch_fixtures(state)
    except requests.RequestException as e:
        print(f"[aviso] falha ao buscar jogos, usando cache: {e}", file=sys.stderr)
        return state["fixtures"]


# ----------------------------- NTFY -----------------------------------------


def notify(title, message, priority="default", tags="soccer"):
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    r = requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                      data=message.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


def fmt_match(f):
    local = datetime.fromisoformat(f["kickoff"]).astimezone(TZ)
    return f"{local:%H:%M} - {f['home']} vs {f['away']} ({f['league']})"


# ----------------------------- LOGIC ----------------------------------------


def due_reminders(fixture, now, sent):
    """Gera os lembretes que devem sair agora para uma partida."""
    kickoff = datetime.fromisoformat(fixture["kickoff"])
    minutes_left = (kickoff - now).total_seconds() / 60
    if minutes_left <= 0:
        return
    for offset, label, priority in REMINDERS:
        key = f"{fixture['id']}:{offset}"
        if key in sent:
            continue
        if offset - GRACE_MINUTES < minutes_left <= offset:
            yield key, label, priority


def run(state, now, dry_run=False):
    now_local = now.astimezone(TZ)
    fixtures = get_fixtures(state)
    send = (lambda *a, **k: print("[dry-run]", a, k)) if dry_run else notify

    # Resumo da manha (opcional)
    if DIGEST_ENABLED:
        today = now_local.date().isoformat()
        if now_local.hour >= DIGEST_HOUR and today not in state["digest_days"]:
            todays = [f for f in fixtures
                      if datetime.fromisoformat(f["kickoff"]).astimezone(TZ).date().isoformat() == today]
            if todays:
                send("Jogos de hoje", "\n".join(fmt_match(f) for f in todays),
                     tags="calendar,soccer")
            state["digest_days"] = (state["digest_days"] + [today])[-14:]

    # Lembretes por partida
    for f in fixtures:
        kickoff_local = datetime.fromisoformat(f["kickoff"]).astimezone(TZ)
        for key, label, priority in due_reminders(f, now, state["sent"]):
            send(f"{f['home']} vs {f['away']}",
                 f"Jogo {label} ({kickoff_local:%a %d/%m as %H:%M})\n{f['league']}",
                 priority=priority, tags="soccer,alarm_clock")
            state["sent"].append(key)

    state["sent"] = state["sent"][-300:]


def print_schedule(fixtures):
    for f in fixtures:
        kickoff = datetime.fromisoformat(f["kickoff"])
        print(f"\n{f['home']} vs {f['away']} ({f['league']})")
        print(f"  jogo: {kickoff.astimezone(TZ):%a %d/%m %H:%M}")
        for offset, label, _ in REMINDERS:
            t = (kickoff - timedelta(minutes=offset)).astimezone(TZ)
            print(f"  lembrete {label:<12} -> {t:%a %d/%m %H:%M}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="envia uma notificacao de teste")
    ap.add_argument("--list", action="store_true", help="atualiza e lista os proximos jogos")
    ap.add_argument("--schedule", action="store_true",
                    help="mostra o horario de cada lembrete por jogo")
    ap.add_argument("--dry-run", action="store_true",
                    help="imprime em vez de enviar (nao grava o state)")
    ap.add_argument("--now", help='simula a data/hora atual, ex: "2026-10-14 15:00" (hora de SP)')
    args = ap.parse_args()

    if args.test:
        notify("Teste", "ntfy configurado. Bora!", tags="tada")
        return

    state = load_state()

    if args.list or args.schedule:
        fixtures = get_fixtures(state, force=True)
        save_state(state)
        if args.schedule:
            print_schedule(fixtures)
        else:
            for f in fixtures:
                d = datetime.fromisoformat(f["kickoff"]).astimezone(TZ)
                print(f"{d:%a %d/%m} {fmt_match(f)}  [{', '.join(f['followed'])}]")
        return

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)

    run(state, now, dry_run=args.dry_run)
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()

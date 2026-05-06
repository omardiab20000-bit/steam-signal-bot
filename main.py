import asyncio
import aiohttp
import os
import re
import time
import json
import yaml
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path("config.yaml")
STATE_PATH = Path("state.json")

STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_alerts": {}, "snapshots": {}, "startup_sent": False}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ts_now():
    return int(time.time())


def clean_text(text):
    text = text or ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def lower_clean(text):
    return clean_text(text).lower()


def clamp(x, low=0, high=100):
    return max(low, min(high, x))


def money_to_float(price_text):
    if not price_text or price_text in ("N/A", "Free"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", price_text.replace(",", ""))
    return float(m.group(1)) if m else None


async def fetch_json(session, url, params=None):
    async with session.get(url, params=params, timeout=30) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def fetch_text(session, url, params=None):
    async with session.get(url, params=params, timeout=30) as response:
        response.raise_for_status()
        return await response.text()


async def steam_app_details(session, appid, cfg):
    cc = cfg["bot"].get("country_code", "us")
    lang = cfg["bot"].get("language", "english")
    data = await fetch_json(
        session,
        STEAM_APPDETAILS_URL,
        params={"appids": appid, "cc": cc, "l": lang}
    )
    node = data.get(str(appid), {})
    if not node.get("success"):
        return None

    d = node.get("data", {})
    price = d.get("price_overview") or {}
    final_price = price.get("final_formatted")
    if d.get("is_free"):
        final_price = "Free"

    return {
        "appid": int(appid),
        "name": d.get("name", f"Steam App {appid}"),
        "steam_url": f"https://store.steampowered.com/app/{appid}",
        "header_image": d.get("header_image"),
        "release_date": (d.get("release_date") or {}).get("date", "Unknown"),
        "is_free": bool(d.get("is_free")),
        "steam_price": final_price or "N/A",
        "short_description": clean_text(d.get("short_description", "")),
        "genres": [g.get("description") for g in d.get("genres", []) if g.get("description")],
        "categories": [c.get("description") for c in d.get("categories", []) if c.get("description")]
    }


async def steam_current_players(session, appid):
    try:
        data = await fetch_json(session, STEAM_PLAYERS_URL, params={"appid": appid})
        return int(data.get("response", {}).get("player_count", 0))
    except Exception:
        return 0


async def steam_recent_reviews(session, appid, cfg):
    max_reviews = int(cfg["bot"].get("max_reviews_per_game", 100))
    lang = cfg["bot"].get("language", "english")
    data = await fetch_json(
        session,
        STEAM_REVIEWS_URL.format(appid=appid),
        params={
            "json": 1,
            "filter": "recent",
            "language": lang,
            "review_type": "all",
            "purchase_type": "all",
            "num_per_page": max_reviews
        }
    )

    reviews = []
    for r in data.get("reviews", []):
        text = lower_clean(r.get("review", ""))
        if not text:
            continue
        author = r.get("author", {}) or {}
        reviews.append({
            "text": text,
            "voted_up": bool(r.get("voted_up")),
            "timestamp_created": r.get("timestamp_created"),
            "playtime_hours": round((author.get("playtime_forever", 0) or 0) / 60, 1),
            "votes_up": r.get("votes_up", 0),
            "weighted_vote_score": float(r.get("weighted_vote_score", 0) or 0)
        })

    return reviews, data.get("query_summary", {})


async def discover_appids(session, cfg):
    discovered = set()
    limit = int(cfg["bot"].get("max_discovery_games", 35))

    for filter_name in cfg.get("discovery_filters", ["popularnew"]):
        try:
            data = await fetch_json(
                session,
                STEAM_SEARCH_URL,
                params={
                    "query": "",
                    "start": 0,
                    "count": limit,
                    "dynamic_data": "",
                    "sort_by": "_ASC",
                    "filter": filter_name,
                    "os": "win",
                    "infinite": 1,
                    "cc": cfg["bot"].get("country_code", "us").upper(),
                    "l": cfg["bot"].get("language", "english")
                }
            )
            html = data.get("results_html", "")
            soup = BeautifulSoup(html, "html.parser")
            for row in soup.select("a.search_result_row"):
                appid = row.get("data-ds-appid") or row.get("data-appid")
                if appid and appid.isdigit():
                    discovered.add(int(appid))
        except Exception as e:
            print(f"Discovery failed for {filter_name}: {e}")

    return list(discovered)


async def instant_gaming_lookup(session, game_name):
    if os.getenv("INSTANT_GAMING_ENABLED", "false").lower() != "true":
        return None

    try:
        html = await fetch_text(
            session,
            "https://www.instant-gaming.com/en/search/",
            params={"q": game_name}
        )
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        match = re.search(r"[$€£]\s?\d+(?:\.\d{2})?", text)
        return match.group(0).replace(" ", "") if match else None
    except Exception:
        return None


def count_keyword_hits(blob, keywords):
    hits = []
    for kw in keywords:
        kw_l = kw.lower()
        count = blob.count(kw_l)
        if count:
            hits.append({"keyword": kw, "count": count})
    return sorted(hits, key=lambda x: x["count"], reverse=True)


def detect_hype_type(blob, cfg):
    hype_cfg = cfg["keywords"].get("hype", {})
    scores = {}
    for hype_type, terms in hype_cfg.items():
        scores[hype_type] = sum(blob.count(t.lower()) for t in terms)

    best = max(scores, key=scores.get) if scores else "unknown"
    if not scores or scores[best] == 0:
        return "early_signal", "Early Signal", "👀", scores

    labels = {
        "organic": ("Organic Buzz", "🌱"),
        "streamer": ("Streamer Fuel", "🎥"),
        "nostalgia": ("Nostalgia Pull", "🕹️"),
        "social": ("Social/Co-op Pull", "🤝"),
        "risk": ("Risk Noise", "⚠️"),
    }
    label, icon = labels.get(best, ("Early Signal", "👀"))
    return best, label, icon, scores


def detect_community_overlap(blob, cfg):
    overlaps = []
    for key, item in cfg.get("community_overlap", {}).items():
        terms = item.get("terms", [])
        count = sum(blob.count(t.lower()) for t in terms)
        if count:
            overlaps.append({
                "key": key,
                "icon": item.get("icon", "🎯"),
                "label": item.get("label", key),
                "count": count
            })
    return sorted(overlaps, key=lambda x: x["count"], reverse=True)[:3]


def detect_patterns(blob):
    pattern_rules = [
        ("🌌", "Atmosphere Check", "Atmosphere/vibe is repeatedly showing up", ["atmosphere", "vibe", "immersive", "world", "setting"]),
        ("🎬", "Visual Check", "Cinematic/visual appeal is carrying attention", ["cinematic", "visuals", "beautiful", "graphics", "art style"]),
        ("🤝", "Social Check", "Players keep tying the fun to friends/co-op", ["friends", "co-op", "coop", "multiplayer", "party"]),
        ("🎮", "Loop Check", "Players describe the gameplay loop as sticky", ["addictive", "can't stop", "one more", "replayable", "grind"]),
        ("🧩", "Mystery Check", "Mystery/worldbuilding is fueling curiosity", ["mystery", "story", "lore", "worldbuilding", "weird"]),
        ("💰", "Value Check", "Players are discussing price/value", ["worth it", "worth the money", "wait for sale", "overpriced"]),
    ]

    out = []
    for icon, title, summary, terms in pattern_rules:
        count = sum(blob.count(t) for t in terms)
        if count:
            out.append((count, icon, title, summary))

    out.sort(reverse=True)
    return out[:4]


def risk_patterns(blob):
    rules = [
        ("Performance/optimization complaints appearing in reviews", ["performance", "optimization", "fps", "stutter", "crash", "crashes"]),
        ("Some players mention repetition or shallow long-term loop", ["repetitive", "boring", "shallow", "same thing"]),
        ("Some reviews suggest the game may need more polish", ["unfinished", "needs work", "early access", "buggy", "broken"]),
        ("Price/value concern is showing up", ["wait for sale", "overpriced", "not worth", "refund"]),
        ("Server or online stability concerns are appearing", ["server", "disconnect", "lag", "matchmaking"])
    ]

    found = []
    for summary, terms in rules:
        count = sum(blob.count(t) for t in terms)
        if count:
            found.append((count, summary))

    found.sort(reverse=True)
    return [x[1] for x in found[:2]] or ["Long-term retention still needs more time/data"]


def arabic(cfg, key):
    if not cfg.get("arabic", {}).get("enabled", True):
        return ""
    return cfg.get("arabic", {}).get("lines", {}).get(key, "")


def analyze_game(app, reviews, players, previous, cfg):
    blob = " ".join(r["text"] for r in reviews)
    review_count = len(reviews)
    positives = sum(1 for r in reviews if r["voted_up"])
    positive_ratio = positives / review_count if review_count else 0

    pos_hits = count_keyword_hits(blob, cfg["keywords"]["positive"])
    risk_hits = count_keyword_hits(blob, cfg["keywords"]["risk"])
    patterns = detect_patterns(blob)
    risks = risk_patterns(blob)
    hype_key, hype_label, hype_icon, hype_scores = detect_hype_type(blob, cfg)
    overlaps = detect_community_overlap(blob, cfg)

    prev_players = (previous or {}).get("players", 0)
    growth_pct = 0
    if prev_players and players:
        growth_pct = round(((players - prev_players) / max(prev_players, 1)) * 100, 1)

    weights = cfg["scoring"]
    review_component = positive_ratio * weights["review_quality_weight"]
    sentiment_component = min(len(pos_hits) * 3.2, weights["sentiment_pattern_weight"])

    player_component = 0
    if players >= 50000:
        player_component += 16
    elif players >= 10000:
        player_component += 12
    elif players >= 2500:
        player_component += 9
    elif players >= 500:
        player_component += 5

    if growth_pct >= 75:
        player_component += 12
    elif growth_pct >= 35:
        player_component += 9
    elif growth_pct >= 12:
        player_component += 6
    elif growth_pct >= 3:
        player_component += 3

    player_component = min(player_component, weights["player_momentum_weight"])

    price_value_component = 0
    steam_price_float = money_to_float(app.get("steam_price"))
    if app.get("is_free"):
        price_value_component = weights["price_value_weight"]
    elif steam_price_float is not None:
        if steam_price_float <= 19.99:
            price_value_component = weights["price_value_weight"]
        elif steam_price_float <= 39.99:
            price_value_component = weights["price_value_weight"] * 0.6
        else:
            price_value_component = weights["price_value_weight"] * 0.25

    risk_penalty = min(len(risk_hits) * 4.5, weights["risk_penalty_weight"])
    confidence_bonus = 0
    min_reviews = int(cfg["bot"].get("min_reviews_for_confidence", 25))
    if review_count >= 100:
        confidence_bonus = weights["confidence_bonus_weight"]
    elif review_count >= min_reviews:
        confidence_bonus = weights["confidence_bonus_weight"] * 0.55

    raw_score = review_component + sentiment_component + player_component + price_value_component + confidence_bonus - risk_penalty
    score = round(clamp(raw_score), 1)

    if positive_ratio >= 0.88 and len(risk_hits) <= 3:
        mood = "Curious • Impressed • Positive"
        mood_ar = arabic(cfg, "mood_positive")
    elif positive_ratio >= 0.72:
        mood = "Interested • Impressed • Cautious"
        mood_ar = arabic(cfg, "mood_positive")
    elif positive_ratio >= 0.55:
        mood = "Divided • Curious • Careful"
        mood_ar = arabic(cfg, "mood_mixed")
    else:
        mood = "Concerned • Mixed • Risky"
        mood_ar = arabic(cfg, "mood_mixed")

    confidence = "Strong" if review_count >= 100 else "Medium" if review_count >= min_reviews else "Weak"

    checks = []
    for _, icon, title, summary in patterns:
        checks.append({"icon": icon, "title": title, "summary": summary})

    if growth_pct > 3:
        checks.append({"icon": "📈", "title": "Momentum Check", "summary": f"Current players are up {growth_pct}% vs last scan"})

    if overlaps:
        checks.append({
            "icon": "🎯",
            "title": "Community Overlap Check",
            "summary": "Crossover interest detected from related gaming communities"
        })

    if not checks:
        checks.append({"icon": "👀", "title": "Attention Check", "summary": "Early positive signals are forming, but more data is needed"})

    return {
        "score": score,
        "confidence": confidence,
        "review_count": review_count,
        "positive_ratio": round(positive_ratio * 100, 1),
        "growth_pct": growth_pct,
        "mood": mood,
        "mood_ar": mood_ar,
        "hype_key": hype_key,
        "hype_label": hype_label,
        "hype_icon": hype_icon,
        "hype_scores": hype_scores,
        "overlaps": overlaps,
        "checks": checks[:5],
        "risks": risks,
        "positive_hits": pos_hits[:6],
        "risk_hits": risk_hits[:6],
    }


def status_from_score(score):
    if score >= 85:
        return "🔥 High Watch"
    if score >= 68:
        return "👀 Worth watching"
    if score >= 50:
        return "⏳ Watchlist only"
    return "💤 Ignore for now"


def color_from_score(score):
    if score >= 85:
        return 0x22C55E
    if score >= 68:
        return 0xF59E0B
    if score >= 50:
        return 0x3B82F6
    return 0x6B7280


def english_arabic_for_check(cfg, title):
    if "Atmosphere" in title:
        return arabic(cfg, "atmosphere")
    if "Visual" in title:
        return arabic(cfg, "visual")
    if "Social" in title:
        return arabic(cfg, "social")
    if "Loop" in title:
        return arabic(cfg, "loop")
    if "Momentum" in title:
        return arabic(cfg, "momentum")
    if "Community" in title:
        return arabic(cfg, "overlap")
    return "في إشارات إيجابية، بس نحتاج بيانات أكثر"


def build_discord_embed(app, analysis, players, instant_price, cfg):
    title = f"👀 ALL EYES ON THIS"

    desc = (
        f"🎮 **{app['name']}**\n\n"
        f"🟡 Score — **{analysis['score']}/100**\n"
        f"🟢 Reviews — **{analysis['positive_ratio']}% Positive**\n"
        f"🟢 Players — **{players:,} Active**\n"
    )

    fields = []

    strong_checks = []

    for c in analysis["checks"][:2]:
        text = c['summary']
        text = text.replace("Atmosphere/vibe is repeatedly showing up", "Strong atmosphere and visual identity")
        text = text.replace("Players keep tying the fun to friends/co-op", "Social/co-op interest rising")
        text = text.replace("Players describe the gameplay loop as sticky", "Gameplay loop keeping attention")
        text = text.replace("Mystery/worldbuilding is fueling curiosity", "Curiosity around gameplay is rising")

        strong_checks.append(f"✅ {text}")

    fields.append({
        "name": "━━━━━━━━━━━━━━━",
        "value": "\n".join(strong_checks),
        "inline": False
    })

    if analysis["overlaps"]:
        overlap_lines = []

        for o in analysis["overlaps"][:2]:
            overlap_lines.append(f"🎮 {o['label']}")

        fields.append({
            "name": "🎯 Audience Pull",
            "value": "\n".join(overlap_lines),
            "inline": False
        })

    fields.append({
        "name": "⚠️ Watch Point",
        "value": analysis["risks"][0],
        "inline": False
    })

    price_text = f"💰 Steam — {app.get('steam_price', 'N/A')}"

    if instant_price:
        price_text += f"\n💰 Instant Gaming — {instant_price}"

    fields.append({
        "name": "💵 Price Check",
        "value": price_text,
        "inline": False
    })

    fields.append({
        "name": "👀 Current Read",
        "value": "Momentum is building naturally.\nواضح الاهتمام عم يكبر بسرعة",
        "inline": False
    })

    embed = {
        "title": title[:256],
        "url": app["steam_url"],
        "description": desc[:4096],
        "color": color_from_score(analysis["score"]),
        "fields": fields[:6],
        "footer": {
            "text": f"Steam Signal Bot • AppID {app['appid']}"
        }
    }

    if app.get("header_image"):
        embed["thumbnail"] = {"url": app["header_image"]}

    return embed


def can_send_alert(appid, analysis, cfg, state):
    threshold = float(cfg["bot"].get("alert_threshold", 60))
    if analysis["score"] < threshold:
        return False

    cooldown = int(cfg["bot"].get("cooldown_hours", 18)) * 3600
    last = int(state["last_alerts"].get(str(appid), 0))
    return ts_now() - last >= cooldown


async def send_discord(webhook_url, embed, cfg):
    username = cfg.get("discord", {}).get("username") or "Steam Signal Bot"
    avatar_url = cfg.get("discord", {}).get("avatar_url") or None

    payload = {
        "username": username,
        "embeds": [embed]
    }
    if avatar_url:
        payload["avatar_url"] = avatar_url

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json=payload, timeout=30) as response:
            if response.status not in (200, 204):
                text = await response.text()
                raise RuntimeError(f"Discord webhook failed {response.status}: {text}")


async def analyze_one(session, appid, cfg, state):
    app = await steam_app_details(session, appid, cfg)
    if not app:
        return None

    reviews, _ = await steam_recent_reviews(session, appid, cfg)
    players = await steam_current_players(session, appid)
    previous = state["snapshots"].get(str(appid))

    analysis = analyze_game(app, reviews, players, previous, cfg)

    state["snapshots"][str(appid)] = {
        "players": players,
        "score": analysis["score"],
        "checked_at": ts_now()
    }

    return app, analysis, players


async def scan_once():
    cfg = load_config()
    state = load_state()

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    print("Webhook loaded:", webhook_url is not None)

    if not webhook_url:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL Railway variable.")

    # Send startup message only once per container start.
    if not state.get("startup_sent"):
        await send_discord(
            webhook_url,
            {
                "title": "🟢 Steam Signal Bot Online",
                "description": "System initialized successfully. Balanced scan mode is active.",
                "color": 5763719,
                "fields": []
            },
            cfg
        )
        state["startup_sent"] = True
        save_state(state)

    headers = {
        "User-Agent": "SteamSignalBot/3.0 (+Railway Discord bot)"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        appids = set(int(x) for x in cfg.get("watchlist_appids", []))

        discovered = await discover_appids(session, cfg)
        appids.update(discovered)

        print(f"Scanning {len(appids)} Steam apps...")

        for appid in list(appids):
            try:
                result = await analyze_one(session, appid, cfg, state)
                if not result:
                    continue

                app, analysis, players = result
                print(
                    f"Checked {app['name']} | "
                    f"score={analysis['score']} | "
                    f"players={players:,} | "
                    f"reviews={analysis['review_count']}"
                )

                if can_send_alert(appid, analysis, cfg, state):
                    instant_price = await instant_gaming_lookup(session, app["name"])
                    embed = build_discord_embed(app, analysis, players, instant_price, cfg)
                    await send_discord(webhook_url, embed, cfg)

                    state["last_alerts"][str(appid)] = ts_now()
                    save_state(state)

                    print(f"ALERT SENT: {app['name']} score={analysis['score']}")

                await asyncio.sleep(1.5)

            except Exception as e:
                print(f"Error appid={appid}: {e}")

    save_state(state)


async def main():
    cfg = load_config()
    minutes = int(cfg["bot"].get("scan_minutes", 60))

    while True:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n[{started}] Scan starting...")
        try:
            await scan_once()
        except Exception as e:
            print(f"Scan error: {e}")

        print(f"Scan complete. Sleeping {minutes} minutes.")
        await asyncio.sleep(minutes * 60)


if __name__ == "__main__":
    asyncio.run(main())

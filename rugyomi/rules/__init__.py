"""The rules module (RGY-015): the single source of signals, score, bands, reasons, outcome labels and the methodology.

  config.py    public defaults (default_config.json) + private override ($RUGYOMI_RULES_CONFIG); version + hash
  context.py   the "as of the launch" view of the database (no lookahead, enforced there)
  signals.py   the signal registry v0
  card.py      compute_card (no writes), issue_for_height (collector), card_row (readers)
  outcomes.py  outcome labels from snapshots (facts, not the score's target)
  rug.py       the rug label (RGY-016): rug_fast / rug_late / rug / severity_near — the score's target
  accuracy.py  the accuracy table (RGY-018): pure stats over cards + stored outcomes
  stored.py    the `launch_outcomes` table (RGY-017): the collector writes the labels, everything else reads them
"""
from __future__ import annotations

from typing import Any

from .accuracy import card_outcome_rows, summarize as accuracy_summary
from .card import NOTE, RULES_VERSION, band_of, card_row, compute_card, issue_card, issue_for_height
from .config import ConfigError, config_hash, default_config, links_config_hash, load_config
from .outcomes import LABELS, outcome_labels
from .rug import LABELS as RUG_LABELS, rug_labels
from .signals import BY_ID, SIGNALS
from .stored import outcome_row, rebuild_outcomes, refresh_touched, refresh_touched_committed


def _env_rules_version() -> str:
    try:
        return str(load_config().get("rules_version") or "")
    except ConfigError:
        return ""


def _signal_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    sigs = []
    for s in SIGNALS:
        c = cfg["signals"].get(s.id)
        if c is None or not c.get("enabled", True):
            continue                                    # a signal of another rules profile (RGY-021), or one switched off
        sigs.append({"id": s.id, "version": s.version, "description": s.describe(c), "config": c})
    return sigs


def methodology_v1() -> dict[str, Any]:
    """The rules-v1 section of /methodology (RGY-021): the v1 signals with the public default thresholds and the rules that keep
    a backtest honest. Shown whether or not v1 is switched on; the live cards say which rules issued them."""
    cfg = default_config("v1")
    return {
        "rules_version": cfg.get("rules_version"), "config_version": cfg["version"], "signals": _signal_list(cfg),
        "intro": "Rules v1 add what the database knows about the people behind a launch: how their earlier launches ended, whether "
                 "the wallet that funded them funded a rugger, and whether the wallets in their funding cluster rugged before. "
                 "They are still a sum of signal contributions, and every card still lists its reasons with their evidence.",
        "no_lookahead": "No lookahead. A card is computed only from what was recorded before the launch's own transaction. A past "
                        "launch counts as a rug in the history of its creator only if its outcome was already known (its 24 hours "
                        "had passed and the label was final) at that moment; a link or a funding counts only if its transaction "
                        "is older than the launch. Deleting everything recorded after a launch must give the identical card; this is "
                        "tested, and a deliberately leaking variant of the code is tested to fail.",
        "new_creators": "A creator without history is not a clean creator. A new creator still gets the score of the signals that "
                        "do not need a history (fingerprint, name copy, creator tax and the like).",
        "split": "How the weights were set. The launches before the split date were used to choose and fit the weights and to fit the "
                 "score → probability scale. The launches from the split date on (history and live) were kept apart, looked at once "
                 "and are the only numbers shown as a backtest on the accuracy page. Nothing was tuned on them.",
        "coverage": "Data coverage. The history comes from a backfill of the chain, not from live collection: launches are complete "
                    "for the launchpads we cover, but the participants' transfers and swaps are approximate (about 1 swap in 450 is "
                    "lost), and the older the launch the less of its surroundings we have. A backtest on this history is therefore "
                    "a backtest on approximate data.",
        "not_track_record": "A backtest is not a track record. The rules were written and chosen after the launches happened. Only "
                            "the live table on the accuracy page, made of cards frozen at launch time, is a track record.",
    }


def methodology_expert(live_rules_version: str | None = None) -> dict[str, Any]:
    """The expert-rules section of /methodology (RGY-045): the two signals of profile v1r (v2r when that is the live one) with the published lists. They are set by hand, not fitted.
    `live_rules_version`: the rules version of the newest stored card (the web service does not run with the collector's profile variable); None = ask the environment."""
    from . import notable as NT
    rv = live_rules_version if live_rules_version is not None else _env_rules_version()
    cfg = default_config("v2l" if rv == "1.3" else "v2r" if rv == "1.2" else "v1r")
    sigs = [x for x in _signal_list(cfg) if x["id"] in ("name_imitation", "fresh_obfuscated_funding")]
    lst, src = NT.notable(), NT.funding_sources()
    live = rv in tuple(str(default_config(p).get("rules_version")) for p in ("v1r", "v2r", "v2l"))      # v1r, v2r or v2l is the profile that scores new launches
    where = ("They are in the rules profile that scores new launches since the switch; cards issued earlier keep the rules they were issued with."
             if live else "They are in a separate rules profile that is not live, so no published card uses them yet.")
    return {"config_version": cfg["version"], "rules_version": cfg.get("rules_version"), "signals": sigs, "live": live,
            "intro": "Expert rules, not fitted. Two further signals were written by hand from how launches are attacked, not learned from data: so few launches "
                     "show them that their weights cannot be fitted honestly, and their points are a judgement. " + where + " The profile also reads the creator's fee of justhoot launches "
                     "from the token-creation call (the same points as for nearly), for launches whose call was stored. The first numbers (how often each fires "
                     "and how those launches ended) are published with the profile.",
            "notable_list": {"version": lst["version"], "accounts": [{"account": e["account"], "source": e["source"]} for e in lst["accounts"]]},
            "funding_sources": {"version": src["version"], "intents": [{"account": e["account"], "source": e["source"]} for e in src["intents"]],
                                "exchanges": [{"account": e["account"], "source": e["source"]} for e in src["exchanges"]]}}


def profile_of(rules_version: str | None) -> str:
    """The rules profile that issues cards with this `rules_version` (v2 shares "1.0" with v1 but is never live)."""
    rv = str(rules_version or "")
    return "v2l" if rv == "1.3" else "v2r" if rv == "1.2" else "v1r" if rv == "1.1" else "v1" if rv.startswith("1") else "v0"


def note_for(rules_version: str | None) -> str:
    """The one-line note of a card by its rules version (the profile's `display.note`): v1 cards are not described with the v0 text."""
    return default_config(profile_of(rules_version))["display"]["note"]


def rug_chance_enabled() -> bool:
    """`display.rug_chance` of the effective config (default false: no \"rug chance ≈ X %\" on the site, in the channel or anywhere)."""
    try:
        return bool(load_config().get("display", {}).get("rug_chance"))
    except ConfigError:
        return False


def _live_state_text(cfg: dict[str, Any]) -> dict[str, Any]:
    from .. import livestate         # imported here: the live state module reads the rules config
    return livestate.describe(cfg)


def _holdings_text(cfg: dict[str, Any]) -> dict[str, Any]:
    from .. import holdings          # imported here: the holdings module reads the rules config
    return holdings.describe(cfg)


def methodology(live_rules_version: str | None = None) -> dict[str, Any]:
    """What /methodology shows: every signal with its description and the DEFAULT thresholds, the bands and the outcome
    labels. Never the production override. The bands and the expert section follow the profile that issues the newest card
    (`live_rules_version`, read by the web layer from the stored cards; None = the environment's profile)."""
    cfg = default_config()
    sigs = _signal_list(cfg)
    rv = live_rules_version if live_rules_version is not None else _env_rules_version()
    live_cfg = default_config(profile_of(rv)) if rv else cfg
    b, o, g = live_cfg["bands"], cfg["outcomes"], cfg["rug"]
    lk, cl = cfg["links"], cfg["clusters"]
    sold, loss, share = g["insiders_sold_bps"] / 100, g["buyers_loss_near"], g["price_max_share"] * 100
    return {
        "rules_version": RULES_VERSION, "config_version": cfg["version"], "note": NOTE, "signals": sigs, "v1": methodology_v1(), "expert": methodology_expert(live_rules_version),
        "bands_note": (f"The bands of the rules profile that scores new launches (configuration {live_cfg['version']}). Cards issued earlier keep the bands they were issued with."
                       if profile_of(rv) != "v0" else ""),
        "bands": [{"name": "Low", "range": f"0–{b['elevated'] - 1}"},
                  {"name": "Elevated", "range": f"{b['elevated']}–{b['high'] - 1}"},
                  {"name": "High", "range": f"{b['high']}–100"}],
        "outcomes": [
            {"id": "dev_exit", "description": f"The dev had sold at least {o['dev_exit']['sold_bps'] / 100:.0f}% of the "
                                              f"tokens he acquired by T+{o['dev_exit']['minute']} min."},
            {"id": "buyers_loss", "description": f"The other buyers' net result at T+{o['buyers_loss']['minute']} min is below "
                                                  f"{o['buyers_loss']['pnl_below_near']} NEAR (a loss)."}],
        "rug": [
            {"id": "rug_fast", "description": f"At T+60 min the insiders had sold at least {sold:.0f}% of the tokens they "
                                              f"acquired, and the outside buyers' net result is {loss} NEAR or worse."},
            {"id": "rug_late", "description": f"Not rug_fast; at T+24 h the insiders had sold at least {sold:.0f}% of the "
                                              f"tokens they acquired, the price is at most {share:.0f}% of its peak and the "
                                              f"outside buyers' net result is {loss} NEAR or worse."},
            {"id": "rug", "description": "rug_fast or rug_late. The risk score is checked against this label: it ranks launches by how "
                                          "likely a rug within 24 hours is. It is not a probability."},
            {"id": "undefined", "description": "Where the insiders acquired nothing through the pool (on justhoot v1 the dev does not buy "
                                                "through it; on nearly the creator-fee tokens are not a buy) their sold share does not exist. "
                                                "If they did sell tokens into the pool and the outside buyers lost money, the label is "
                                                "undefined: we do not say rug and we do not say no rug, and the launch is left out of every "
                                                "rate. If they sold nothing there was nothing to dump: no rug. (Label version 3.)"}],
        "links": {
            "intro": "Who is behind a launch. We show recorded facts between accounts, each with its transaction; we never "
                     "say that two accounts are one person. Links are not part of the risk score yet.",
            "types": [
                {"id": "funded", "description": f"One account sent NEAR (or wNEAR) to another: at least {lk['min_near']:g} NEAR."},
                {"id": "created", "description": "One account created the other (the account that paid for its creation)."},
                {"id": "launchbot_owner", "description": "Two sources record different real accounts behind the same launch "
                                                         "(through the launch bot the real owner is the account that paid it)."},
                {"id": "token_transfer", "description": "The dev sent the launch's tokens to the other account."}],
            "linked": f"Linked accounts: accounts that launched and are joined by direct links (any chain length; a group over "
                      f"{lk['group_max']} accounts is marked truncated). Hops through wallets that did not launch are not followed. "
                      "Linked accounts are not a cluster.",
            "cluster": f"Funding cluster: one wallet that is not a service account and did not launch itself, that funded at least "
                       f"{cl['min_creators']} different accounts less than {cl['window_days']:g} days before each of them launched, "
                       f"and that funded at most {cl['narrow_max']} accounts within {cl['narrow_window_days']:g} days of those fundings "
                       "(an exchange or a payout wallet funds far more). A funding is a direct transfer signed by the sender. Other kinds of cluster are not detected yet.",
            "service": "Service accounts (launchpads, the launch bot, the swap and wrap contracts, registrars, relayers, bridges) "
                       "are excluded from links by an explicit list, never by a name pattern.",
            "not_covered": "Not covered yet: hops through NEAR Intents and bridge deposit addresses, a withdrawal that equals a "
                           "later deposit, exchange hot wallets that are not on the list, and launches older than our data."},
        "holdings": _holdings_text(cfg),
        "live_state": _live_state_text(cfg),
        "insiders": "The insiders are the dev (the real owner behind the launch bot, and the creator) and every account the "
                    "dev sent launch tokens to. The outside buyers are everyone else who bought from the pool. If there "
                    "are no outside buyers, nobody was hurt and the launch is not a rug. Selling alone is not a rug: "
                    "most tokens see some selling.",
    }

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


def methodology() -> dict[str, Any]:
    """What /methodology shows: every signal with its description and the DEFAULT thresholds, the bands and the outcome
    labels. Never the production override."""
    cfg = default_config()
    sigs = _signal_list(cfg)
    b, o, g = cfg["bands"], cfg["outcomes"], cfg["rug"]
    lk, cl = cfg["links"], cfg["clusters"]
    sold, loss, share = g["insiders_sold_bps"] / 100, g["buyers_loss_near"], g["price_max_share"] * 100
    return {
        "rules_version": RULES_VERSION, "config_version": cfg["version"], "note": NOTE, "signals": sigs, "v1": methodology_v1(),
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
            {"id": "rug", "description": "rug_fast or rug_late. The risk score is calibrated against this label: its "
                                          "meaning is the chance of a rug within 24 hours."}],
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
        "insiders": "The insiders are the dev (the real owner behind the launch bot, and the creator) and every account the "
                    "dev sent launch tokens to. The outside buyers are everyone else who bought from the pool. If there "
                    "are no outside buyers, nobody was hurt and the launch is not a rug. Selling alone is not a rug: "
                    "most tokens see some selling.",
    }

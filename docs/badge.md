# Embed the risk badge

Any page can show the risk score of a NEAR token launch next to the token. The badge is a small SVG served by the site: no script, no cookie, no key, nothing to install. Its number is the stored risk card of the launch, the same number the launch page and `/api/v1` show. It is an estimate from on-chain behaviour: **analysis, not advice.**

## The embed code

```html
<a href="https://mihaikoodo.dev/launch/TOKEN">
  <img src="https://mihaikoodo.dev/badge/TOKEN.svg" alt="rugyomi risk score" height="20" loading="lazy">
</a>
```

`TOKEN` is the contract id of the launched token, for example `mossy.nearlytrade.near`. Use it exactly as the chain spells it (lower case).

## Styles

| URL | What you get | Size |
|---|---|---|
| `/badge/TOKEN.svg` or `?style=a` | One pill in the colour of the band: "Risk 45 · Elevated". | height 20, the width follows the text |
| `?style=b` | The lens of the logo with the number in the ring, the band word and the name beside it, on a dark tile. | always 116 x 40 |
| `?style=b&theme=light` | The same on a light tile. | always 116 x 40 |

The bands are Low (green), Elevated (amber) and High (red); their score ranges are in `/api/v1/meta`. Both styles bring their own colours (style `a` is a coloured pill, style `b` has its own tile), so they do not depend on your background.

## What the badge can say

- A score and a band: the launch has a card.
- "pending": we know the launch, and it has no card yet.
- "unknown" (HTTP 404, neutral grey): we have not seen this token. Hide the badge in that case.

## Limits and good manners

- A scored answer is cached for 60 seconds (`Cache-Control: public, max-age=60`); a launch that does not exist yet or has no card yet is answered with `Cache-Control: no-store`, so ask again as often as you need; the files may be loaded from any page (`Access-Control-Allow-Origin: *`).
- The site allows 60 requests per 10 seconds from one IP address. One badge is one request, so a list of 50 rows loaded at once can hit the limit: use `loading="lazy"`, and for tokens that are already old read the score from a file instead of calling the site (next section).
- Do not poll a badge on a timer. A new launch gets its card shortly after the launch; load the image when the row is shown.

## The past from a file

A score does not change after the card is issued, so the past does not have to be fetched at all. `python -m rugyomi.risk_snapshot` writes every score we have as `risk_snapshot.json`, `risk_snapshot.csv` and a small `risk_scores.json` (token -> `[score, band letter]`, `L`, `E` or `H`):

```
python -m rugyomi.risk_snapshot build --db rugyomi.db --out ./out
python3 risk_snapshot.py split risk_snapshot.json ./out        # the same files from a JSON made earlier, standard library only
```

Serve `risk_scores.json` with your page, look the token up there first, and use the badge only for a token that is not in the file.

## The JSON API

For anything more than the number (the reasons with a link to each transaction, the outcome after 24 hours) use the read-only API: `/api/v1/launch/{token}` and `/api/v1/launches?since=&limit=`. The README of the repository lists the fields. An AI agent can read the same API through the MCP server (`python -m rugyomi.mcp`).

## Turning it off

Remove the `<img>`. There is nothing else on your side: no account, no key, no stored state.

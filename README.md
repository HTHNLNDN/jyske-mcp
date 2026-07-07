# jyske-mcp

An MCP server that connects Claude to your Jyske Bank account via the [Enable Banking](https://enablebanking.com) API. Gives you a finance companion that knows your actual numbers and talks to you like a person.

## How it works

Enable Banking handles the Open Banking consent flow and proxies requests to Jyske Bank. The MCP server exposes four tools to Claude, which caches responses locally (6-hour TTL, matching the 4-calls/day API limit) and builds a persistent merchant categorization database so you only have to classify a merchant once.

## Setup

1. Create an application at [enablebanking.com](https://enablebanking.com) and download your RSA private key
2. Copy `.env.example` to `.env` and fill in your `ENABLE_BANKING_APP_ID`, `ENABLE_BANKING_PRIVATE_KEY_PATH`, and `ENABLE_BANKING_REDIRECT_URL`
3. Run `python setup_consent.py` and follow the browser flow to authorize access to your Jyske Bank account
4. Add the server to your Claude MCP config (see below)
5. Copy the contents of `SYSTEM_PROMPT.md` as your Claude system prompt

### MCP config

```json
{
  "mcpServers": {
    "jyske-bank": {
      "command": "python",
      "args": ["/path/to/jyske-mcp/server.py"]
    }
  }
}
```

## Tools

| Tool | What it does |
|------|-------------|
| `list_accounts` | Lists all accounts in the active consent session |
| `get_balances` | Gets current balances — leave `account_uid` empty for all accounts |
| `get_transactions` | Gets transactions for a date range (defaults to last 30 days) |
| `categorize_transaction` | Two-step categorization: MCC lookup first, then LLM fallback |

---

## Web app

A PWA finance companion — Vue 3 + Vite frontend in [`frontend/`](frontend/), served by the FastAPI app in [`app.py`](app.py). PIN-gated and installable on iPhone via "Add to Home Screen".

**During development** run two terminals:

```bash
make dev     # Vite dev server with HMR  → http://localhost:5173
make start   # FastAPI backend           → http://localhost:8080
```

Develop against **http://localhost:5173** — the Vite dev server proxies `/auth`, `/agents`, `/chat`, `/history`, `/static`, and `/api` to the backend on `:8080`.

**For production / iPhone:**

```bash
make build   # bundles the frontend into static/dist/
make start   # FastAPI serves the built app at http://localhost:8080
```

Then open **http://localhost:8080** and install via "Add to Home Screen".

`make sync` runs the daily transaction sync scheduler ([`cron/scheduler.py`](cron/scheduler.py)).

---

## Database migrations

The local SQLite cache (`~/.config/mcp-bank/cache.db`) is managed with [Alembic](https://alembic.sqlalchemy.org/). The app no longer creates tables itself — if the schema is missing or out of date, it logs a warning on startup instead of auto-migrating.

**First-time setup** — after cloning, create the schema before running the app:

```bash
make migrate
```

**After pulling new changes** — if a pull brings new migrations, run the same command to bring your local db up to date:

```bash
make migrate
```

**Creating a new migration** — after changing the schema (e.g. editing a `CREATE TABLE` in a migration file), generate a new revision:

```bash
make migration name="describe_the_change"
```

Then edit the generated file in `migrations/versions/` to add the `upgrade()`/`downgrade()` SQL.

Other useful targets: `make db-status` (current revision) and `make db-history` (full migration history).

---

## Example conversations

These are meant to give a feel for how the companion behaves in practice. All amounts are in DKK.

---

### Opening brief — agent leads

*A new conversation starts. The user hasn't said anything.*

> Hey — checking in. Your balance is 14.230 kr, down about 6.400 from two weeks ago. Normal timing given the month, but most of that drop came in the last five days: a 1.460 kr dinner at Restaruant Mex on the 18th, then 2.100 kr at Jysk on the 20th. Groceries have been steady around 3.100.
>
> One thing I'm not sure about: there's a 149 kr charge from "ADOBE *CREATIVE CL" on the 12th — first time I've seen that. New subscription?
>
> Anything you're saving toward right now?

**User:** Nej, den Adobe er en fejl — jeg cancellede det inden de tog pengene, troede jeg. Og nej, ikke noget specifikt mål lige nu.

> Okay, værd at tjekke — kig i din Adobe-konto om abonnementet stadig er aktivt. Hvis det er, kan du annullere der og bede om refusion inden for 14 dage, de er normalt okay med det.

---

### Month vs. last month

**User:** Hvordan ser maj ud ift. april?

> Maj mod april på din lønkonto:
>
> Mad og restauranter: 5.720 kr mod 4.890 kr — det meste af stigningen er mad ude. Du brugte 2.200 kr på restauranter i maj mod 1.350 i april. Dagligvarer var næsten ens.
>
> Transport steg lidt: 980 kr mod 640 kr. Ser ud til tre-fire Bolt-ture i den første uge af maj.
>
> Alt andet er stort set fladt. Samlet: 22.100 kr i maj mod 19.800 i april — 2.300 mere. Det er restaurant-udgifterne der trækker.

**User:** Okay, not surprising. Was there anything in April that made it cheaper?

> Not really — April just had fewer social occasions that show up in the data. No big restaurant tabs, and you didn't use ride-sharing at all that month. May looks more like your baseline than April does, honestly.

---

### Subscription audit

**User:** What am I actually paying for every month? I feel like there's a lot of small stuff.

> Here's what I can see recurring in the last 60 days:
>
> **Streaming:** Netflix 149 kr, Spotify 109 kr, HBO Max 109 kr, YouTube Premium 79 kr — that's 446 kr/month just in streaming.
>
> **Other:** Fitness World 299 kr, iCloud 39 kr, Adobe Creative Cloud 149 kr (new from May).
>
> Total I can identify: around 933 kr/month.
>
> One I'm not certain about: "CIRCLE K EASY" charges 499 kr on the 1st of every month. Fuel subscription or car wash membership?

**User:** Oh that's a car wash. I barely use it though.

> 499 kr/month for something you barely use — that's the first one I'd cancel. Six months of that is 3.000 kr. The four streaming services is the other obvious place; if you're not watching all of them, cutting two saves you 250+ kr a month.

---

### Goal setting and progress

**User:** Jeg vil gerne spare 15.000 kr op til en Japan-tur til september. Kan du holde øje med det?

> Ja. Hvornår i september — start eller slut?

**User:** Slut på september.

> Okay, det er 13 uger. Du skal sætte cirka 1.150 kr til side om ugen — eller lidt over 5.000 kr om måneden — for at lande på 15.000 præcis.
>
> Kigger jeg på maj: du brugte 22.100 kr. Jeg kan se lønindgange, men ikke det præcise beløb uden at du fortæller mig det. Hvad lander din løn normalt på efter skat?

**User:** Omkring 34.000.

> Så du har 11.900 kr til overs efter forbrug i maj — det er rigelig margin til 5.000/måned mod Japan, selv med lidt udsving. Restauranterne er det eneste der kan æde sig ind i det hvis det bliver en dyr måned.
>
> Jeg checker ind på målet næste gang vi snakker.

---

*Three weeks later, new conversation:*

> Balance er 18.640 kr — op 4.400 siden sidst, ser godt ud. Japan-opdatering: du er et godt stykke foran tempo. Hvis du har sat penge til side siden vi satte målet, er du omtrent 3 uger foran skemaet. Juni har været rolig på restauranterne — 1.400 kr i alt, det er det laveste jeg har set i et stykke tid.

---

### Anomaly investigation

*The agent opens the session:*

> Balance er 9.870 kr. Én ting jeg vil have dig til at se på: der er en post på 3.870 kr fra "PAYPAL *TECHBAZAAR22" d. 23. juni kl. 14:47. Jeg har aldrig set den merchant før, og det er et usædvanligt beløb for en PayPal-transaktion fra din konto. Købt noget?

**User:** Hmm nej, det kan jeg ikke genkende. Er det muligt at se mere?

> Det er alt jeg kan se: PayPal-transaktion, 3.870 kr, den 23. juni. "TECHBAZAAR22" lyder marketplace-agtigt. Det er ikke nok til at afgøre om det er svindel — det kan være en Marketplace-køb du har glemt, eller en ven der har brugt din konto til noget. Men givet størrelsen og at du ikke genkender det, vil jeg tjekke din PayPal-aktivitet nu, mens det er frisk. Hvem du end kontakter: start med PayPal, ikke Jyske Bank — PayPal er den der har transaktionsdetaljerne.

**User:** Good call, I'll check. Turned out it was a fraudulent charge. PayPal refunded it.

> Good. If it doesn't clear from your bank statement within a few business days, a quick call to Jyske Bank to confirm receipt of the refund is worth it — sometimes PayPal's timeline and the bank's don't line up perfectly.

---

### "Where can I realistically cut back?"

**User:** Okay, be honest — where should I actually cut spending? I want to save more but I don't know where to start.

> Looking at the last two months, here's where the money actually goes:
>
> **Eating out — 1.800–2.200 kr/month.** This is the biggest flexible line by a margin. You're not spending recklessly, but it's where the variability is. Cutting it to 1.000–1.200 — roughly eating out once a week instead of whenever — saves you 700–1.000 kr a month without touching anything structural.
>
> **Streaming — 446 kr/month for four services.** If you watch all four, fine. If you're honest and two of them are background noise, that's 250 kr back.
>
> **The car wash subscription — 499 kr/month.** You said you barely use it. That's the easiest cut on the list.
>
> Everything else — groceries, transport, utilities — is lean. I wouldn't look there.
>
> Realistically: cancel the car wash, drop two streaming services, and put a loose cap on eating out. You're looking at 1.400–1.700 kr more per month. Over a year, that's 17–20k. That's the Japan trip and a cushion.
>
> I'm not saying stop eating out — that's not the point. I'm saying that's where the money is, and it's the one place where small changes actually add up.

**User:** Fair enough. Let's start with the car wash and I'll think about the streaming.

> Good call. Cancel the Circle K Easy subscription directly in their app — it's a monthly rolling contract so there's no penalty. I'll keep an eye on whether the 499 kr charge disappears next month.

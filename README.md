# jyske-mcp

An MCP server that connects Claude to your Jyske Bank account via the [Enable Banking](https://enablebanking.com) API. Gives you a finance companion that knows your actual numbers and talks to you like a person.

## How it works

Enable Banking handles the Open Banking consent flow and proxies requests to Jyske Bank. A scheduled sync job pulls accounts/balances/transactions into a local SQLite cache; the MCP server never talks to Enable Banking itself — it only reads from that cache (see [`src/jyske_mcp/mcp/server.py`](src/jyske_mcp/mcp/server.py)). A persistent merchant categorization database means you only have to classify a merchant once. A companion PWA (Vue 3 + Vite, served by the same FastAPI app) gives a chat UI on top of the same data.

## Setup

1. Create an application at [enablebanking.com](https://enablebanking.com) and download your RSA private key.
2. Copy `.env.example` to `.env` and fill in `ENABLE_BANKING_APP_ID`, `ENABLE_BANKING_PRIVATE_KEY_PATH`, `ENABLE_BANKING_REDIRECT_URL`, `APP_PIN`, `SESSION_SECRET`, and `SCHEDULER_SECRET` (see [Environment variables](#environment-variables) below).
3. Run `make install` (editable install of the `src/` package + runtime dependencies, inside your venv). Contributors should use `pip install -r requirements-dev.txt` instead, which adds `pre-commit`.
4. Run `make migrate` to create the local SQLite schema (`~/.config/mcp-bank/cache.db`) — the app does not create tables itself, so this has to happen before first run.
5. Run `python scripts/setup_consent.py` and follow the browser flow to authorize access to your Jyske Bank account.
6. Add the server to your Claude MCP config (see below).
7. Copy the contents of [`src/jyske_mcp/prompts/system_prompt.md`](src/jyske_mcp/prompts/system_prompt.md) as your Claude system prompt.

### MCP config

```json
{
  "mcpServers": {
    "jyske-bank": {
      "command": "/path/to/jyske-mcp/.venv/bin/python",
      "args": ["-m", "jyske_mcp.mcp.server"]
    }
  }
}
```

### Environment variables

Required, from `.env.example`:

| Variable | Purpose |
|---|---|
| `ENABLE_BANKING_APP_ID`, `ENABLE_BANKING_PRIVATE_KEY_PATH`, `ENABLE_BANKING_REDIRECT_URL` | Enable Banking application credentials and consent callback URL |
| `APP_PIN` | PIN gating the web app |
| `SESSION_SECRET` | Signs web app session cookies |
| `SCHEDULER_SECRET` | Shared secret between the FastAPI app and the internal scheduler service (`127.0.0.1:8081`). If unset, the scheduler fails closed and rejects all requests — both the manual "sync now" button and the nightly cron sync stop working until it's set. |
| `LLM_MODEL`, and the matching provider key (e.g. `ANTHROPIC_API_KEY`) | Chat model, via LiteLLM |

Optional:

| Variable | Purpose |
|---|---|
| `LLM_FALLBACK_MODEL` | Fallback model if the primary LLM call fails |
| `APP_HOST`, `APP_PORT` | Web server bind address/port (default `0.0.0.0:8080`) |
| `LANGFUSE_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Optional observability via a self-hosted Langfuse instance (see [Web app](#web-app)) |

## Tools

The MCP server exposes 23 tools, all reading from the local SQLite cache (never calling Enable Banking directly).

**Accounts & balances**

| Tool | What it does |
|---|---|
| `list_accounts` | Lists all accounts in the active consent session |
| `get_balances` | Gets cached balances — leave `account_uid` empty for all accounts |
| `get_transactions` | Gets categorized transactions for a date range (defaults to last 30 days) |
| `get_sync_status` | Reports when data was last synced and flags stale data |

**Spending & budgets**

| Tool | What it does |
|---|---|
| `categorize_transaction` | Two-step categorization: merchant/MCC lookup first, then LLM fallback |
| `get_spending` | Sums spending by category/month, grouped as requested; totals are per-currency |
| `compare_spending` | Compares spending in one month against a baseline month, with proration for in-progress months |
| `set_budget` | Sets a spending budget for a category |
| `get_budget_status` | Gets current budget status against limits (DKK-primary) |
| `get_overspend_patterns` | Flags categories overspent 3+ consecutive months |

**Goals**

| Tool | What it does |
|---|---|
| `get_goals` | Lists active savings/spending goals with progress |
| `set_goal` | Creates a new goal (target amount, purpose, deadline) |
| `update_goal_progress` | Updates the current amount saved toward a goal |
| `goal_pace` | Computes whether a goal is on track and the pace needed to hit its deadline |

**Recurring charges**

| Tool | What it does |
|---|---|
| `recurring_charges` | Detects subscriptions and frequent merchants from transaction history, flagging any that have gone quiet |
| `confirm_recurring_status` | Records the user's answer on whether a flagged recurring charge is still active |

**Memory & onboarding**

| Tool | What it does |
|---|---|
| `get_memory` | Returns the stored user profile and recent session summaries — call at session start |
| `update_memory` | Saves a session summary and any profile updates — call at session end |
| `get_onboarding_status` | Checks whether budget onboarding is complete, and current stage if not |
| `set_onboarding_stage` | Records progress through budget onboarding, one stage at a time |
| `complete_onboarding` | Marks budget onboarding complete |

**Tip of the day**

| Tool | What it does |
|---|---|
| `get_current_tip` | Returns today's financial tip, generated overnight by the scheduler |
| `submit_tip_feedback` | Records the user's accept/reject reaction to a tip, with reasoning |

---

## Web app

A PWA finance companion — Vue 3 + Vite frontend in [`frontend/`](frontend/), served by the FastAPI app in [`src/jyske_mcp/web/app.py`](src/jyske_mcp/web/app.py). PIN-gated and installable on iPhone via "Add to Home Screen".

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

`make sync` runs the scheduler service ([`src/jyske_mcp/jobs/scheduler.py`](src/jyske_mcp/jobs/scheduler.py)) on `127.0.0.1:8081`. It's the single owner of syncing — the web app's manual "sync now" button calls into it over an authenticated internal endpoint rather than syncing itself, and it also runs the following on a schedule:

- daily transaction sync — 03:00
- nightly evals — 04:00
- nightly tip-of-the-day generation — 04:30
- a sync-freshness check — every 6 hours

Requires `SCHEDULER_SECRET` to be set (see [Environment variables](#environment-variables)) — without it, the scheduler rejects every request, including the manual sync button.

**Optional observability:** `make langfuse` starts a self-hosted [Langfuse](https://langfuse.com) instance via [`docker/langfuse/`](docker/langfuse/) at `http://localhost:3000`, for tracing chat/LLM calls. It's off unless `LANGFUSE_ENABLED=true` and the Langfuse keys are set; the compose setup uses its own gitignored env file under `docker/langfuse/`.

---

## Database migrations

The local SQLite cache (`~/.config/mcp-bank/cache.db`) is managed with [Alembic](https://alembic.sqlalchemy.org/). The app no longer creates tables itself — if the schema is missing or out of date, it logs a warning on startup instead of auto-migrating.

**First-time setup** — after cloning, create the schema before running the app (also step 4 above):

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

## Tests

`make test` (or `pytest`) runs the test suite under [`tests/`](tests/) — covers categorization/recurring-charge classification, spending aggregation and proration, goal pacing, mixed-currency handling, and config security.

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

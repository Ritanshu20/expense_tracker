# Smart Expense Tracker

A backend-focused Django app for personal and shared expenses. It splits bills fairly, works out who owes whom, records payments, tracks monthly budgets, and produces reports and exports. The frontend stays simple on purpose, and the logic and its tests are the main work.

## Features

- **Accounts:** register, log in, log out and edit your profile. Every page needs a login unless it is marked as public.
- **Categories:** 11 default categories, plus your own. Names are unique in any letter case.
- **Expenses:** personal and shared, with search, filters and pagination.
- **Splitting:** equal, custom amount or percentage, with several people paying one bill. Shares always add up to the exact amount.
- **Who owes whom:** a net balance per pair of people, after the payments already recorded.
- **Settle up:** record that someone paid you back. Partial payments and overpayments both work.
- **Dashboard:** my spending today, this week, this month and in total, paid by me, totals by category, payment method and person, and budget status, with two simple charts.
- **Reports:** daily, weekly, monthly, yearly and custom range.
- **Budgets:** a monthly limit with spent, remaining, usage percent and a warning threshold.
- **Exports:** CSV, Excel and PDF, with the same filters as the expense list.
- **REST API:** token or session login, with Swagger docs.
- **Admin:** Django admin for every model, which refuses an expense whose money does not add up.

## Screenshots

These use the data from `python manage.py seed_demo`.

| Dashboard | Dashboard on a phone |
|---|---|
| ![Dashboard](screenshots/dashboard.png) | ![Dashboard at phone width](screenshots/dashboard-phone.png) |

| Adding a shared expense | Monthly report |
|---|---|
| ![Form for a shared expense](screenshots/shared-expense-form.png) | ![Monthly report](screenshots/monthly-report.png) |

![Expense list with filters and downloads](screenshots/expense-list.png)

## Tech stack

Python 3.13, Django 5.2 LTS, Django REST Framework, drf-spectacular, PostgreSQL (driver: psycopg), Django templates with Bootstrap 5, Chart.js, openpyxl, reportlab, Ruff, pip-audit, GitHub Actions.

The tests run on PostgreSQL too. SQLite is refused by the settings, because some rules, such as the case-insensitive unique index, only mean something on the real database.

## Architecture

| Part | What it does |
|---|---|
| `config/` | Settings, read from environment variables, and the root URLs |
| `accounts/` | Registration, login and the profile page |
| `expenses/` | The models and the main logic: categories, expenses, budgets, settlements and the API |
| `dashboard/` | The home page and the dashboard |
| `reports/` | Reports and export files |

Inside `expenses/`, the code is split by job:

```mermaid
flowchart LR
    Browser --> Views[views.py and api_views.py<br>thin, no business rules]
    Views --> Forms[forms.py and serializers.py<br>check the input]
    Views --> Services[services.py<br>split, balances, settlements, saving]
    Views --> Selectors[selectors.py<br>read queries and totals]
    Services --> Models[models.py<br>tables and constraints]
    Selectors --> Models
    Models --> PostgreSQL
```

The website and the API call the same services and selectors, so they cannot disagree.

## Database

```mermaid
erDiagram
    USER ||--o{ CATEGORY : "owns custom"
    USER ||--o{ EXPENSE : creates
    USER ||--o{ EXPENSE_PARTICIPANT : "takes part as"
    USER ||--o{ BUDGET : sets
    USER ||--o{ SETTLEMENT_PAYMENT : "pays or receives"
    CATEGORY ||--o{ EXPENSE : groups
    EXPENSE ||--o{ EXPENSE_PARTICIPANT : has
```

- **Category:** default categories have no owner. `owner` + lowercase `name` is unique, and this also covers default categories.
- **Expense:** one bill, with an amount greater than zero. A category that an expense uses cannot be deleted.
- **ExpenseParticipant:** one person's part in one expense, with `paid_amount` and `share_amount`. There is no single "paid by" field, because several people can pay one bill. A personal expense has one row, where both amounts equal the expense amount.
- **Budget:** one per user per month.
- **SettlementPayment:** real money that one person paid another. It lowers a debt, and it is not spending.

The database itself refuses negative amounts, duplicate participants, duplicate budgets and a payment to yourself, even if Python validation is skipped.

## How the money logic works

### Splitting and rounding

Money is always a `Decimal`, and floats are refused. Rounding each share on its own would lose money: 100.00 / 3 rounds to 33.33 three times, which is only 99.99. So the calculation uses the **largest remainder method**:

1. Convert the total to whole paise.
2. Give everyone their share rounded down, and remember what was cut off.
3. Hand out the leftover paise one at a time: largest remainder first, ties by participant order.

| Split | Input | Result |
|---|---|---|
| Equal | 100 between A, B, C | 33.34, 33.33, 33.33 |
| Percentage | 10 at 33.33%, 33.33%, 33.34% | 3.33, 3.33, 3.34 |
| Custom | 1000 as 700 and 300 | 700.00, 300.00 |

Input values are rounded to 2 places with `ROUND_HALF_UP`. Percentages must add up to exactly 100, and custom shares must add up to the amount.

### Spending definitions

- **My spending** is the sum of my shares, which is what I consumed. Budgets, totals and reports use this.
- **Paid by me** is the sum of what I paid, which is the cash that left my pocket. It is always shown separately.
- A shared expense that I created without taking part in it does not count in my spending.

### Balances and settlement

A person's balance on an expense is what they paid minus their share. To settle an expense, a greedy algorithm repeatedly matches the largest debtor with the largest creditor and transfers the smaller of the two amounts. Each transfer clears at least one person, so n people need at most n - 1 payments.

**Example: a dinner of 3000, shared equally**

| Person | Paid | Share | Balance |
|---|---|---|---|
| Rahul | 1500 | 1000 | +500 |
| Aman | 1000 | 1000 | 0 |
| Rohit | 500 | 1000 | -500 |

Result: Rohit pays Rahul 500.

**Example: A +700, B +300, C -600, D -400** gives C pays A 600, D pays B 300, and D pays A 100.

### Who owes whom across many expenses

Each expense is settled on its own, then debts between the same two people are netted, and recorded payments are subtracted. So the app never suggests a payment between two people who never shared an expense. Nothing is stored as a balance, so editing or deleting an expense or a payment updates everything at once.

**Example:** Rohit owes Rahul 500. If he pays 200, he still owes 300. If he pays 500, nobody owes anything. If he pays 700, Rahul now owes Rohit 200.

## Permissions

| You are | View an expense | Edit or delete it |
|---|---|---|
| The creator | Yes | Yes |
| A participant | Yes | No, you get a 403 |
| Anyone else | No, you get a 404 | No, you get a 404 |

- Categories, budgets, reports and exports are limited to your own data in the same way.
- Both people on a payment can view it, and only the person who recorded it can change it.
- There is no list of users. Participants are added by exact username or email.

## Setup on Windows

Run every command in PowerShell from the project root.

### 1. Python packages

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
```

Open `.env` and set a real `SECRET_KEY`. The command to generate one is in `.env.example`.

### 2. PostgreSQL

The app needs PostgreSQL 16 or newer. Choose one option.

**Option A: normal installer.** Use the [official Windows installer](https://www.postgresql.org/download/windows/). It needs admin rights, and it runs PostgreSQL as a Windows service that starts automatically. Set the superuser password when it asks. Its tools are in `C:\Program Files\PostgreSQL\18\bin` by default.

**Option B: portable, no admin rights.** This keeps everything in the git-ignored `.postgres` folder and installs no service, so the server must be started by hand after a reboot.

1. Download the Windows x86-64 zip for the newest PostgreSQL 18 from the [EDB binaries page](https://www.enterprisedb.com/download-postgresql-binaries).
2. Verify the download. EDB publishes no checksum and most files inside are not signed, so compare against the `hash` in [Scoop's manifest](https://raw.githubusercontent.com/ScoopInstaller/Main/master/bucket/postgresql.json). The version in that file must match your zip.
   ```powershell
   Get-FileHash .\postgresql-*.zip -Algorithm SHA256
   ```
3. Create the folder `.postgres\pgsql` and copy `pgsql\bin`, `pgsql\lib` and `pgsql\share` from the zip into it.
4. Create the database cluster. You will be asked to type a superuser password.
   ```powershell
   .\.postgres\pgsql\bin\initdb.exe -D .postgres\data -U postgres --encoding=UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C --auth=scram-sha-256 --pwprompt
   ```
5. Start the server, using the command below.

Everyday commands for the portable option:

```powershell
# Start (needed after every reboot)
Start-Process .\.postgres\pgsql\bin\pg_ctl.exe -ArgumentList 'start','-D','.postgres\data','-l','.postgres\server.log','-w' -WindowStyle Hidden

# Check that it is running
.\.postgres\pgsql\bin\pg_isready.exe -h localhost -p 5432

# Stop
.\.postgres\pgsql\bin\pg_ctl.exe stop -D .\.postgres\data -w
```

If a `manage.py` command waits about 20 seconds and then fails with `connection timeout expired`, the server is not running.

### 3. App role and database

The app uses its own role, which is not a superuser. It has `CREATEDB` because Django's test runner creates its own test database.

Open `psql` as the superuser, using the one from your option:

```powershell
.\.postgres\pgsql\bin\psql.exe -h localhost -U postgres -d postgres
```

Choose a password made of letters and digits, because other characters would need escaping in the URL below:

```sql
CREATE ROLE expense_tracker_user LOGIN CREATEDB PASSWORD 'choose-a-password';
CREATE DATABASE expense_tracker OWNER expense_tracker_user ENCODING 'UTF8' TEMPLATE template0;
\q
```

Add this line to `.env` with the same password. Never commit `.env`.

```
DATABASE_URL=postgres://expense_tracker_user:choose-a-password@localhost:5432/expense_tracker
```

### 4. Migrate and run

```powershell
python manage.py migrate
python manage.py test
python manage.py runserver
```

The test run must print `Creating test database` and end with `OK`. The app is then at http://127.0.0.1:8000/.

### 5. Optional: an admin account and demo data

```powershell
python manage.py createsuperuser
python manage.py seed_demo
```

`seed_demo` creates the users Rahul, Aman and Rohit, the 3000 dinner, two personal expenses, a custom category and a budget. It prints the password of the new users, or use `--password` to choose one. Running it again creates nothing new, and it refuses to run with `DEBUG` off.

## Environment variables

| Name | Required | Meaning |
|---|---|---|
| `SECRET_KEY` | Yes | Django's secret key. The app refuses to start without it |
| `DATABASE_URL` | One of these two | PostgreSQL connection URL. Any other database is refused |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | One of these two | The same connection as separate values |
| `DEBUG` | No | `True` on your own machine. Off if unset |
| `ALLOWED_HOSTS` | In production | Comma-separated host names |
| `CSRF_TRUSTED_ORIGINS` | In production | Comma-separated origins, such as `https://example.com` |
| `SECURE_SSL_REDIRECT` | No | On by default when `DEBUG` is off |
| `SECURE_HSTS_SECONDS` | No | One hour by default when `DEBUG` is off. Raise it once HTTPS is known to work |
| `BUDGET_WARNING_THRESHOLD_PERCENT` | No | When the budget warning starts. Default 80 |

## URLs

| URL | Page | Login needed |
|---|---|---|
| `/` | Home. Logged-in users go to the dashboard | No |
| `/register/`, `/login/` | Create an account, log in | No |
| `/logout/` | Log out (POST only) | Yes |
| `/profile/` | Your profile | Yes |
| `/dashboard/` | Your spending, budget and who owes whom | Yes |
| `/categories/`, `/categories/add/` | Default categories and your own, add one | Yes |
| `/categories/<id>/edit/`, `/categories/<id>/delete/` | Change your category | Yes |
| `/expenses/`, `/expenses/add/` | Your expenses, add one | Yes |
| `/expenses/<id>/` | One expense. You must be its creator or a participant | Yes |
| `/expenses/<id>/edit/`, `/expenses/<id>/delete/` | Change an expense. Creator only | Yes |
| `/settlements/`, `/settlements/add/` | Who owes whom and your payments, record one | Yes |
| `/settlements/<id>/` | One payment. You must be one of its two people | Yes |
| `/settlements/<id>/edit/`, `/settlements/<id>/delete/` | Change a payment. Only the person who recorded it | Yes |
| `/reports/` | List of reports | Yes |
| `/reports/daily/`, `/reports/weekly/`, `/reports/monthly/`, `/reports/yearly/` | One period. `?date=` picks any day in it | Yes |
| `/reports/custom/` | Any range of dates | Yes |
| `/reports/export/csv/`, `/reports/export/xlsx/`, `/reports/export/pdf/` | Download your expenses, with the same filters as `/expenses/` | Yes |
| `/budget/`, `/budget/add/` | Your monthly budgets, add one | Yes |
| `/budget/<id>/edit/`, `/budget/<id>/delete/` | Change your budget | Yes |
| `/admin/` | Django admin | Staff account |

## REST API

Swagger docs are at `/api/docs/` after logging in. Money is sent as strings, such as `"250.50"`, so it stays exact.

| Method and URL | What it does |
|---|---|
| `POST /api/token/` | Exchange a username and password for a token |
| `GET /api/expenses/` | Your expenses, with the same filters as the website |
| `POST /api/expenses/` | Add a personal or shared expense |
| `GET /api/expenses/<id>/` | One expense. You must be its creator or a participant |
| `PUT /api/expenses/<id>/` | Replace an expense. Creator only |
| `DELETE /api/expenses/<id>/` | Delete an expense. Creator only |
| `GET /api/dashboard/` | The same numbers as the dashboard |
| `GET /api/reports/monthly/?date=2026-11-15` | The monthly report for that date's month |
| `GET /api/settlements/` | Who owes whom, and the recorded payments |

Send the token in a header on every other request:

```
Authorization: Token <your token>
```

A shared expense looks like this:

```json
{
  "title": "Dinner",
  "amount": "3000.00",
  "date": "2026-11-10",
  "category": 1,
  "payment_method": "upi",
  "expense_type": "shared",
  "split_type": "equal",
  "participant_rows": [
    {"person": "rahul", "paid_amount": "1500.00"},
    {"person": "aman@example.com", "paid_amount": "1000.00"},
    {"person": "rohit", "paid_amount": "500.00"}
  ]
}
```

`split_type` is `equal`, `custom` or `percentage`. For the last two, each row also needs `share_value`, which is an amount or a percent.

## Common commands

| Task | Command |
|---|---|
| Apply migrations | `python manage.py migrate` |
| Tests | `python manage.py test` |
| Tests, failing on deprecated APIs | `python -W error::DeprecationWarning manage.py test` |
| Lint and format | `ruff check .` and `ruff format .` |
| Django checks | `python manage.py check` |
| Missing migrations | `python manage.py makemigrations --check --dry-run` |
| Vulnerable dependencies | `pip-audit` |
| Production settings check | `python manage.py check --deploy` |

GitHub Actions runs Ruff, `check`, `makemigrations --check` and all the tests against a PostgreSQL service on every push and pull request. It does not deploy.

## Deployment

The app is not deployed yet.

What is ready: with `DEBUG` off it requires HTTPS, sends cookies only over HTTPS and turns on HSTS for one hour. `check --deploy` reports only W005 and W021, which are left off on purpose, because HSTS for subdomains and preloading affect a whole domain.

What deployment still needs: a production server such as gunicorn, static file serving such as WhiteNoise with `collectstatic`, `SECURE_PROXY_SSL_HEADER` if the host uses a proxy, and logging.

## Future improvements

- A login rate limit, because repeated password guesses are not slowed down today.
- An accept step when someone adds you to an expense or records a payment with you. Today it takes effect straight away.
- A Content Security Policy header. Django's built-in one needs Django 6, and the templates are already ready for it because they use no inline scripts.
- Groups, recurring expenses, per-category budgets and receipt attachments, which are not built.
- Stored balances, if a user with a very large number of shared expenses makes the dashboard slow.

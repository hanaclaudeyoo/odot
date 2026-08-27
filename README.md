# Odot

Odot is a simple single-user todo app based on Eisenhower matrix principles. Tasks are rated by importance, urgency, difficulty, category, and time estimate, then can be viewed in a sortable list, plotted on a matrix, filtered by category, or pulled based on current energy level.

## Dev Setup

Install backend dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
```

Install frontend dependencies:

```sh
npm install --prefix frontend
```

## Launch Dev

Start the backend API:

```sh
npm run backend:dev
```

Start the frontend app in another terminal:

```sh
npm run frontend:dev
```

Open:

```sh
http://127.0.0.1:5173/
```

## Checks

Run backend tests:

```sh
.venv/bin/python -m pytest backend/tests
```

Build the frontend:

```sh
npm run frontend:build
```

## Database Import/Export

Export the current database to a SQL dump:

```sh
python3 scripts/db_io.py export --out scripts/odot-dump.sql
```

Import a SQL dump into the current database:

```sh
python3 scripts/db_io.py import scripts/odot-dump.sql
```

Both commands default to the app database, or `ODOT_DB_PATH` if set. Import replaces existing data and creates a `.bak` backup first.

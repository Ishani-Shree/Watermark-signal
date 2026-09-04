# Watermark

An attention filter, not a price tracker. Tells you what actually changed
since you last checked, explains why it mattered, and stays quiet about
everything else.

Full build plan: see `BUILD_PLAN.md`. Trade-off log: see `DECISIONS.md`.

## Status

Hour 0-2 skeleton: backend + frontend wired end-to-end against the replay
data provider. Detection/scoring, auth, and the real digest UI are not built
yet.

## Local setup

### Backend
```
cd backend
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

### Frontend
```
cd frontend
npm install
cp .env.example .env
npm run dev
```

## What "meaningful change" means here

(to be filled in as scoring lands — see BUILD_PLAN.md section 5)

## What we deliberately did not build

See BUILD_PLAN.md section 11.

# Setup local

## Pré-requisitos

- Python 3.12+
- Node.js 22+
- PostgreSQL 16+ (local, Docker ou Supabase)

## 1. Clonar e configurar ambiente

```bash
git clone <url-do-repositorio>
cd trevo
cp .env.example .env
```

Edite `.env` com `DATABASE_URL` e `JWT_SECRET_KEY` (mínimo 32 caracteres).

## 2. Banco e backend

```bash
python -m pip install -r requirements-dev.txt
python migrate.py
python -m uvicorn main:app --reload --port 8000
```

`requirements-dev.txt` inclui `requirements.txt` (produção) mais pytest/ruff/bandit —
o passo 5 abaixo (ruff, pytest) depende deles.

Health: `http://localhost:8000/api/health`

## 3. Frontend

```bash
cd frontend
npm ci
```

Crie `frontend/.env.local` se necessário:

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

```bash
npm run dev
```

App: `http://localhost:3000`

## 4. Docker (alternativa)

```bash
docker compose up --build
```

## 5. Verificação rápida

```bash
python -m ruff check .
python -m pytest tests/unit -q
cd frontend && npm run typecheck && npm run build
```

Integração com banco: veja [testing.md](testing.md).

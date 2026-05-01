# Lauren Project Layout

## Contents
- [Recommended tree](#recommended-tree)
- [main.py pattern](#mainpy-pattern)
- [pyproject.toml essentials](#pyprojecttoml-essentials)
- [Environment variables](#environment-variables)

---

## Recommended tree

```
my_project/
├── src/
│   └── my_app/
│       ├── __init__.py
│       ├── main.py                  # LaurenFactory.create + uvicorn entry
│       ├── app_module.py            # root @module
│       ├── users/
│       │   ├── __init__.py
│       │   ├── users_module.py
│       │   ├── users_controller.py
│       │   ├── users_service.py
│       │   └── schemas.py           # Pydantic DTOs
│       ├── auth/
│       │   ├── __init__.py
│       │   ├── auth_module.py
│       │   └── auth_guard.py
│       ├── middlewares/
│       │   ├── cors_middleware.py
│       │   └── logging_middleware.py
│       └── interceptors/
│           └── timing_interceptor.py
├── tests/
│   ├── conftest.py                  # env vars FIRST, then imports
│   ├── unit/
│   │   └── test_users_service.py
│   └── integration/
│       └── test_users_api.py
├── .env
├── .env.example
├── pyproject.toml
└── README.md
```

## main.py pattern

```python
# src/my_app/main.py
from dotenv import load_dotenv
load_dotenv()  # MUST run before any app module is imported

from my_app.app_module import AppModule
from my_app.interceptors.timing_interceptor import TimingInterceptor
from my_app.middlewares.cors_middleware import CorsMiddleware
from my_app.middlewares.logging_middleware import LoggingMiddleware
from lauren import LaurenFactory

app = LaurenFactory.create(
    AppModule,
    global_middlewares=[CorsMiddleware, LoggingMiddleware],
    global_interceptors=[TimingInterceptor],
)
```

## pyproject.toml essentials

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "lauren>=0.1",
    "uvicorn[standard]>=0.29",
    "python-dotenv>=1.0",
    "pydantic>=2.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
target-version = "py311"
```

## Environment variables

`.env.example` (commit this):
```
PORT=8000
LOG_LEVEL=INFO
SECRET_KEY=change-me
ALLOWED_ORIGINS=http://localhost:3000
DATABASE_URL=postgresql://user:pass@localhost/mydb
```

`.env` (gitignore this — actual values):
```
PORT=8000
SECRET_KEY=supersecret
DATABASE_URL=postgresql://...
```

Singletons are constructed during phase 6 of startup, after `load_dotenv()` runs. Services should read env vars in `__init__` or `@post_construct`, not at import time.

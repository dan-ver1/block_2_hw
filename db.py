"""Доступ до навчальної бази PostgreSQL лише для читання.

Усі SQL-запити зафіксовані в цьому модулі. Текст запитання користувача
сюди не передається і в SQL не підставляється.

Параметри підключення беруться зі змінних середовища PG* (PGHOST, PGPORT,
PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE), які завантажуються з локального .env.
"""

import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

REQUIRED_DB_VARS = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
STATEMENT_TIMEOUT_MS = 15_000
CONNECT_TIMEOUT_S = 10

# Робоче визначення сертифікованого запису (бізнес-визначення не підтверджене).
CERTIFIED_STATE = "certified"
DEFINITION_NOTE = (
    "Сертифікований запис — рядок enrollments з funnel_state = 'certified'. "
    "Це робоче визначення; бізнес-визначення завершеного навчання не підтверджене."
)

KPI_SQL = """
    SELECT COUNT(*) AS total_enrollments,
           COUNT(*) FILTER (WHERE funnel_state = %(certified)s) AS certified_enrollments
    FROM enrollments
"""

FUNNEL_SQL = """
    SELECT funnel_state, COUNT(*) AS enrollments
    FROM enrollments
    GROUP BY funnel_state
    ORDER BY enrollments DESC, funnel_state
"""

QUALITY_SQL = """
    SELECT COUNT(*) FILTER (WHERE progress_pct > 100) AS progress_above_100,
           COUNT(*) FILTER (WHERE progress_pct >= 100
                              AND funnel_state IS DISTINCT FROM %(certified)s)
               AS progress_100_plus_not_certified,
           COUNT(*) FILTER (WHERE completed_at IS NOT NULL) AS completed_at_filled,
           COUNT(*) FILTER (WHERE completed_at IS NOT NULL
                              AND funnel_state IS DISTINCT FROM %(certified)s)
               AS completed_at_filled_not_certified
    FROM enrollments
"""

TOP_COURSES_LIMIT = 5

# Курс = course_id (курс у межах спеціалізації). dim_course.course_id — PK,
# тому з'єднання не множить записи. Персональні поля не вибираються.
TOP_COURSES_SQL = f"""
    SELECT d.course_id,
           btrim(d.course) AS course_name,
           btrim(d.specialization_name) AS specialization_name,
           COUNT(*) AS enrollments,
           COUNT(*) FILTER (WHERE e.funnel_state = %(certified)s) AS certified_enrollments
    FROM enrollments e
    JOIN dim_course d ON d.course_id = e.course_id
    GROUP BY d.course_id, d.course, d.specialization_name
    ORDER BY enrollments DESC, d.course_id
    LIMIT {TOP_COURSES_LIMIT}
"""


class DatabaseConfigError(RuntimeError):
    """У .env бракує змінних для підключення до бази."""


def missing_db_vars() -> list[str]:
    return [name for name in REQUIRED_DB_VARS if not os.getenv(name)]


def _fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    missing = missing_db_vars()
    if missing:
        raise DatabaseConfigError("У .env не заповнено: " + ", ".join(missing))

    # libpq сам читає PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE.
    with psycopg.connect(connect_timeout=CONNECT_TIMEOUT_S, row_factory=dict_row) as conn:
        conn.read_only = True  # транзакція відкривається як READ ONLY
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
    return rows


def fetch_certification_kpis() -> dict:
    row = _fetch_all(KPI_SQL, {"certified": CERTIFIED_STATE})[0]
    total = int(row["total_enrollments"])
    certified = int(row["certified_enrollments"])
    rate = round(certified / total * 100, 2) if total else 0.0
    return {
        "total_enrollments": total,
        "certified_enrollments": certified,
        "certification_rate_pct": rate,
        "definition": DEFINITION_NOTE,
    }


def fetch_funnel_distribution() -> dict:
    rows = _fetch_all(FUNNEL_SQL)
    total = sum(int(r["enrollments"]) for r in rows)
    states = [
        {
            "funnel_state": r["funnel_state"] if r["funnel_state"] is not None else "(не вказано)",
            "enrollments": int(r["enrollments"]),
            "share_pct": round(int(r["enrollments"]) / total * 100, 2) if total else 0.0,
        }
        for r in rows
    ]
    return {
        "total_enrollments": total,
        "states": states,
        "note": "Порядок етапів воронки в базі не описаний; значення відсортовано за кількістю записів.",
    }


def fetch_top_courses() -> dict:
    rows = _fetch_all(TOP_COURSES_SQL, {"certified": CERTIFIED_STATE})
    courses = []
    for r in rows:
        enrollments = int(r["enrollments"])
        certified = int(r["certified_enrollments"])
        courses.append({
            "course_id": r["course_id"],
            "course_name": r["course_name"],
            "specialization_name": r["specialization_name"],
            "enrollments": enrollments,
            "certified_enrollments": certified,
            "certified_share_pct": round(certified / enrollments * 100, 2) if enrollments else 0.0,
        })
    return {
        "courses": courses,
        "definition": DEFINITION_NOTE,
        "note": (
            f"До {TOP_COURSES_LIMIT} курсів (course_id) з найбільшою кількістю записів; "
            "порядок — за кількістю записів. Рахуються записи, а не унікальні студенти. "
            "Частка сертифікованих записів не є рейтингом успішності курсу: "
            "на меншій кількості записів вона менш надійна."
        ),
    }


def fetch_data_quality_notes() -> dict:
    row = _fetch_all(QUALITY_SQL, {"certified": CERTIFIED_STATE})[0]
    return {
        "progress_above_100": int(row["progress_above_100"]),
        "progress_100_plus_not_certified": int(row["progress_100_plus_not_certified"]),
        "completed_at_filled": int(row["completed_at_filled"]),
        "completed_at_filled_not_certified": int(row["completed_at_filled_not_certified"]),
        "note": (
            "Через ці невідповідності progress_pct і completed_at не використовуються "
            "як критерій сертифікації; використовується funnel_state."
        ),
    }

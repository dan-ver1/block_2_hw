"""Агент на Gemini, що пояснює вже обчислені показники сертифікації.

Модель не має доступу до SQL: вона може лише викликати заздалегідь визначені
аналітичні інструменти без параметрів. Числа рахує код у db.py.
"""

import os
from dataclasses import dataclass
from typing import Callable

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_QUESTION_CHARS = 500
MAX_TOOL_CALLS = 6

# SDK за замовчуванням не повторює запити; вмикаємо його власні обмежені повтори
# лише для тимчасових помилок сервера. 429 (ліміт) не повторюємо.
TRANSIENT_STATUS_CODES = [408, 500, 502, 503, 504]
HTTP_OPTIONS = types.HttpOptions(
    timeout=60_000,  # мс на один HTTP-запит
    retry_options=types.HttpRetryOptions(
        attempts=3,  # разом із першою спробою
        initial_delay=1.0,
        max_delay=8.0,
        http_status_codes=TRANSIENT_STATUS_CODES,
    ),
)

SYSTEM_INSTRUCTION = """\
Ти аналітик даних навчальної платформи. Відповідай українською мовою, коротко і зрозуміло.

Правила:
- Використовуй лише числа, які повернули інструменти. Не вигадуй і не оцінюй відсутні дані.
- Перед відповіддю виклич інструменти, потрібні для запитання.
- Сертифікований запис — це funnel_state = 'certified'. Це робоче визначення;
  бізнес-визначення завершеного навчання не підтверджене. Згадуй це, коли йдеться про завершення.
- Тривалість навчання за датами не розраховується; якщо про неї питають, скажи, що такого показника немає.
- Не називай курси найуспішнішими лише через високу частку сертифікованих записів;
  зважай на кількість записів курсу.
- Чітко відокремлюй факти з даних від припущень.
- Якщо запитання виходить за межі доступних інструментів, прямо скажи, яких даних бракує.
- Ти не можеш змінювати дані чи виконувати SQL; на такі прохання відповідай відмовою.
"""


class AgentConfigError(RuntimeError):
    """У .env бракує ключа Gemini."""


class AgentServiceError(RuntimeError):
    """Помилка звернення до Gemini, зведена до безпечної категорії.

    kind: unavailable | rate_limit | access | model | other
    """

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return "unavailable"
    if not isinstance(exc, errors.APIError):
        return "other"
    if exc.code in TRANSIENT_STATUS_CODES:
        return "unavailable"
    if exc.code == 429:
        return "rate_limit"
    if exc.code in (401, 403) or (
        exc.code == 400 and "API_KEY" in f"{exc.status} {exc.details}".upper()
    ):
        return "access"
    if exc.code == 404:
        return "model"
    return "other"


@dataclass
class AgentAnswer:
    text: str
    tools_used: list[str]


class CertificationAgent:
    def __init__(
        self,
        kpis_fn: Callable[[], dict],
        funnel_fn: Callable[[], dict],
        quality_fn: Callable[[], dict],
        top_courses_fn: Callable[[], dict],
    ):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise AgentConfigError("У .env не заповнено: GEMINI_API_KEY")
        self._client = genai.Client(api_key=api_key, http_options=HTTP_OPTIONS)
        self.model = os.getenv("GEMINI_MODEL") or DEFAULT_MODEL
        self._kpis_fn = kpis_fn
        self._funnel_fn = funnel_fn
        self._quality_fn = quality_fn
        self._top_courses_fn = top_courses_fn

    def _build_tools(self, used: list[str]) -> list[Callable[[], dict]]:
        def get_certification_kpis() -> dict:
            """Повертає кількість записів на курси, кількість сертифікованих записів
            (funnel_state = 'certified') і частку сертифікації у відсотках."""
            used.append("get_certification_kpis")
            return self._kpis_fn()

        def get_funnel_state_distribution() -> dict:
            """Повертає кількість і частку записів на курси для кожного значення funnel_state."""
            used.append("get_funnel_state_distribution")
            return self._funnel_fn()

        def get_data_quality_notes() -> dict:
            """Повертає лічильники невідповідностей у progress_pct і completed_at,
            які пояснюють, чому сертифікацію визначено через funnel_state."""
            used.append("get_data_quality_notes")
            return self._quality_fn()

        def get_top_courses_by_enrollments() -> dict:
            """Повертає до 5 курсів (course_id) з найбільшою кількістю записів: назву курсу,
            спеціалізацію, кількість записів, кількість сертифікованих записів
            (funnel_state = 'certified') і їхню частку. Порядок — за кількістю записів."""
            used.append("get_top_courses_by_enrollments")
            return self._top_courses_fn()

        return [
            get_certification_kpis,
            get_funnel_state_distribution,
            get_data_quality_notes,
            get_top_courses_by_enrollments,
        ]

    def ask(self, question: str) -> AgentAnswer:
        question = question.strip()
        if not question:
            raise ValueError("Запитання порожнє.")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(f"Запитання довше за {MAX_QUESTION_CHARS} символів.")

        used: list[str] = []
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=self._build_tools(used),
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_CALLS
            ),
        )
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=question, config=config
            )
        except (errors.APIError, httpx.TimeoutException, httpx.ConnectError) as exc:
            raise AgentServiceError(_classify_error(exc)) from exc
        text = (response.text or "").strip() or "Модель не повернула текстової відповіді."
        return AgentAnswer(text=text, tools_used=used)

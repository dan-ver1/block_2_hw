"""Streamlit-застосунок: записи на курси та сертифікація."""

import altair as alt
import pandas as pd
import streamlit as st

import db
from agent import MAX_QUESTION_CHARS, AgentConfigError, AgentServiceError, CertificationAgent

CACHE_TTL_S = 600

# Кольори діаграми узгоджені з .streamlit/config.toml.
TEXT_COLOR = "#1F2933"
PRIMARY_COLOR = "#1D4F91"  # сертифіковані записи; білий текст на ньому 8.1:1
NEUTRAL_COLOR = "#8795A8"  # інші статуси; 3:1 до фону, темний текст на ньому 4.8:1
# Підпис значення ставиться всередину стовпчика, якщо він не коротший за половину найбільшого;
# інакше — праворуч від стовпчика. Так підписи вміщуються і на вузькому екрані.
INSIDE_LABEL_MIN_RATIO = 0.5
BAR_SIZE = 45  # товщина стовпчика, px
ROW_HEIGHT = 70  # висота рядка: стовпчик + проміжок ~25 px

# Українські підписи для значень funnel_state; значення в БД не змінюються.
STATE_LABELS = {
    "viewed": "Переглянуто",
    "registered": "Зареєстровано",
    "explored": "Досліджено",
    "certified": "Сертифіковано",
}

# (рівень повідомлення, текст) для категорій помилок Gemini.
AGENT_ERROR_MESSAGES = {
    "unavailable": (
        "warning",
        "Сервіс Gemini тимчасово недоступний або перевантажений. "
        "Кілька повторних спроб не допомогли — спробуйте ще раз за хвилину.",
    ),
    "rate_limit": (
        "error",
        "Вичерпано ліміт запитів до Gemini для цього ключа. "
        "Зачекайте або перевірте квоту в Google AI Studio.",
    ),
    "access": (
        "error",
        "Gemini відхилив доступ. Перевірте GEMINI_API_KEY у файлі `.env`.",
    ),
    "model": (
        "error",
        "Модель Gemini недоступна для цього ключа. Перевірте GEMINI_MODEL у файлі `.env`.",
    ),
    "other": (
        "error",
        "Gemini не зміг обробити запитання. Спробуйте переформулювати його або повторіть пізніше.",
    ),
}

st.set_page_config(page_title="Аналітика записів на курси", layout="centered")

# Доповнення до теми для контрасту (WCAG): межа поля введення ≥3:1 до фону
# (у фокусі лишається межа основного кольору), підписи й плейсхолдер ≥4.5:1.
st.html(
    """<style>
    [data-testid='stTextAreaRootElement']:not(:focus-within) { border-color: #848F9D !important; }
    textarea::placeholder { color: #5A6675 !important; opacity: 1; }
    [data-testid='stCaptionContainer'] { opacity: 0.8 !important; }
    /* Вузький екран: компактніші комірки, щоб таблиця курсів вміщалася без прокручування. */
    @media (max-width: 480px) {
      [data-testid='stTable'] th, [data-testid='stTable'] td {
        padding-left: 0.375rem !important; padding-right: 0.375rem !important;
        font-size: 0.8125rem !important;
      }
    }
    </style>"""
)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_kpis() -> dict:
    return db.fetch_certification_kpis()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_funnel() -> dict:
    return db.fetch_funnel_distribution()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_quality() -> dict:
    return db.fetch_data_quality_notes()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_top_courses() -> dict:
    return db.fetch_top_courses()


@st.cache_resource(show_spinner=False)
def get_agent() -> CertificationAgent:
    return CertificationAgent(
        kpis_fn=load_kpis,
        funnel_fn=load_funnel,
        quality_fn=load_quality,
        top_courses_fn=load_top_courses,
    )


def fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def fmt_pct(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + "%"


def status_chart(states: list[dict]) -> alt.LayerChart:
    df = pd.DataFrame(states)
    max_count = int(df["enrollments"].max())
    df["label"] = df["funnel_state"].map(STATE_LABELS).fillna(df["funnel_state"])
    df["count_text"] = df["enrollments"].map(fmt_int)
    df["share_text"] = df["share_pct"].map(fmt_pct)
    df["value_text"] = df["count_text"] + " · " + df["share_text"]
    df["is_certified"] = df["funnel_state"] == db.CERTIFIED_STATE
    df["inside"] = df["enrollments"] >= INSIDE_LABEL_MIN_RATIO * max_count
    order = df.sort_values("enrollments", ascending=False)["label"].tolist()

    base = alt.Chart(df).encode(
        y=alt.Y(
            "label:N",
            sort=order,  # явний порядок: однаковий для всіх шарів діаграми
            title=None,
            axis=alt.Axis(
                labelAngle=0, labelLimit=220, labelFontSize=14, labelColor=TEXT_COLOR,
                labelPadding=10, ticks=False, domain=False,
            ),
        ),
        # Значення підписані біля стовпчиків, тому окрема числова вісь не потрібна.
        x=alt.X("enrollments:Q", axis=None, scale=alt.Scale(domain=[0, max_count], nice=False)),
        tooltip=[
            alt.Tooltip("label:N", title="Статус"),
            alt.Tooltip("count_text:N", title="Записів"),
            alt.Tooltip("share_text:N", title="Частка"),
        ],
    )
    bars = base.mark_bar(size=BAR_SIZE, cornerRadiusEnd=3).encode(
        color=alt.condition(alt.datum.is_certified, alt.value(PRIMARY_COLOR), alt.value(NEUTRAL_COLOR))
    )
    text_style = {"fontSize": 14, "fontWeight": 600, "baseline": "middle"}
    inside = (
        base.transform_filter("datum.inside")
        .mark_text(align="right", dx=-10, **text_style)
        .encode(
            text="value_text:N",
            color=alt.condition(alt.datum.is_certified, alt.value("#FFFFFF"), alt.value(TEXT_COLOR)),
        )
    )
    outside = (
        base.transform_filter("!datum.inside")
        .mark_text(align="left", dx=8, color=TEXT_COLOR, **text_style)
        .encode(text="value_text:N")
    )
    description = "; ".join(f"{r.label}: {r.count_text} ({r.share_text})" for r in df.itertuples())
    return (
        (bars + inside + outside)
        .properties(height=ROW_HEIGHT * len(df), description=f"Записи за статусом. {description}")
        .configure_view(stroke=None)
    )


CERTIFIED_COLUMN = "Серти­фіковані"  # м'який перенос: заголовок вміщується на вузькому екрані


def top_courses_table(courses: list[dict]):
    # Числа лишаються числовими (st.table вирівнює їх праворуч), а відображаються у форматі KPI.
    df = pd.DataFrame(
        {
            "Курс": [c["course_name"] for c in courses],
            "Спеціалізація": [c["specialization_name"] for c in courses],
            "Записи": [c["enrollments"] for c in courses],
            CERTIFIED_COLUMN: [c["certified_enrollments"] for c in courses],
            "Частка": [c["certified_share_pct"] for c in courses],
        }
    )
    return df.style.hide(axis="index").format(
        {"Записи": fmt_int, CERTIFIED_COLUMN: fmt_int, "Частка": fmt_pct}
    )


st.title("Аналітика записів на курси")
st.caption("Статуси записів і частка сертифікованих у навчальній базі")

missing = db.missing_db_vars()
if missing:
    st.error("У файлі `.env` не заповнено змінні для бази: " + ", ".join(missing))
    st.stop()

try:
    with st.spinner("Завантаження даних з бази…"):
        kpis = load_kpis()
        funnel = load_funnel()
except Exception as exc:  # деталі підключення не показуємо
    st.error(
        f"Не вдалося отримати дані з бази ({type(exc).__name__}). "
        "Перевірте змінні PG* у `.env` і доступ до мережі."
    )
    st.stop()

col1, col2, col3 = st.columns(3)
col1.metric("Кількість записів", fmt_int(kpis["total_enrollments"]), border=True)
col2.metric("Сертифіковані записи", fmt_int(kpis["certified_enrollments"]), border=True)
col3.metric("Частка сертифікованих", fmt_pct(kpis["certification_rate_pct"]), border=True)
st.caption(
    "Рахуються записи на курси, а не унікальні користувачі. "
    "Сертифіковані записи — записи зі статусом **certified** у БД."
)

st.subheader("Розподіл записів за статусом")
st.altair_chart(status_chart(funnel["states"]), width="stretch")

st.markdown(f"**Курси з найбільшою кількістю записів (топ-{db.TOP_COURSES_LIMIT})**")
try:
    top_courses = load_top_courses()
except Exception:  # збій цього запиту не повинен ховати решту сторінки
    st.warning("Не вдалося завантажити список курсів. Спробуйте оновити сторінку пізніше.")
else:
    st.table(top_courses_table(top_courses["courses"]))
    st.caption("Порядок — за кількістю записів. Частка сертифікованих записів не є рейтингом курсів.")

with st.expander("Як рахуються показники"):
    st.markdown(
        f"""
- **Кількість записів** — усі рядки таблиці `enrollments`. Кожен рядок — запис на окремий курс,
  тому це не кількість унікальних людей.
- **Сертифіковані записи** — рядки зі статусом `funnel_state = '{db.CERTIFIED_STATE}'`.
  Це робоче визначення: **бізнес-визначення завершеного навчання не підтверджене**.
- **Частка сертифікованих** — сертифіковані записи / кількість записів × 100%.
- **Розподіл за статусом** — кількість записів для кожного значення `funnel_state`; частка — від усіх записів.
  Статуси впорядковано за кількістю записів. Послідовність етапів між ними в даних не описана,
  тому діаграма не є воронкою.
- **Курси з найбільшою кількістю записів** — до {db.TOP_COURSES_LIMIT} курсів, згрупованих за `course_id`
  (курс у межах спеціалізації; однакова назва в різних спеціалізаціях — різні курси).
  Для кожного: записи, сертифіковані записи (`certified`) і їхня частка від записів курсу.
  Порядок — за кількістю записів; частка не є рейтингом, бо на меншій кількості записів вона менш надійна.
- `progress_pct` і `completed_at` не використовуються як критерій сертифікації: у даних є значення
  прогресу понад 100% та заповнені дати завершення в записах без сертифіката.
- Тривалість навчання за датами не розраховується.

| Підпис | Значення в БД |
|---|---|
"""
        + "\n".join(f"| {label} | `{state}` |" for state, label in STATE_LABELS.items())
    )

st.subheader("Запитати про дані")
with st.form("question_form"):
    question = st.text_area(
        "Ваше запитання",
        max_chars=MAX_QUESTION_CHARS,
        height="content",
        placeholder="Наприклад: яка частка записів отримала сертифікат і що це означає?",
    )
    submitted = st.form_submit_button("Запитати", type="primary")

if submitted:
    if not question.strip():
        st.warning("Введіть запитання.")
    else:
        try:
            agent = get_agent()
            with st.spinner("Агент готує відповідь…"):
                answer = agent.ask(question)
        except AgentConfigError as exc:
            st.error(str(exc))
        except ValueError as exc:
            st.warning(str(exc))
        except AgentServiceError as exc:
            level, message = AGENT_ERROR_MESSAGES.get(exc.kind, AGENT_ERROR_MESSAGES["other"])
            getattr(st, level)(message)
        except Exception:  # напр., помилка бази під час виклику інструмента
            st.error("Агент не зміг відповісти через внутрішню помилку. Спробуйте ще раз пізніше.")
        else:
            with st.container(border=True):
                st.markdown("**Відповідь**")
                st.markdown(answer.text)
                with st.expander("Технічні деталі"):
                    tools = ", ".join(dict.fromkeys(answer.tools_used)) or "жодного"
                    st.caption(f"Використані інструменти: {tools} · модель: {agent.model}")

"""
=====================================================================
AI SOURCING AGENT - SKELAR (MVP)
=====================================================================
Що це таке (для нетехнічної людини):
Це веб-додаток на Streamlit, який автоматично шукає кандидатів на
LinkedIn через Apify, перевіряє їх штучним інтелектом (Claude) за
твоїми жорсткими критеріями (локація, стоп-лист компаній, релевантний
досвід) і крутиться по колу ("agentic loop"), поки не назбирає рівно
стільки ідеальних (A-tier) кандидатів, скільки ти вказала - або поки
не вичерпає ліміт спроб (safety limit), щоб не зациклитись назавжди.

ЯК ЗАПУСТИТИ ЛОКАЛЬНО (для перевірки перед деплоєм):
1. Встанови Python 3.10+ (якщо ще нема): https://www.python.org/downloads/
2. Відкрий термінал у папці з цим файлом і виконай:
   pip install -r requirements.txt
3. Запусти додаток:
   streamlit run app.py
4. У браузері відкриється сторінка. Введи свої API-ключі в лівій панелі.

ЯК ЗАДЕПЛОЇТИ НА STREAMLIT COMMUNITY CLOUD (безкоштовний хостинг):
1. Заведи GitHub-репозиторій і заливай туди app.py + requirements.txt.
2. Зайди на https://share.streamlit.io , натисни "New app".
3. Вибери свій репозиторій, файл app.py, гілку main.
4. Натисни Deploy. Ключі API вводяться прямо у формі додатку
   (нікуди більше вставляти не треба).

ЩО ПОКИ НЕ ПІДКЛЮЧЕНО (навмисно, за домовленістю):
Ashby-перевірка (Agent 2) зараз працює як "заглушка" (stub): якщо
Ashby API key не введений - кандидат просто позначається як
"Не перевірено в ATS" і йде далі по циклу. Коли отримаєш Ashby Admin
API key - шукай коментар "TODO ASHBY" нижче по коду, там точно
вказано, що і як дописати.
=====================================================================
"""

import streamlit as st
import json
import re
import time
import io
import csv
import math
import hashlib
import itertools
import concurrent.futures

import anthropic
from apify_client import ApifyClient

# ---------------------------------------------------------------------------
# КОНСТАНТИ, ЯКІ МОЖНА МІНЯТИ
# ---------------------------------------------------------------------------

# У ТЗ просили "Claude 3.5 Sonnet" - але це вже стара версія моделі.
# Тут стоїть найновіший Sonnet. Якщо Anthropic випустить нову модель,
# просто заміни рядок нижче - решта коду міняти не треба.
CLAUDE_MODEL = "claude-sonnet-5"

# Актор Apify, який шукає профілі LinkedIn (той, що вказаний у ТЗ)
APIFY_ACTOR = "harvestapi/linkedin-profile-search"

# Офіційний актор Apify для Google-пошуку (SERP) - для X-ray каналу
GOOGLE_SEARCH_ACTOR = "apify/google-search-scraper"

# Актор, який дотягує повні дані LinkedIn-профілю за URL (для профілів,
# знайдених через Google X-ray, а не через прямий LinkedIn-пошук)
LINKEDIN_PROFILE_BY_URL_ACTOR = "harvestapi/linkedin-profile-scraper"

# Скільки сторінок Google-видачі сканувати за одну ітерацію (1 сторінка ≈ 10 посилань).
# Свідомо невелике число для MVP - більше коштуватиме дорожче й довше.
GOOGLE_XRAY_MAX_PAGES = 1

# Скільки разів Strategist може сам "полагодити" і повторити запит до Apify
# ВСЕРЕДИНІ однієї ітерації, якщо перша спроба дала помилку або 0 профілів
# (замість того щоб сліпо чекати наступної ітерації з тим самим браком).
MAX_APIFY_ATTEMPTS_PER_ITERATION = 3

# Скільки кандидатів перевіряти ОДНОЧАСНО (ATS -> Evaluator -> Critic) через
# ThreadPoolExecutor замість по одному. Свідомо невелике число (не 5+) - це
# напряму множить кількість одночасних запитів до Claude API, а в нас уже
# були симптоми, схожі на rate limit одного акаунту.
MAX_CANDIDATE_WORKERS = 3

# Локації, які завжди виключаємо з пошуку (додатковий захист "ніколи не Росія"
# ще на рівні самого пошуку в Apify, до того як профіль взагалі побачить AI)
HARD_EXCLUDE_LOCATIONS = ["Russia"]

# ВАЖЛИВО: LinkedIn (а через нього і Apify-актор) розуміє в полі "locations"
# ТІЛЬКИ реальні географічні назви (країна/місто), які є в автопідказках
# LinkedIn-пошуку. Значення на кшталт "Remote" не розпізнаються і актор
# падає з 404 на ВСЮ сторінку результатів (перевірено на реальному запуску:
# "Input location 'Remote' is not recognized by Linkedin"). Тому такі
# значення відсіюємо ще до відправки в Apify, незалежно від того, що
# згенерував Strategist.
INVALID_LOCATION_TOKENS = {
    "remote", "remote-friendly", "remote friendly", "anywhere", "worldwide",
    "global", "hybrid", "будь-де", "віддалено", "дистанційно", "не має значення",
}


def clean_locations(locations: list) -> list:
    """Прибирає значення, які LinkedIn не розпізнає як реальну локацію
    (напр. 'Remote') - інакше весь запуск актора падає на 404."""
    return [
        loc for loc in (locations or [])
        if loc and loc.strip().lower() not in INVALID_LOCATION_TOKENS
    ]

# Скільки мілісекунд чекати між LLM-викликами, щоб не впертися в rate limit
LLM_CALL_DELAY_SEC = 0.4

# Постійна пам'ять про вирішені проблеми зберігається в Apify Key-Value Store
# (той самий акаунт/токен, що вже й так введений для пошуку - без жодного
# додаткового сервісу чи ключа). Це те, що дозволяє агенту "самому вчитись"
# між сесіями, а не через ручне копіювання тексту рекрутеркою.
KNOWN_ISSUES_STORE_NAME = "skelar-sourcing-known-issues"
KNOWN_ISSUES_RECORD_KEY = "known_issues"

# Пам'ять фідбеку рекрутера (кого і чому відхилили вручну на фінальному
# екрані) - у ТОМУ Ж сторі, що й known_issues, під окремим ключем. Свідомо
# СПІЛЬНА на всі вакансії (не по одній, як seen_urls): "часто змінює
# роботу" чи "слабкий стек" - це загальні смаки рекрутерки, релевантні й
# для інших ролей, а не технічний факт про одну вакансію.
RECRUITER_FEEDBACK_RECORD_KEY = "recruiter_feedback"

# Скільки НАЙНОВІШИХ прикладів фідбеку передавати в промпт Evaluator/
# Strategist - без ліміту цей список ріс би без кінця і роздував би вартість
# кожного виклику Claude. Найновіші приклади зазвичай найрелевантніші.
MAX_FEEDBACK_ENTRIES_IN_PROMPT = 20

# Скільки НАЙНОВІШИХ записів фактично ЗБЕРІГАТИ в Apify Key-Value Store -
# окремо від ліміту вище, який обрізає лише те, що йде в промпт Claude.
# БЕЗ цього ліміту сам список у сховищі ріс би без кінця (роками, для всієї
# команди), навіть якщо в промпт завжди йдуть тільки останні 20 - а Apify
# Key-Value Store не безрозмірний і не безкоштовний. Значно більше за
# MAX_*_IN_PROMPT, щоб не губити реальну історію - це запобіжник від
# необмеженого росту, а не робоче обмеження "скільки уроків тримати".
MAX_KNOWN_ISSUES_STORED = 200
MAX_FEEDBACK_ENTRIES_STORED = 500

# Готові категорії причин відхилення на екрані результатів (плюс "Інше" з
# вільним текстом) - структурований, але не жорсткий список.
REJECTION_REASON_OPTIONS = ["", "Замало B2B досвіду", "Часто змінює роботу", "Слабкий стек", "Інше"]

# Пам'ять про вже перевірених кандидатів (seen_urls) - теж в Apify Key-Value
# Store, але СВІДОМО в ОКРЕМОМУ сторі і з ключем, прив'язаним до конкретної
# вакансії (не спільним на всі пошуки, як known_issues). Інакше кандидат,
# якого система вже бачила для "Product Analyst", ніколи більше не з'явився
# б навіть у геть іншому пошуку, напр. "Marketing Manager" через рік.
SEEN_URLS_STORE_NAME = "skelar-sourcing-seen-urls"

# Імена "команди" - косметика для того, щоб у логу було видно не один суцільний
# "чорний ящик", а 5 ролей, які працюють по черзі і перевіряють одна одну. Це
# НЕ окремі AI-моделі чи персонажі з власною пам'яттю - це той самий Claude,
# просто з різними системними промптами і різними задачами. Ім'я тут - лише
# підпис у логу, не нова технічна сутність.
TEAM = {
    "strategist": "🧭 Стратег",
    "pm": "🛠️ Механік",
    "ats": "🗂️ Ашбі",
    "evaluator": "⚖️ Суддя",
    "critic": "🔍 Скептик",
}


# ---------------------------------------------------------------------------
# ДОПОМІЖНІ ФУНКЦІЇ: ВИТЯГТИ ДАНІ З ПРОФІЛЮ (структура відповіді Apify-актора
# може відрізнятись, тому скрізь є запасні варіанти назв полів)
# ---------------------------------------------------------------------------

def get_profile_name(p: dict) -> str:
    if p.get("fullName"):
        return p["fullName"]
    if p.get("name"):
        return p["name"]
    first = p.get("firstName", "")
    last = p.get("lastName", "")
    if first or last:
        return f"{first} {last}".strip()
    return "Ім'я не знайдено"


def get_profile_url(p: dict) -> str:
    for key in ("linkedinUrl", "profileUrl", "url"):
        if p.get(key):
            return p[key]
    if p.get("publicIdentifier"):
        return f"https://www.linkedin.com/in/{p['publicIdentifier']}"
    return ""


def get_profile_headline(p: dict) -> str:
    if p.get("headline"):
        return p["headline"]
    cp = p.get("currentPosition")
    if isinstance(cp, dict):
        return cp.get("title", "")
    if isinstance(cp, list) and cp:
        return cp[0].get("title", "")
    return "н/д"


def extract_role_label(job_description: str) -> str:
    """У додатку немає окремого структурованого поля "назва вакансії" - опис
    вставляється одним вільним текстом. Для фідбеку рекрутера беремо перший
    непорожній рядок опису як приблизну назву ролі (зазвичай там і є тайтл)."""
    for line in (job_description or "").strip().splitlines():
        if line.strip():
            return line.strip()[:100]
    return "Вакансія без назви"


def profile_to_compact_text(p: dict, max_chars: int = 6000) -> str:
    """Перетворює сирий JSON профілю в текст для передачі в Claude. Обрізаємо,
    щоб не роздувати вартість виклику на дуже великих профілях.

    БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: раніше обрізали ГОТОВИЙ JSON-текст наосліп по
    кількості символів (`text[:max_chars]`), а не за структурою. Чим більше
    досвіду в кандидата (більше записів experience), тим вища ймовірність,
    що обрізка впаде ПОСЕРЕДИНІ запису чи навіть посеред лапок/дужок -
    Critic сам це виявив на практиці ("Дані обрізані, JSON обривається на
    '1+1'..."). Це систематично шкодило саме досвідченим Senior/Lead
    кандидатам (у них найбільше experience-записів), яких найчастіше й
    шукають. Тепер, якщо повний JSON не влазить у ліміт, прибираємо
    НАЙСТАРІШІ записи experience/education по одному (вони йдуть останніми
    в масиві - найновіші, включно з поточним місцем роботи, завжди
    лишаються цілими), поки текст не влізе - і щоразу повертаємо ПОВНИЙ,
    валідний JSON, а не рядок, обірваний посередині."""
    try:
        text = json.dumps(p, ensure_ascii=False)
    except Exception:
        return str(p)[:max_chars]
    if len(text) <= max_chars:
        return text

    trimmed = dict(p)
    for list_field in ("experience", "education"):
        items = trimmed.get(list_field)
        if not isinstance(items, list) or not items:
            continue
        while items and len(json.dumps(trimmed, ensure_ascii=False)) > max_chars:
            items.pop()  # найстаріший запис - в кінці списку
        if not items:
            trimmed.pop(list_field, None)
    text = json.dumps(trimmed, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    # Крайній випадок - профіль завеликий навіть без experience/education
    # (напр. величезне поле "about"). Чесно позначаємо це явним
    # попередженням у тексті, а не мовчки обриваємо JSON без жодної
    # позначки, як було раніше.
    warning = "[УВАГА: профіль обрізано, дані можуть бути неповними] "
    return warning + text[: max_chars - len(warning)]


# ---------------------------------------------------------------------------
# ДОПОМІЖНА ФУНКЦІЯ: ВИКЛИК CLAUDE З ГАРАНТОВАНО ЧИСТИМ JSON (TOOL USE)
# ---------------------------------------------------------------------------
# Раніше тут був регулярний вираз r"\{.*\}" (жадібний, з DOTALL), який шукав
# перший "{" і останній "}" у вільному тексті відповіді. Це нестабільно: якщо
# Claude випадково згадає фігурні дужки де завгодно в поясненні - regex хапає
# не той фрагмент, json.loads падає, і агент іде у fallback-режим (саме це,
# ймовірно, і стало причиною одного з випадків "Стратег не зміг згенерувати
# запит"). Замість цього примушуємо Claude відповісти СТРОГО викликом одного
# інструмента (tool_choice) - Anthropic API сам гарантує, що result.input
# буде валідним JSON-об'єктом за нашою схемою, без жодного парсингу тексту.

def ask_claude_tool(client: anthropic.Anthropic, system_prompt: str, user_prompt: str, tool_schema: dict, retries: int = 2) -> dict:
    """Як і раніше - до `retries` повторних спроб з паузою, що зростає
    (1с, 2с, 4с...), бо найчастіша причина одноразового збою це transient
    помилка (rate limit 429, короткий мережевий збій)."""
    last_error = None
    tool_name = tool_schema["name"]
    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool_name},
            )
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                    return dict(block.input)
            last_error = "Claude не викликав очікуваний tool_use блок"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1с, 2с, 4с...
        finally:
            time.sleep(LLM_CALL_DELAY_SEC)
    return {"error": last_error}


# ---------------------------------------------------------------------------
# AGENT 1a: STRATEGIST - БРИФІНГ ПЕРЕД СТАРТОМ (як реальний рекрутер-стратег,
# перш ніж будувати запит, дивиться - чи достатньо йому інформації, чи є
# сенс перепитати щось конкретне, а не мовчки здогадуватись)
# ---------------------------------------------------------------------------

BRIEF_TOOL_SCHEMA = {
    "name": "submit_brief_result",
    "description": "Результат брифінгу: чи вакансія достатньо описана, чи є уточнюючі питання",
    "input_schema": {
        "type": "object",
        "properties": {
            "ready": {"type": "boolean", "description": "true, якщо опису вакансії достатньо для пошуку"},
            "questions": {"type": "array", "items": {"type": "string"}, "description": "0-2 уточнюючих питання"},
        },
        "required": ["ready", "questions"],
    },
}


def agent_strategist_brief(client, job_description: str) -> dict:
    system_prompt = """Ти - Стратег, перша людина в команді сорсингу, з якою спілкується
рекрутер. Перш ніж будувати пошуковий запит, подивись на опис вакансії і
виріши: чи є в ньому все необхідне (позиція/тайтл, рівень досвіду/грейд,
ключові навички, бажана локація), чи щось критично незрозуміло.

ОБОВ'ЯЗКОВА ПЕРЕВІРКА - географічний скоуп (перенесено з іншого нашого
інструменту для побудови стратегій пошуку, там це завжди перевіряють
окремо, і це справді часто губиться в описах вакансій): чи зрозуміло з
опису, це пошук ЛИШЕ в Україні, Україна + конкретні ринки релокейту, чи
взагалі без обмежень по країні? Якщо це ніяк не випливає з опису і не
очевидно з контексту - постав про це окреме уточнююче питання, навіть
якщо решта вакансії описана добре. Неправильний здогад тут напряму
зіпсує весь пошук (або занадто вузький, або з кандидатами, яких
рекрутерка взагалі не розглядає).

Якщо опису вакансії ЦІЛКОМ достатньо для якісного пошуку (включно з
географічним скоупом вище) - постав ready=true і не став жодних питань.
Не чіпляйся до дрібниць, які й так можна вивести з контексту - питай
тільки про те, без чого пошук реально пiде в неправильному напрямку.

Якщо чогось критично бракує - постав РІВНО 1-2 короткі, конкретні
уточнюючі питання рекрутеру (не більше 2). Виклич інструмент
submit_brief_result з результатом."""
    user_prompt = f"Опис вакансії:\n{job_description}"
    result = ask_claude_tool(client, system_prompt, user_prompt, BRIEF_TOOL_SCHEMA)
    if "error" in result:
        # якщо брифінг сам не спрацював - не блокуємо рекрутерку, просто
        # пропускаємо крок і одразу йдемо на пошук з тим, що є
        return {"ready": True, "questions": []}
    return result


# ---------------------------------------------------------------------------
# AGENT 1: STRATEGIST - будує / перебудовує параметри пошуку для Apify
# ---------------------------------------------------------------------------

STRATEGIST_TOOL_SCHEMA = {
    "name": "submit_search_params",
    "description": "Параметри пошуку для Apify LinkedIn actor",
    "input_schema": {
        "type": "object",
        "properties": {
            "searchQuery": {"type": "string", "description": "усі альтернативні тайтли ОДНИМ рядком через OR"},
            "currentJobTitles": {"type": "array", "items": {"type": "string"}, "description": "залишай порожнім, якщо не жорсткий фільтр"},
            "locations": {"type": "array", "items": {"type": "string"}, "description": "тільки реальні географічні назви"},
            "yearsOfExperienceIds": {
                "type": "array", "items": {"type": "string", "enum": ["1", "2", "3", "4", "5"]},
                "description": "нативний LinkedIn-фільтр досвіду за таблицею грейдів з системного промпту; порожній масив, якщо грейд у вакансії не зрозумілий чи навмисно широкий",
            },
            "reasoning": {"type": "string", "description": "1-2 речення чому саме такий запит цього разу"},
        },
        "required": ["searchQuery", "currentJobTitles", "locations", "yearsOfExperienceIds", "reasoning"],
    },
}


def agent_strategist(client, job_description: str, search_history: list, still_needed: int, recruiter_note: str = "", known_issues: list = None, recruiter_feedback: list = None) -> dict:
    system_prompt = """Ти - Strategist, агент з побудови пошукових запитів для LinkedIn
через Apify-актор harvestapi/linkedin-profile-search.

Якщо тобі дали "Відомі проблеми і рішення" (уроки з попередніх сесій або
попередніх ітерацій цього ж пошуку) - це готові уроки, зароблені або тобою,
або рекрутером раніше. НЕ повторюй ці самі помилки: якщо там написано,
наприклад, що локація "Remote" завжди падає з 404 - просто ніколи її не
пропонуй, не чекай, поки PM-агент знову це виявить.

Якщо тобі дали "Фідбек рекрутера" (кандидати, яких рекрутерка вручну
відхилила раніше, і чому) - це реальні приклади її смаку, не гіпотези.
Подивись, чи є серед причин відхилення патерн, який можна врахувати ще на
рівні пошукового запиту (наприклад, якщо кілька відхилень через "слабкий
стек" стосуються конкретної технології - можеш уточнити тайтли/ключові
слова, щоб менше таких кандидатів взагалі потрапляло у видачу). Не
вигадуй зайвого - якщо патерну не видно, просто врахуй фідбек в оцінці
пізніше (це вже робить Evaluator), а не тут.

Твоє завдання: на основі опису вакансії згенерувати параметри пошуку.
Якщо в history вже є попередні спроби - подивись, скільки вони дали
релевантних кандидатів, і зроби НОВИЙ, ІНШИЙ запит (інші ключові слова,
ширші/вужчі тайтли, інші локації), щоб не повторювати той самий пошук.

ЯК ПРИДУМУВАТИ АЛЬТЕРНАТИВНІ ТАЙТЛИ (перенесено з іншого нашого
інструменту для побудови стратегій пошуку) - перш ніж згорнути все в один
boolean-рядок через OR, подумай про тайтли систематично, за категоріями,
а не навмання:
- прямі синоніми того самого тайтла з вакансії
- назви, типові для суміжних індустрій, де є схожі ролі
- різні конвенції найменування: як цю роль назвали б у стартапі, а як - у
  великій enterprise-компанії (це часто різні слова для тієї самої суті)
- назви, типові саме для українського ринку (можуть відрізнятись від
  прямого перекладу з англійської)
Мета - ширший, але усвідомлений funnel кандидатів, а не перший тайтл, що
спав на думку. Лише ПІСЛЯ такого перебору обʼєднай фінальний список
одним boolean-рядком через OR у searchQuery (див. правило нижче).

Якщо рекрутер дав текстову корективу для цієї ітерації (recruiter_note) -
це НАЙВИЩИЙ пріоритет: врахуй її першою, навіть якщо це суперечить твоїй
попередній стратегії. Наприклад, якщо рекрутер написав "шукай ще в Львові" -
додай Львів у locations. Якщо написав "тайтл X не підходить" - прибери
цей тайтл з переліку і не повертайся до нього.

КРИТИЧНО ВАЖЛИВО про searchQuery vs currentJobTitles (перевірено на
реальних запусках - неправильна комбінація дає 0 результатів, хоча кожен
фільтр окремо на сайті Apify працює):
Apify-актор застосовує searchQuery і currentJobTitles як AND, а не OR.
Якщо заповнити ОБИДВА одночасно (напр. searchQuery="Data Analyst" І
currentJobTitles=["Data Analyst","Product Analyst",...]) - профіль повинен
пройти ОБИДВА фільтри одночасно, і в комбінації з locations це майже
завжди дає 0, навіть якщо кандидати реально є.
Тому роби так: усі альтернативні тайтли об'єднай ОДНИМ boolean-рядком
через OR прямо в searchQuery, наприклад:
"Product Analyst" OR "Data Analyst" OR "Growth Product Analyst"
А currentJobTitles залишай ПОРОЖНІМ масивом [] за замовчуванням - не
дублюй туди ті самі тайтли. Заповнюй currentJobTitles окремо ТІЛЬКИ якщо
рекрутер явно попросив жорстко відфільтрувати по точному тайтлу (це
окремий, свідомо вужчий режим, не комбінуй з широким searchQuery в
той самий момент).

КРИТИЧНО ВАЖЛИВО про locations: пиши ТІЛЬКИ реальні географічні назви -
країни або міста (наприклад "Ukraine", "Kyiv", "Lviv", "Poland", "Warsaw").
НІКОЛИ не пиши туди "Remote", "Anywhere", "Worldwide", "Hybrid" чи будь-що
не-географічне - LinkedIn не розпізнає такі значення, і через це actor
падає з помилкою на ВЕСЬ запит (перевірено на практиці), а не просто
ігнорує це значення. Якщо вакансія дозволяє віддалену роботу - це вже
означає ширший список країн/міст у locations, а не додавання слова "Remote".

ПРО yearsOfExperienceIds (нативний фільтр LinkedIn, окремо від searchQuery) -
якщо з опису вакансії ЗРОЗУМІЛИЙ цільовий грейд, постав відповідні ID за
таблицею нижче (перевірена на практиці, перенесена з іншого нашого
сорсингового інструменту) - це фільтрує кандидатів ще на рівні LinkedIn, а
не тільки пізніше через Evaluator, тож команда не витрачає виклики Claude
на кандидатів явно не того рівня:
| Грейд вакансії | yearsOfExperienceIds |
|---|---|
| Junior (0-2 роки) | ["1","2"] |
| Middle (2-5 років) | ["2","3"] |
| Middle/Senior, 4+ років | ["3","4","5"] |
| Senior (5-8 років) | ["3","4","5"] |
| Lead/Head (8+ років) | ["4","5"] |
Якщо грейд у вакансії розмитий, широкий ("від Middle до Senior і вище") або
взагалі не вказаний - залиш yearsOfExperienceIds порожнім масивом []: краще
не звужувати штучно, ніж помилково відсіяти підходящих людей нативним
фільтром ще до того, як їх побачить Evaluator.

Виклич інструмент submit_search_params з результатом."""
    user_prompt = f"""Опис вакансії:
{job_description}

Скільки A-tier кандидатів ще потрібно знайти: {still_needed}

Текстова корректива від рекрутера саме для цієї ітерації (може бути порожньою):
{recruiter_note or "(немає)"}

Відомі проблеми і рішення з попередніх сесій (уникай їх заздалегідь, може
бути порожньо):
{json.dumps(known_issues or [], ensure_ascii=False, indent=2)}

Фідбек рекрутера - кого і чому відхилили вручну раніше (може бути порожньо):
{json.dumps((recruiter_feedback or [])[-MAX_FEEDBACK_ENTRIES_IN_PROMPT:], ensure_ascii=False, indent=2)}

Історія попередніх ітерацій цього пошуку (може бути порожньою):
{json.dumps(search_history, ensure_ascii=False, indent=2)}
"""
    result = ask_claude_tool(client, system_prompt, user_prompt, STRATEGIST_TOOL_SCHEMA)
    if "error" in result:
        # Запасний варіант, щоб цикл не падав, якщо Claude сам не відповів
        # (навіть після повторних спроб у ask_claude_tool). Зберігаємо ТЕКСТ
        # реальної помилки в "reasoning", а не ховаємо її - інакше рекрутерка
        # бачить тільки сирий, непридатний запит (перші 100 символів опису
        # вакансії як буквальний текст пошуку) і не розуміє чому.
        result = {
            "searchQuery": job_description[:100],
            "currentJobTitles": [],
            "locations": ["Ukraine"],
            "yearsOfExperienceIds": [],
            "reasoning": "fallback",
            "fallback_error": result["error"],
        }
    return result


# ---------------------------------------------------------------------------
# АГЕНТ "PM" - ДІАГНОСТИКА І ПОЛАГОДЖЕННЯ ЗАПИТУ ВСЕРЕДИНІ ІТЕРАЦІЇ
# ---------------------------------------------------------------------------
# Apify - це інструмент, а не гарантія. Якщо перша спроба дала помилку або
# 0 профілів, ми не чекаємо сліпо наступної повної ітерації (і твоєї дії на
# чекпоінті) - Claude одразу як технічний PM аналізує, що могло піти не так,
# і сам лагодить запит, до MAX_APIFY_ATTEMPTS_PER_ITERATION спроб.

PM_TOOL_SCHEMA = {
    "name": "submit_repair",
    "description": "Діагноз проблеми і виправлений пошуковий запит",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string"},
            "searchQuery": {"type": "string"},
            "currentJobTitles": {"type": "array", "items": {"type": "string"}},
            "locations": {"type": "array", "items": {"type": "string"}},
            "autoQuerySegmentation": {
                "type": "boolean",
                "description": "true, якщо варто увімкнути офіційну функцію актора 'розкласти широкий запит на сегменти' замість того щоб звужувати searchQuery чи прибирати локації",
            },
            "new_lesson": {"type": "string", "description": "'Проблема: ... → Рішення: ...' або порожній рядок, якщо це вже відомий випадок"},
        },
        "required": ["diagnosis", "searchQuery", "currentJobTitles", "locations", "autoQuerySegmentation", "new_lesson"],
    },
}


def agent_diagnose_and_repair(client, job_description: str, search_params: dict, apify_error, profiles_found: int, previous_attempts: list, known_issues: list = None) -> dict | None:
    system_prompt = """Ти - технічний PM, який відповідає за якість запитів до Apify-актора
harvestapi/linkedin-profile-search. Твоя робота - не сліпо приймати
результат Apify, а як досвідчений PM одразу аналізувати, чому запит дав
помилку або підозріло мало/нуль профілів, і лагодити запит, не чекаючи
наступної повної ітерації циклу.

Відомі, перевірені на практиці причини поганого результату:
1. Локація, яку LinkedIn не розпізнає (напр. "Remote", "Anywhere", "Hybrid")
   - актор падає з 404 на ВЕСЬ запит, не просто ігнорує значення.
2. Одночасне заповнення і searchQuery, і currentJobTitles - Apify застосовує
   їх як AND, і разом з locations це часто дає 0, хоча кожен фільтр окремо
   на сайті Apify працює нормально. Правильно: усі альтернативні тайтли
   ОДНИМ boolean-рядком через OR у searchQuery, currentJobTitles - порожній.
3. Занадто вузький/специфічний searchQuery (забагато AND, занадто конкретні
   фрази) - спробуй ширший OR-варіант.
4. Технічна помилка виклику (мережа, формат) - спробуй спростити запит.
5. Запит виглядає нормально широким, помилки нема, але профілів все одно
   підозріло мало - можливо LinkedIn обмежує видачу саме для широких
   запитів. У актора є офіційна функція саме для цього випадку -
   autoQuerySegmentation (сам розкладає широкий запит на менші сегменти за
   країною/сеньйорністю/індустрією і обходить цей ліміт). Спробуй
   УВІМКНУТИ ЇЇ (autoQuerySegmentation = true) РАНІШЕ, ніж звужувати
   searchQuery чи прибирати локації - це офіційний важіль актора для рівно
   цієї ситуації, а не костиль.

Тобі ще дають список "Відомі проблеми і рішення" - уроки, зароблені раніше
(цією ж системою в попередніх сесіях, або тобою самим раніше в цій же
сесії). Перш ніж діагностувати з нуля - перевір, чи це вже відомий випадок.
Якщо так - просто застосуй відоме рішення і в полі new_lesson залиш
порожній рядок (не дублюй урок). Якщо це НОВИЙ випадок, якого немає в
списку - постав діагноз як зазвичай, і додатково сформулюй new_lesson:
ОДИН короткий рядок у форматі "Проблема: ... → Рішення: ...", який можна
буде показати рекруторці і зберегти на майбутнє, щоб ця ж проблема більше
не виникала з нуля.

Тобі дають: параметри запиту, скільки профілів реально повернулось (0 -
підозріло, навіть без явної помилки), текст помилки (якщо була), історію
попередніх невдалих спроб ЦІЄЇ Ж ітерації (щоб не повторити ту саму
помилку двічі), і список уже відомих проблем.

Постав короткий діагноз і запропонуй ВИПРАВЛЕНИЙ запит. Виклич інструмент
submit_repair з результатом (new_lesson - тільки якщо це новий випадок,
інакше порожній рядок)."""
    user_prompt = f"""Опис вакансії:
{job_description}

Параметри запиту, що використали цього разу:
{json.dumps(search_params, ensure_ascii=False)}

Скільки профілів реально повернулось: {profiles_found}
Текст помилки (якщо була): {apify_error or "(без помилки, просто підозріло мало/нуль профілів)"}

Попередні невдалі спроби САМЕ цієї ітерації (може бути порожньо):
{json.dumps(previous_attempts, ensure_ascii=False, indent=2)}

Відомі проблеми і рішення з попередніх сесій (перевір спочатку тут, може
бути порожньо):
{json.dumps(known_issues or [], ensure_ascii=False, indent=2)}
"""
    # "error" тут НЕ повертаємо як None - інакше виклик у run_one_iteration
    # бачить тільки загальне "не зміг запропонувати виправлення" і губить
    # текст реальної причини (напр. rate limit, мережевий збій).
    return ask_claude_tool(client, system_prompt, user_prompt, PM_TOOL_SCHEMA)


# ---------------------------------------------------------------------------
# APIFY: ЗАПУСК ПОШУКУ ПРОФІЛІВ
# ---------------------------------------------------------------------------

def extract_dataset_id(run) -> str | None:
    """apify-client в різних версіях/середовищах повертає результат запуску
    актора то як звичайний dict, то як типізований обʼєкт (звідси помилка
    "'Run' object is not subscriptable", коли код жорстко очікував dict).
    Підтримуємо обидва варіанти, замість того щоб покладатись на один формат."""
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    # Обʼєктний варіант (напр. pydantic-модель чи dataclass) - пробуємо
    # типові назви атрибутів
    for attr in ("defaultDatasetId", "default_dataset_id"):
        value = getattr(run, attr, None)
        if value:
            return value
    return None


def extract_store_id(store) -> str | None:
    """ТОЙ САМИЙ БАГ, що й у extract_dataset_id вище, тільки для Key-Value
    Store: `client.key_value_stores().get_or_create(...)` в деяких версіях
    apify-client повертає не dict, а типізований об'єкт `KeyValueStore` -
    звідси помилка "'KeyValueStore' object is not subscriptable", коли код
    жорстко очікував `store["id"]`. Підтримуємо обидва варіанти."""
    if store is None:
        return None
    if isinstance(store, dict):
        return store.get("id")
    return getattr(store, "id", None)


def load_known_issues(apify_token: str) -> tuple[list, str | None]:
    """Тягне список уроків з персонального Apify Key-Value Store (той самий
    акаунт/токен, що й для пошуку). Це реальна крос-сесійна пам'ять - на
    відміну від ручного копіювання тексту, вона підвантажується сама, навіть
    в іншому браузері чи через тиждень."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=KNOWN_ISSUES_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        record = client.key_value_store(store_id).get_record(KNOWN_ISSUES_RECORD_KEY)
        value = record.get("value") if record else None
        return (value if isinstance(value, list) else []), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def save_known_issues(apify_token: str, known_issues: list) -> str | None:
    """Перезаписує повний список уроків у той самий Key-Value Store - викликається
    одразу, як тільки PM-агент знаходить новий урок (write-through), щоб нічого
    не загубилось, навіть якщо сесію закриють достроково."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=KNOWN_ISSUES_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        client.key_value_store(store_id).set_record(KNOWN_ISSUES_RECORD_KEY, known_issues, content_type="application/json")
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def load_recruiter_feedback(apify_token: str) -> tuple[list, str | None]:
    """Тягне накопичений фідбек рекрутера (кого і чому відхилили вручну) з
    того самого Key-Value Store, що й known_issues, під окремим ключем.
    Свідомо спільний на всі вакансії - "часто змінює роботу" чи "слабкий
    стек" це загальні смаки рекрутерки, не факт про одну конкретну роль."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=KNOWN_ISSUES_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        record = client.key_value_store(store_id).get_record(RECRUITER_FEEDBACK_RECORD_KEY)
        value = record.get("value") if record else None
        return (value if isinstance(value, list) else []), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def save_recruiter_feedback(apify_token: str, feedback: list) -> str | None:
    """Перезаписує повний список фідбеку - викликається одразу, коли
    рекрутерка тисне "Зберегти фідбек для навчання AI" на екрані результатів."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=KNOWN_ISSUES_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        client.key_value_store(store_id).set_record(RECRUITER_FEEDBACK_RECORD_KEY, feedback, content_type="application/json")
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def merge_and_save_known_issues(apify_token: str, local_known_issues: list, new_lesson: str = "") -> tuple[list, str | None]:
    """БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: раніше ми просто перезаписували Key-Value Store
    локальним списком (`save_known_issues(apify_token, st.session_state.known_issues)`).
    Якщо двоє рекрутерів працюють ОДНОЧАСНО з одним і тим самим (спільним)
    Apify-токеном - а саме цей сценарій ми щойно зробили можливим через
    st.secrets - хто зберігає ДРУГИМ, повністю затирає урок, щойно
    збережений першим (класичний lost update: read-modify-write без
    злиття). Тепер, перш ніж зберегти, ми ЗАНОВО читаємо store (щоб
    підхопити те, що встигла дописати паралельна сесія), зливаємо без
    дублів з локальним списком і новим уроком, і зберігаємо вже об'єднаний
    результат. Це звужує вікно гонки з "уся сесія" до "мілісекунди між
    fetch і set" - Apify Key-Value Store не підтримує атомарний append чи
    optimistic locking, тож повністю усунути гонку без переходу на іншу
    базу неможливо, але це суттєво знижує ризик."""
    latest_issues, load_error = load_known_issues(apify_token)
    if load_error:
        # не вдалось перечитати - працюємо з тим, що маємо локально, аби не
        # втратити щойно знайдений урок повністю
        latest_issues = local_known_issues
    merged = list(latest_issues)
    for issue in local_known_issues:
        if issue not in merged:
            merged.append(issue)
    new_lesson = (new_lesson or "").strip()
    if new_lesson and new_lesson not in merged:
        merged.append(new_lesson)
    # Обрізаємо до MAX_KNOWN_ISSUES_STORED НАЙНОВІШИХ - без цього список у
    # сховищі ріс би без кінця роками для всієї команди (Apify Key-Value
    # Store не безрозмірний і не безкоштовний). Найстаріші уроки - перші
    # кандидати на витіснення, найновіші зазвичай найактуальніші.
    if len(merged) > MAX_KNOWN_ISSUES_STORED:
        merged = merged[-MAX_KNOWN_ISSUES_STORED:]
    save_error = save_known_issues(apify_token, merged)
    return merged, save_error


def merge_and_save_recruiter_feedback(apify_token: str, local_feedback: list, new_entries: list = None) -> tuple[list, str | None]:
    """Той самий принцип, що і merge_and_save_known_issues вище - перечитує
    store перед записом, щоб паралельна сесія з тим самим спільним
    Apify-токеном не затерла фідбек, щойно збережений іншим рекрутером."""
    latest_feedback, load_error = load_recruiter_feedback(apify_token)
    if load_error:
        latest_feedback = local_feedback
    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: раніше фідбек просто конкатенувався без
    # перевірки дублів (на відміну від merge_and_save_known_issues вище, де
    # дедуп є). Повторний клік "Зберегти фідбек" на тих самих кандидатах,
    # або паралельна сесія з тим самим спільним Apify-токеном - і однакові
    # приклади накопичувались повторно, займаючи місце в "останніх 20
    # прикладах", які Evaluator бачить у кожному виклику.
    merged = list(latest_feedback)
    for entry in (new_entries or []):
        if entry not in merged:
            merged.append(entry)
    # Той самий запобіжник, що й у merge_and_save_known_issues - без ліміту
    # список фідбеку ріс би без кінця роками для всієї команди.
    if len(merged) > MAX_FEEDBACK_ENTRIES_STORED:
        merged = merged[-MAX_FEEDBACK_ENTRIES_STORED:]
    save_error = save_recruiter_feedback(apify_token, merged)
    return merged, save_error


def vacancy_key(job_description: str) -> str:
    """Стабільний ідентифікатор конкретної вакансії (для пам'яті seen_urls) -
    хеш від тексту опису, а не назва вакансії, бо назви можуть повторюватись,
    а хеш детерміновано прив'язаний до змісту опису.

    БАГ 1, ЯКИЙ ЦЕ ВИПРАВЛЯЄ (крихкий ключ): раніше хешувався СИРИЙ текст як
    є - зайвий пробіл, інший регістр, інший тип переносу рядка (той самий
    JD, скопійований з іншого джерела) давали ІНШИЙ хеш, і пам'ять "хто вже
    перевірений для цієї вакансії" не знаходилась, хоча по суті це та сама
    вакансія. Тепер текст нормалізується (нижній регістр, усі
    пробіли/переноси рядків згорнуті в один пробіл) перед хешуванням - це
    не рятує від ЗМІСТОВНОЇ зміни тексту (напр. рекрутер дописав щось
    суттєве чи по-іншому відповів на брифінг - це слушно вважати іншим
    пошуком), лише від випадкового шуму форматування.

    БАГ 2, ЯКИЙ ЦЕ ВИПРАВЛЯЄ (невалідний ключ): Apify Key-Value Store
    дозволяє в ключах лише символи a-zA-Z0-9!-_.'() - двокрапка НЕ входить
    у цей список. Ключ "seen_urls::hash" був невалідний ЗАВЖДИ, незалежно
    від довжини, і збереження падало на кожній ітерації (підтверджено
    логом: "Record key ... must be at most 256 characters long and only
    contain the following characters..."). "::" замінено на дозволений "-"."""
    normalized = re.sub(r"\s+", " ", job_description.strip().lower())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"seen_urls-{digest}"


def load_seen_urls(apify_token: str, job_description: str) -> tuple[set, str | None]:
    """Тягне список кандидатів, уже перевірених для ЦІЄЇ КОНКРЕТНОЇ вакансії,
    з персонального Apify Key-Value Store - щоб не скрапити й не оцінювати
    їх повторно (гроші й час) навіть якщо сесію закрили і відкрили заново."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=SEEN_URLS_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        record = client.key_value_store(store_id).get_record(vacancy_key(job_description))
        value = record.get("value") if record else None
        return (set(value) if isinstance(value, list) else set()), None
    except Exception as e:
        return set(), f"{type(e).__name__}: {e}"


def save_seen_urls(apify_token: str, job_description: str, seen_urls: set) -> str | None:
    """Перезаписує повний список перевірених URL для ЦІЄЇ вакансії
    (write-through, одразу після кожної ітерації)."""
    try:
        client = ApifyClient(apify_token)
        store = client.key_value_stores().get_or_create(name=SEEN_URLS_STORE_NAME)
        store_id = extract_store_id(store)
        if not store_id:
            raise RuntimeError(f"Не вдалось визначити ID Key-Value Store (тип відповіді: {type(store).__name__})")
        client.key_value_store(store_id).set_record(
            vacancy_key(job_description), list(seen_urls), content_type="application/json"
        )
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def run_apify_search(apify_token: str, search_params: dict, max_items: int, start_page: int = 1) -> list:
    client = ApifyClient(apify_token)
    run_input = {
        "profileScraperMode": "Full",
        "searchQuery": search_params.get("searchQuery", ""),
        "maxItems": max_items,
        "startPage": start_page,
        "excludeLocations": HARD_EXCLUDE_LOCATIONS,
    }
    original_locations = search_params.get("locations") or []
    safe_locations = clean_locations(original_locations)
    dropped = [loc for loc in original_locations if loc not in safe_locations]
    if dropped:
        st.warning(f"Прибрала нерозпізнані LinkedIn-локації, щоб актор не впав: {dropped}")
    if safe_locations:
        run_input["locations"] = safe_locations
    # НЕ дублюємо currentJobTitles разом з searchQuery - Apify застосовує їх
    # як AND, і в комбінації з locations це часто дає 0 результатів навіть
    # коли кандидати реально є (перевірено на практиці). Тайтли вже мають
    # бути об'єднані OR-ом всередині searchQuery Strategist-ом.
    if search_params.get("currentJobTitles"):
        run_input["currentJobTitles"] = search_params["currentJobTitles"]
    # Нативний LinkedIn-фільтр досвіду (перенесено з /sourcer-ai) - фільтрує
    # ще на рівні actor-а, до того як Evaluator витратить виклик Claude на
    # кандидата явно не того грейду. Порожній масив від Strategist - не
    # заповнюємо поле взагалі (не звужуємо штучно, коли грейд розмитий).
    if search_params.get("yearsOfExperienceIds"):
        run_input["yearsOfExperienceIds"] = search_params["yearsOfExperienceIds"]
    # Офіційний важіль актора для широких запитів, які дають підозріло мало
    # унікальних профілів (перенесено з /sourcer-ai) - PM-агент вмикає це
    # ЯВНО через autoQuerySegmentation=true в repaired-параметрах, перш ніж
    # звужувати searchQuery чи прибирати локації.
    if search_params.get("autoQuerySegmentation"):
        run_input["autoQuerySegmentation"] = True
        run_input["autoQuerySegmentationLevels"] = ["seniority_level", "industry", "state"]

    try:
        run = client.actor(APIFY_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId у відповіді Apify (тип відповіді: {type(run).__name__})"
        items = list(client.dataset(dataset_id).iterate_items())
        return items, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# GOOGLE X-RAY: ДОДАТКОВИЙ КАНАЛ ПОШУКУ (той самий Apify-токен, без нового ключа)
# ---------------------------------------------------------------------------
# Ідея: пряма LinkedIn-видача (актор вище) обмежена тим, що бачить сам LinkedIn
# в своєму пошуку. Google-пошук з фільтром site:linkedin.com/in іноді знаходить
# публічні профілі, яких немає у видачі LinkedIn-пошуку. Це працює як другий,
# незалежний канал у тій самій ітерації - знайдені URL йдуть у той самий
# Evaluator/Critic, що і профілі з прямого пошуку.

def build_xray_query(search_params: dict) -> str:
    """Формує простий Google X-ray запит з тих самих параметрів, що згенерував
    Strategist. Без окремого виклику Claude - це просто текстовий шаблон,
    щоб не витрачати зайві LLM-виклики на кожну ітерацію."""
    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: currentJobTitles майже завжди порожній (Strategist
    # свідомо лишає його порожнім - усі тайтли вже об'єднані одним готовим
    # boolean-рядком через OR у searchQuery). Раніше в цьому випадку код брав
    # ВЕСЬ цей вже-готовий OR-рядок як єдиний "тайтл" і ще раз обгортав його в
    # зовнішні лапки нижче - Google читав подвійні лапки як пошук ОДНІЄЇ
    # точної фрази, якої ніде не існує, і X-ray канал гарантовано повертав 0
    # результатів (підтверджено логом: 0 посилань у кожній ітерації). Тепер
    # searchQuery (якщо currentJobTitles порожній) використовується як
    # ГОТОВИЙ boolean-вираз, без повторного обгортання в лапки.
    titles = (search_params.get("currentJobTitles") or [])[:3]
    if titles:
        title_clause = " OR ".join(f'"{t}"' for t in titles if t)
    else:
        title_clause = (search_params.get("searchQuery") or "").strip()
    locations = (clean_locations(search_params.get("locations")) or ["Ukraine"])[:3]
    location_clause = " OR ".join(f'"{l}"' for l in locations if l)

    query = "site:linkedin.com/in"
    if title_clause:
        query += f" ({title_clause})"
    if location_clause:
        query += f" ({location_clause})"
    return query


def run_google_xray_search(apify_token: str, query: str) -> list:
    """Шукає LinkedIn-профілі через звичайний Google-пошук (актор
    apify/google-search-scraper), тим самим Apify-токеном - окремий Google
    API key не потрібен. Повертає список dict {"url", "title", "snippet"}
    (не просто "голі" URL) - заголовок і сніпет з видачі Google потрібні,
    щоб відсіяти нерелевантні посилання ще ДО того, як платити за повний
    скрапінг профілю через fetch_linkedin_profiles_by_url."""
    client = ApifyClient(apify_token)
    run_input = {"queries": query, "maxPagesPerQuery": GOOGLE_XRAY_MAX_PAGES}
    try:
        run = client.actor(GOOGLE_SEARCH_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId у відповіді Google-пошуку (тип: {type(run).__name__})"
        items = list(client.dataset(dataset_id).iterate_items())
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    results = []
    seen = set()
    for item in items:
        for r in (item.get("organicResults") or []):
            url = r.get("url", "")
            if "linkedin.com/in/" in url:
                clean_url = url.split("?")[0].rstrip("/")
                if clean_url not in seen:
                    seen.add(clean_url)
                    results.append({
                        "url": clean_url,
                        "title": r.get("title", ""),
                        "snippet": r.get("description") or r.get("snippet") or "",
                    })
    return results, None


def extract_title_keywords(search_params: dict) -> list:
    """Спільна логіка витягування "голих" ключових слів з тайтлів пошуку -
    або з currentJobTitles (якщо заповнено), або розбором boolean OR-рядка
    searchQuery (прибираючи OR/лапки). Винесено окремо, щоб однакову логіку
    використовували і X-ray фільтр (filter_relevant_xray_results), і
    дешевий pre-filter основного LinkedIn-каналу (filter_relevant_profiles)
    - раніше вона існувала тільки для X-ray, і саме тому pre-filter на
    основному каналі був відсутній (Bug 3 з QA-звіту)."""
    titles = list(search_params.get("currentJobTitles") or [])
    if not titles and search_params.get("searchQuery"):
        titles = [t.strip(' "') for t in re.split(r'\bOR\b', search_params["searchQuery"]) if t.strip(' "')]
    return [t.lower() for t in titles if t]


def filter_relevant_xray_results(xray_results: list, search_params: dict) -> list:
    """Безкоштовний евристичний фільтр (без виклику Claude): перевіряє, чи в
    заголовку/сніпеті з Google взагалі згадується щось із шуканих тайтлів,
    перш ніж витрачати гроші на платний скрапінг повного профілю через
    fetch_linkedin_profiles_by_url. Google іноді видає посилання на
    LinkedIn-профілі, ніяк не пов'язані з шуканою роллю (проста збіжна назва
    компанії, локації тощо) - без цього фільтру ми платили б за скрапінг і
    таких профілів теж."""
    keywords = extract_title_keywords(search_params)
    if not keywords:
        return xray_results  # нема з чим звіряти - краще не відсіювати наосліп

    relevant = []
    for item in xray_results:
        haystack = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
        if any(kw in haystack for kw in keywords):
            relevant.append(item)
    return relevant


def filter_relevant_profiles(profiles: list, search_params: dict) -> tuple[list, int]:
    """БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ (Bug 3 з QA-звіту): такий самий дешевий
    keyword pre-filter (без виклику Claude) уже існував, але застосовувався
    ТІЛЬКИ до Google X-ray каналу (filter_relevant_xray_results вище).
    Основний канал (harvestapi/linkedin-profile-search), який реально дає
    по 20-25 кандидатів щоразу і з'їдає весь бюджет Claude (Evaluator +
    Critic на КОЖНОГО), такого фільтру не мав - через AND-баг Apify
    currentJobTitles завжди лишається порожнім, і пошук іде як широкий
    full-text-match по searchQuery, який реально повертає багато явно
    нерелевантних профілів (рекрутери/маркетологи/інженери на пошук "Legal
    Counsel"). Кожен такий кандидат усе одно йшов на повний платний виклик
    Судді, перш ніж отримати hard-stop.

    Перевіряємо headline і назву поточної посади (currentPosition) на
    згадку хоч одного шуканого тайтлу - якщо жодного немає, профіль
    відсіюється ще ДО виклику Evaluator/Critic. Якщо нема з чим звіряти
    (немає ключових слів) - повертаємо все як є, а не відсіюємо наосліп.
    Повертає (відфільтрований список, скільки відсіяно)."""
    keywords = extract_title_keywords(search_params)
    if not keywords:
        return profiles, 0

    relevant = []
    for p in profiles:
        haystack = get_profile_headline(p).lower()
        cp = p.get("currentPosition")
        if isinstance(cp, list):
            haystack += " " + " ".join((c.get("title") or "") for c in cp if isinstance(c, dict))
        elif isinstance(cp, dict):
            haystack += " " + (cp.get("title") or "")
        if any(kw in haystack for kw in keywords):
            relevant.append(p)
    return relevant, len(profiles) - len(relevant)


def fetch_linkedin_profiles_by_url(apify_token: str, urls: list) -> list:
    """Дотягує повні дані профілю для URL, знайдених через Google X-ray
    (сам Google дає тільки заголовок і сніпет, не структуровані дані)."""
    if not urls:
        return [], None
    client = ApifyClient(apify_token)
    run_input = {"urls": urls}
    try:
        run = client.actor(LINKEDIN_PROFILE_BY_URL_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId (тип: {type(run).__name__})"
        return list(client.dataset(dataset_id).iterate_items()), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# AGENT 2: ATS CHECKER (Ashby) - ЗАГЛУШКА
# ---------------------------------------------------------------------------

def agent_ats_checker(ashby_api_key: str, profile: dict) -> dict:
    """
    Правила (за ТЗ):
    - Кандидата немає в Ashby -> proceed
    - Кандидат є, але НЕ на активній вакансії поточного рекрутера -> proceed,
      позначити 'Exists in ATS, free'
    - Кандидат є і активний на вакансії ІНШОГО рекрутера -> SKIP

    !!! TODO ASHBY !!!
    Зараз ця функція нічого реально не перевіряє, бо Ashby Admin API key
    ще не підключений. Коли ключ буде готовий, тут треба:
    1. Викликати Ashby REST API (https://developers.ashbyhq.com/) напряму
       (POST на https://api.ashbyhq.com/candidate.search з іменем/LinkedIn URL).
    2. Якщо candidate знайдений - викликати application.list по candidateId,
       дістати job.hiringTeam і порівняти recruiter з поточним юзером.
    3. Повернути один із статусів нижче замість "not_checked".

    Ashby працює через звичайний REST API з Admin API key - НЕ через MCP,
    бо MCP живе всередині Claude/Cowork-чату, а не всередині окремого
    Streamlit-додатку, який ти відкриваєш сама в браузері.
    """
    if not ashby_api_key:
        return {"status": "not_checked", "note": "Ashby ще не підключено - перевір вручну перед аутричем"}

    # Поки ключ є, але логіку ще не реалізовано - чесно позначаємо це,
    # а не вдаємо, що перевірка відбулась.
    return {"status": "not_implemented", "note": "Ashby key є, але інтеграцію ще не дописано (див. TODO ASHBY)"}


# ---------------------------------------------------------------------------
# AGENT 3: STRICT EVALUATOR
# ---------------------------------------------------------------------------

EVALUATOR_TOOL_SCHEMA = {
    "name": "submit_evaluation",
    "description": "Оцінка кандидата за жорсткими правилами SKELAR",
    "input_schema": {
        "type": "object",
        "properties": {
            "hard_stop": {"type": "boolean"},
            "hard_stop_reason": {"type": "string"},
            "stop_list_hit": {"type": "string"},
            "ex_stop_list_company": {"type": "string"},
            "location_note": {"type": "string"},
            "total_yoe": {"type": "number"},
            "relevant_yoe": {"type": "number"},
            "grade": {"type": "string", "enum": ["Junior", "Middle", "Senior", "Lead"]},
            "tier": {"type": "string", "enum": ["A", "B", "C"]},
            "summary": {"type": "string"},
        },
        "required": ["hard_stop", "hard_stop_reason", "stop_list_hit", "ex_stop_list_company",
                     "location_note", "total_yoe", "relevant_yoe", "grade", "tier", "summary"],
    },
}


def agent_evaluator(client, job_description: str, profile_text: str, stop_list: list, allow_belarusians: bool, recruiter_feedback: list = None, donor_list: list = None) -> dict:
    feedback_block = json.dumps((recruiter_feedback or [])[-MAX_FEEDBACK_ENTRIES_IN_PROMPT:], ensure_ascii=False, indent=2)
    system_prompt = f"""Ти - Strict Evaluator, найсуворіший рекрутер-оцінювач кандидатів у SKELAR.

Профіль кандидата - це реальний JSON з актора harvestapi/linkedin-profile-search.
Ось ТОЧНА структура полів, якими треба користуватись (перевірено на реальних
даних актора, не вгадуй інші назви):
- `currentPosition` (масив) - ПОТОЧНЕ місце роботи кандидата ЗАРАЗ. Дивись
  `currentPosition[].companyName`.
- `experience` (масив) - ВСЯ історія роботи, включно з поточною. Запис
  вважається поточним, якщо `endDate.text == "Present"`, інакше - це минуле
  місце роботи.
- `location.parsed.country` / `location.parsed.city` / `location.linkedinText` -
  структурована локація кандидата. Це основне джерело для визначення країни,
  а не здогадки з тексту "about" чи skills.

ЖОРСТКІ HARD-STOP ПРАВИЛА (якщо хоч одне спрацювало - hard_stop = true,
незалежно від будь-яких інших переваг кандидата):
1. За `location.parsed.country` / `location.linkedinText`: кандидат з Росії,
   живе в Росії, або явно вказує російське громадянство без ознак
   релокації/протидії режиму - HARD STOP.
   Білоруси дозволені ТІЛЬКИ якщо allow_belarusians = {allow_belarusians} і
   вони явно перебувають ЗА межами Білорусі.
2. Компанія САМЕ з `currentPosition[].companyName` (тобто ПОТОЧНИЙ
   роботодавець) збігається (навіть частково, без урахування регістру) з
   будь-якою компанією зі стоп-листа: {stop_list} -> HARD STOP.
   ВАЖЛИВО: якщо стоп-компанія зустрічається лише в `experience` як МИНУЛЕ
   місце роботи (endDate не "Present") - це НЕ hard stop. Колишні
   працівники стоп-компаній - це нормальна і навіть цільова аудиторія для
   найму, блокуємо тільки тих, хто працює там ЗАРАЗ.

Якщо hard_stop не спрацював - оціни:
- total_yoe: загальний досвід роботи в роках, порахований з усіх записів
  `experience` (число)
- relevant_yoe: досвід САМЕ в релевантній для вакансії сфері/ролі, теж з
  `experience` (число). Якщо загальний досвід 5 років, але релевантний
  тільки 1 рік - грейд визначається по relevant_yoe (тобто це Junior, а не
  Middle/Senior).
- grade: Junior / Middle / Senior / Lead (на основі relevant_yoe і рівня задач)
- tier: "A" (ідеальний збіг з вакансією), "B" (непоганий, але не ідеальний),
  "C" (слабкий збіг)

ДВІ ДОДАТКОВІ ЕВРИСТИКИ (перевірені на практиці в іншому нашому сорсинговому
інструменті - переносимо сюди, бо це реальні, а не гіпотетичні пастки):
1. Стеля переквалифікації: досвід 10+ років - це і сеньйор на 10-12 років, і
   ветеран на 20-40. Якщо кандидат явно перекваліфікований для цієї ролі
   (фаундер, ex-C-level, GC/CLO чи подібний рівень 20+ років) і вакансія -
   рядова IC-роль (не керівна) - це НЕ tier "A", навіть якщо formal grade і
   yoe формально підходять. Постав tier "B" або "C" і напиши причину в
   summary (overqualified) - такі кандидати або не відгукнуться, або швидко
   підуть.
2. Домен ≠ тайтл: збіг назви посади не означає збіг сфери/індустрії
   досвіду. Якщо `experience` кандидата явно з нерелевантного для вакансії
   домену (зовсім інша галузь чи тип продукту) - НЕ став tier "A" тільки
   через формальний збіг тайтла, навіть при правильному стажі.

КОМПАНІЇ-ДОНОРИ (перенесено з /search-strategy-assistant, м'який сигнал,
може бути порожньо): {donor_list or []}
Це компанії, чиї поточні чи колишні співробітники історично добре
підходять під такі ролі - НЕ hard-правило і НЕ обов'язкова умова. Якщо
`currentPosition[].companyName` АБО будь-який запис `experience[]`
кандидата збігається (навіть частково) з компанією зі списку - це
позитивний сигнал: за інших рівних можеш трохи впевненіше поставити tier
"A" замість "B" і згадати компанію-донора в summary. Відсутність збігу
з донорами НІЧОГО поганого не означає - не знижуй tier тільки через це.

ФІДБЕК РЕКРУТЕРА - реальні приклади кандидатів, яких рекрутерка вже вручну
відхилила раніше, і чому (може бути порожньо):
{feedback_block}
Це не гіпотези, а факти про смак цієї конкретної рекрутерки. Якщо кандидат
має ПОДІБНИЙ мінус до одного з прикладів вище (та сама причина відхилення
по суті, не обов'язково той самий текст) - НЕ став йому tier "A", навіть
якщо формально він проходить hard-stop правила. Познач у summary, на який
саме приклад фідбеку це схоже.

Виклич інструмент submit_evaluation з результатом (порожні текстові поля
залишай як "" якщо неактуальні, а не пропускай)."""
    user_prompt = f"""Опис вакансії:
{job_description}

Дані профілю кандидата (сирий JSON з LinkedIn):
{profile_text}
"""
    return ask_claude_tool(client, system_prompt, user_prompt, EVALUATOR_TOOL_SCHEMA)


# ---------------------------------------------------------------------------
# AGENT 4: SELF-CHECKER (CRITIC)
# ---------------------------------------------------------------------------

CRITIC_TOOL_SCHEMA = {
    "name": "submit_critique",
    "description": "Перевірка висновку Evaluator-а на помилки та галюцинації",
    "input_schema": {
        "type": "object",
        "properties": {
            "pass": {"type": "boolean"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["pass", "issues", "confidence"],
    },
}


def agent_critic(client, job_description: str, profile_text: str, evaluator_result: dict, stop_list: list) -> dict:
    system_prompt = f"""Ти - Critic, скептичний внутрішній аудитор, який перевіряє роботу
іншого AI-агента (Evaluator) і шукає в ній помилки та галюцинації.

Тобі дали висновок Evaluator-а. Постав собі ці конкретні контрольні питання
і чесно дай відповідь по кожному:
1. Чи Evaluator дійсно звірив САМЕ поточну компанію кандидата (поле
   `currentPosition[].companyName`, а не всю історію `experience`) зі
   стоп-листом {stop_list}, чи просто написав "false" навмання? Якщо
   стоп-компанія є лише в минулому досвіді (не поточна) - це НЕ повинно
   бути причиною відхилення, і якщо Evaluator помилково поставив через це
   hard_stop - познач як issue.
2. Чи справді є докази в `location.parsed` / `location.linkedinText`
   профілю, що людина НЕ з Росії (а не просто відсутність згадки про Росію)?
3. Чи relevant_yoe порахований коректно як релевантний досвід, а не
   переплутаний із total_yoe?
4. Чи grade і tier логічно узгоджені з описаним досвідом?

pass = true ставиш ТІЛЬКИ якщо жодних проблем не знайдено (перфектний скор).
Якщо є хоч один сумнів - pass = false. Виклич інструмент submit_critique
з результатом."""
    user_prompt = f"""Опис вакансії:
{job_description}

Профіль кандидата (сирий JSON):
{profile_text}

Висновок Evaluator-а, який треба перевірити:
{json.dumps(evaluator_result, ensure_ascii=False, indent=2)}
"""
    return ask_claude_tool(client, system_prompt, user_prompt, CRITIC_TOOL_SCHEMA)


# ---------------------------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="AI Sourcing Agent - SKELAR", page_icon="🎯", layout="wide")

st.title("🎯 AI Sourcing Agent - SKELAR")
st.caption("Автономний пошук кандидатів: Apify + Ashby (заглушка поки) + Claude агенти")

# --- SIDEBAR: НАЛАШТУВАННЯ ---


def get_shared_api_key(secret_name: str, label: str, help_text: str | None = None) -> str:
    """Ключ для всієї команди: спочатку шукаємо в st.secrets (Streamlit Secrets -
    налаштовується в Settings -> Secrets на Streamlit Cloud, або в локальному
    .streamlit/secrets.toml, який НЕ комітиться в git). Це дає командний доступ
    без хардкоду ключа прямо в app.py - хардкод означав би, що ключ лежить
    відкритим текстом у git-історії репозиторію, і будь-хто з доступом до репо
    (або випадковий витік) отримує повний доступ до вашого білінгу.
    Якщо секрет не налаштований (напр. хтось запускає локально без нього) -
    падаємо назад на звичайне поле ручного вводу, щоб додаток і далі працював."""
    try:
        secret_value = st.secrets.get(secret_name)
    except Exception:
        secret_value = None
    if secret_value:
        return secret_value
    return st.text_input(label, type="password", help=help_text)


with st.sidebar:
    st.header("🔑 API ключі")
    st.caption(
        "Якщо ключі налаштовані в Streamlit Secrets (для всієї команди) - поля "
        "нижче не з'являться, додаток підхопить їх сам. Інакше введи вручну."
    )
    anthropic_api_key = get_shared_api_key("ANTHROPIC_API_KEY", "Anthropic API key")
    apify_token = get_shared_api_key("APIFY_API_TOKEN", "Apify API token")
    ashby_api_key = get_shared_api_key(
        "ASHBY_API_KEY",
        "Ashby Admin API key (опційно)",
        "Поки не обов'язково. Без нього Ashby-перевірка просто пропускається зі статусом 'Не перевірено'.",
    )

    st.divider()
    st.header("⚙️ Параметри циклу")
    max_iterations = st.slider("Максимум ітерацій пошуку (safety limit)", min_value=1, max_value=20, value=5)
    max_profiles_per_iteration = st.slider("Профілів за одну ітерацію (Apify maxItems)", min_value=10, max_value=100, value=25)
    enable_google_xray = st.checkbox(
        "🌐 Додатково шукати через Google X-ray",
        value=True,
        help="Паралельний канал у тій самій ітерації: шукає LinkedIn-профілі через Google (site:linkedin.com/in), "
        "яких могла не показати пряма видача LinkedIn-пошуку. Використовує той самий Apify-токен, окремий ключ не потрібен.",
    )
    enable_calibration = st.checkbox(
        "🔍 Спершу калібрування - підтвердь перших кандидатів",
        value=True,
        help="Перш ніж шукати повну кількість, цикл зупиниться, щойно набереться calibration_size "
        "схвалених кандидатів, і покаже їх тобі на підтвердження - той самий напрямок пошуку, чи ні. "
        "Лише після явного 'так' цикл продовжить шукати решту до цілі. Якщо скажеш 'ні' і даси "
        "корективу - попередня (неправильна) вибірка скидається, і цикл покаже НОВУ вибірку знову, "
        "поки не підтвердиш. Це та сама ідея, що й 'калібрація' в /sourcer-ai.",
    )
    calibration_size = st.number_input(
        "Розмір калібрувальної вибірки", min_value=1, max_value=20, value=3,
        help="Скільки перших схвалених кандидатів показати на підтвердження, перш ніж шукати решту.",
        disabled=not enable_calibration,
    )

    st.divider()
    st.header("🚫 Do-Not-Hunt список компаній")
    st.caption("Впиши через кому назви компаній, кандидатів з яких (поточних або минулих) система одразу відсіює.")
    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: раніше value="Приклад Компанія 1, Приклад
    # Компанія 2" підставляла ЦЕЙ ТЕКСТ як РЕАЛЬНЕ значення поля (не сіру
    # підказку-placeholder) - якщо рекрутерка не помітить і не очистить
    # поле, стоп-лист фактично не працює (перевіряє неіснуючі компанії),
    # а виглядає так, ніби захист увімкнений. Тепер поле порожнє за
    # замовчуванням, приклад формату - лише як сірий placeholder.
    stop_list_raw = st.text_area(
        "Стоп-лист компаній",
        value="",
        placeholder="напр. Компанія А, Компанія Б",
        height=100,
        help="Порожньо за замовчуванням - якщо не заповнити, стоп-лист просто не застосовується.",
    )

    st.divider()
    st.header("🏢 Компанії-донори (опційно)")
    st.caption(
        "Перенесено з нашого іншого сорсингового інструменту (/sourcer-ai): компанії, "
        "чиї поточні/колишні співробітники історично добре підходять під такі ролі. "
        "Це М'ЯКИЙ сигнал для Судді (Evaluator) - не жорсткий фільтр Apify. Свідомо НЕ "
        "робимо це нативним фільтром актора (currentCompanies) - той фільтр вимагає "
        "LinkedIn-URL компаній (не просто назв) і застосовується як AND з searchQuery, "
        "а ми вже на практиці бачили, що така комбінація легко дає 0 результатів, навіть "
        "коли кандидати реально є. М'який сигнал безпечніший: кандидата з компанії-донора "
        "не відкидає і не вимагає збігу, лише трохи піднімає впевненість при виборі між "
        "B і A за інших рівних."
    )
    donor_list_raw = st.text_area(
        "Компанії-донори",
        value="",
        placeholder="напр. Компанія В, Компанія Г",
        height=80,
    )

    st.divider()
    st.header("🌍 Локація / громадянство")
    allow_belarusians = st.checkbox("Дозволити білорусів, якщо вони ЗА кордоном Білорусі", value=False)

    st.divider()
    st.header("🧠 Пам'ять про вирішені проблеми")
    st.caption(
        "Це справжня пам'ять між сесіями, не копі-паста: PM-агент сам зберігає нові "
        "уроки (напр. 'локація Remote завжди дає 404') у твоєму персональному Apify "
        "Key-Value Store - тому самому акаунті, що й Apify token вище, без жодного "
        "додаткового ключа. Кожна наступна сесія (навіть в іншому браузері) підвантажує "
        "ці уроки сама. Тобі нічого копіювати не треба."
    )
    st.session_state.setdefault("known_issues", [])
    st.session_state.setdefault("known_issues_synced_token", None)
    if apify_token:
        if st.session_state.known_issues_synced_token != apify_token:
            loaded_issues, load_error = load_known_issues(apify_token)
            if load_error:
                st.warning(f"Не вдалось завантажити пам'ять про уроки: {load_error}")
            else:
                st.session_state.known_issues = loaded_issues
            st.session_state.known_issues_synced_token = apify_token
        st.caption(f"У постійній пам'яті зараз **{len(st.session_state.known_issues)}** уроків.")
        if st.session_state.known_issues:
            with st.expander("Показати уроки"):
                for issue in st.session_state.known_issues:
                    st.markdown(f"- {issue}")
            if st.button("🗑️ Очистити пам'ять (якщо агент вивчив щось хибне)"):
                clear_error = save_known_issues(apify_token, [])
                if clear_error:
                    st.warning(f"Не вдалось очистити: {clear_error}")
                else:
                    st.session_state.known_issues = []
                    st.success("Пам'ять очищена.")
    else:
        st.caption("Введи Apify token вище, щоб побачити накопичені уроки.")

    st.divider()
    st.header("🎯 Фідбек рекрутера (навчання на відхиленнях)")
    st.caption(
        "Коли ти вручну відхиляєш кандидата на екрані результатів і вказуєш причину - "
        "вона зберігається в той самий персональний Apify Key-Value Store. Evaluator і "
        "Strategist бачать ці приклади наперед і намагаються не пропонувати tier 'A' "
        "кандидатам з подібними мінусами знову."
    )
    st.session_state.setdefault("recruiter_feedback", [])
    st.session_state.setdefault("recruiter_feedback_synced_token", None)
    if apify_token:
        if st.session_state.recruiter_feedback_synced_token != apify_token:
            loaded_feedback, feedback_load_error = load_recruiter_feedback(apify_token)
            if feedback_load_error:
                st.warning(f"Не вдалось завантажити фідбек рекрутера: {feedback_load_error}")
            else:
                st.session_state.recruiter_feedback = loaded_feedback
            st.session_state.recruiter_feedback_synced_token = apify_token
        st.caption(f"У пам'яті зараз **{len(st.session_state.recruiter_feedback)}** відхилених прикладів.")
        if st.session_state.recruiter_feedback:
            with st.expander("Показати фідбек"):
                for fb in st.session_state.recruiter_feedback:
                    st.markdown(f"- **{fb.get('candidate_name', '?')}** ({fb.get('role', '?')}): {fb.get('reason', '?')}")
            if st.button("🗑️ Очистити фідбек"):
                clear_fb_error = save_recruiter_feedback(apify_token, [])
                if clear_fb_error:
                    st.warning(f"Не вдалось очистити: {clear_fb_error}")
                else:
                    st.session_state.recruiter_feedback = []
                    st.success("Фідбек очищено.")
    else:
        st.caption("Введи Apify token вище, щоб побачити накопичений фідбек.")

# --- ОБРОБКА СТОП-ЛИСТА І СПИСКУ ДОНОРІВ ---
stop_list = [c.strip() for c in stop_list_raw.split(",") if c.strip()]
donor_list = [c.strip() for c in donor_list_raw.split(",") if c.strip()]

# --- ІНІЦІАЛІЗАЦІЯ SESSION STATE ---
# stage: "idle" (форма вводу) -> "active" (крутиться по ітераціях з чекпоінтами) -> "done" (фінал)
for key, default in [
    ("stage", "idle"),
    ("trigger_run", False),
    ("pending_note", ""),
    ("approved_candidates", []),
    ("log", []),
    ("search_history", []),
    ("seen_urls", set()),
    ("iteration", 0),
    ("page_cursor", {}),
    ("brief_job_description", ""),
    ("brief_target_count", 10),
    ("brief_questions", []),
    # "known_issues" НЕ тут - це постійна пам'ять, ініціалізується і синхронізується
    # окремо в сайдбарі (setdefault + завантаження з Apify Key-Value Store), а не
    # скидається щоразу як звичайний стан циклу.
]:
    if key not in st.session_state:
        st.session_state[key] = default


def lock_in_and_start(job_description: str, target_count: int):
    """Фіксує параметри запуску (щоб зміни в сайдбарі під час циклу не плутали
    хід уже розпочатого пошуку) і переводить стан у "active". Спільна для
    обох шляхів: коли Стратег одразу готовий (без питань) і коли рекрутерка
    щойно відповіла на брифінг."""
    st.session_state.job_description = job_description
    st.session_state.target_count = target_count
    st.session_state.max_iterations = max_iterations
    st.session_state.max_profiles = max_profiles_per_iteration
    st.session_state.enable_google_xray = enable_google_xray
    # Калібрування: якщо вибірка для калібрування >= самої цілі пошуку -
    # окремо калібрувати нема сенсу (це вже весь пошук), тому в цьому
    # випадку gate вимикається одразу (calibration_approved = True).
    st.session_state.calibration_enabled = bool(enable_calibration) and calibration_size < target_count
    st.session_state.calibration_size = calibration_size
    st.session_state.calibration_approved = not st.session_state.calibration_enabled
    st.session_state.stop_list = stop_list
    st.session_state.donor_list = donor_list
    st.session_state.allow_belarusians = allow_belarusians
    st.session_state.ashby_api_key = ashby_api_key
    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: раніше anthropic_api_key і apify_token НЕ
    # фіксувались тут, на відміну від УСІХ інших параметрів циклу вище. Це
    # означало, що "active"-екран щоразу читав live-значення прямо з полів
    # сайдбару - якщо рекрутерка випадково змінить/зітре поле ключа в
    # сайдбарі поки цикл активний (між ітераціями чекпоінта), наступна
    # ітерація тихо піде з іншим (можливо зламаним) ключем без пояснення,
    # чому раптом усе відвалилось. Фіксуємо їх так само, як усе інше.
    st.session_state.anthropic_api_key = anthropic_api_key
    st.session_state.apify_token = apify_token

    st.session_state.iteration = 0
    st.session_state.approved_candidates = []
    st.session_state.search_history = []
    st.session_state.log = []
    st.session_state.page_cursor = {}

    # Пам'ять про вже перевірених кандидатів - ПРИВ'ЯЗАНА до цієї конкретної
    # вакансії (не спільна на все, як known_issues). Якщо для цього ж опису
    # вакансії вже колись шукали - підвантажуємо, кого вже перевіряли, щоб
    # не платити за повторний скрапінг і повторну оцінку тих самих людей.
    loaded_seen, seen_error = load_seen_urls(apify_token, job_description)
    if seen_error:
        st.session_state.log.append(f"⚠️ Не вдалось завантажити пам'ять про перевірених кандидатів: {seen_error}")
    elif loaded_seen:
        st.session_state.log.append(
            f"🧠 Для цієї вакансії вже є {len(loaded_seen)} раніше перевірених кандидатів у пам'яті - "
            "вони не скрапляться і не оцінюються повторно."
        )
    st.session_state.seen_urls = loaded_seen

    # st.session_state.known_issues НЕ скидаємо і не переприсвоюємо тут - це
    # постійна пам'ять (Apify Key-Value Store), уже підвантажена в сайдбарі,
    # і має продовжувати рости, а не обнулятись з кожним новим пошуком.
    st.session_state.stage = "active"
    st.session_state.trigger_run = True


def process_single_candidate(claude_client, profile: dict, source: str, ashby_api_key: str,
                              job_description: str, stop_list: list, allow_belarusians: bool,
                              recruiter_feedback: list = None, donor_list: list = None):
    """Повна перевірка ОДНОГО кандидата: ATS -> Evaluator -> Critic. Винесено в
    окрему функцію (без жодного звернення до st.session_state всередині),
    щоб можна було безпечно запускати паралельно через ThreadPoolExecutor -
    послідовна перевірка 25 кандидатів по черзі займала понад хвилину.
    Повертає (candidate_row або None, [рядки для логу])."""
    name = get_profile_name(profile)
    url = get_profile_url(profile)
    log_lines = []

    ats_result = agent_ats_checker(ashby_api_key, profile)
    if ats_result["status"] == "exists_blocked":
        log_lines.append(f"⛔ {TEAM['ats']}: {name} активний на вакансії іншого рекрутера в Ashby, пропускаю")
        return None, log_lines

    profile_text = profile_to_compact_text(profile)
    eval_result = agent_evaluator(claude_client, job_description, profile_text, stop_list, allow_belarusians, recruiter_feedback=recruiter_feedback, donor_list=donor_list)
    if "error" in eval_result:
        log_lines.append(f"⚠️ {TEAM['evaluator']}: помилка оцінки {name} ({eval_result['error']}), пропускаю")
        return None, log_lines
    if eval_result.get("hard_stop"):
        log_lines.append(f"⛔ {TEAM['evaluator']}: {name} - hard stop, {eval_result.get('hard_stop_reason')}")
        return None, log_lines
    if eval_result.get("tier") != "A":
        log_lines.append(f"🟡 {TEAM['evaluator']}: {name} - {eval_result.get('tier', '?')}-tier, не ідеальний збіг, пропускаю")
        return None, log_lines

    critic_result = agent_critic(claude_client, job_description, profile_text, eval_result, stop_list)
    if "error" in critic_result or not critic_result.get("pass"):
        reason = critic_result.get("issues") if "error" not in critic_result else critic_result["error"]
        log_lines.append(f"⚠️ {TEAM['critic']}: {name} не пройшов self-check ({reason}), пропускаю")
        return None, log_lines

    summary_text = eval_result.get("summary", "")
    ex_stop_company = eval_result.get("ex_stop_list_company")
    if ex_stop_company:
        summary_text += f" (Ex-{ex_stop_company} - колишній працівник стоп-компанії, не поточний)"

    candidate_row = {
        "Обрати": False,
        "Ім'я": name,
        "LinkedIn": url,
        "Поточна роль": get_profile_headline(profile),
        "Грейд": eval_result.get("grade", "н/д"),
        "Джерело": source,
        "Ashby статус": ats_result["note"],
        "AI summary": summary_text,
        "Причина відхилення": "",
        "Своя причина": "",
    }
    log_lines.append(f"✅ {TEAM['critic']} підтвердив {name} ({source}) - кандидат A-tier")
    return candidate_row, log_lines


def search_signature(search_params: dict) -> str:
    """"Підпис" конкретного пошукового запиту (як cursor.json в еталонному
    скілі /sourcer-ai) - щоб пам'ятати, на якій сторінці видачі ми зупинились
    для КОЖНОГО унікального запиту окремо, а не для пошуку загалом."""
    query = (search_params.get("searchQuery") or "").strip().lower()
    locations = tuple(sorted(l.strip().lower() for l in (search_params.get("locations") or [])))
    return f"{query}|{locations}"


def run_one_iteration(claude_client, apify_token: str, recruiter_note: str):
    """Виконує РІВНО одну ітерацію циклу (Strategist -> Apify -> Evaluator -> Critic)
    і зупиняється. Це навмисно - щоб рекрутер міг подивитись проміжний результат
    і дати текстову корективу перед наступною ітерацією, а не чекати кінця всього
    циклу наосліп."""
    st.session_state.iteration += 1
    iteration = st.session_state.iteration
    still_needed = st.session_state.target_count - len(st.session_state.approved_candidates)
    log = st.session_state.log

    log.append(f"#### Ітерація {iteration}/{st.session_state.max_iterations}")
    if recruiter_note.strip():
        log.append(f"📝 Корективи від рекрутера для цієї ітерації: _{recruiter_note.strip()}_")

    search_params = agent_strategist(
        claude_client, st.session_state.job_description, st.session_state.search_history, still_needed,
        recruiter_note, known_issues=st.session_state.known_issues,
        recruiter_feedback=st.session_state.recruiter_feedback,
    )
    if search_params.get("reasoning") == "fallback":
        # Claude сам не відповів навіть після повторних спроб - це деградований
        # запит (буквально перші 100 символів опису вакансії як текст пошуку),
        # а НЕ нормальний boolean-запит. Кажемо про це прямо, а не мовчки.
        log.append(
            f"⚠️ {TEAM['strategist']} не зміг згенерувати запит (Claude не відповів): "
            f"{search_params.get('fallback_error', 'невідома помилка')}. "
            "Використовую аварійний запит - швидше за все дасть 0 або сміттєві результати."
        )
    extra_titles_note = f" | точний фільтр тайтлів: {search_params.get('currentJobTitles')}" if search_params.get("currentJobTitles") else ""
    log.append(
        f"{TEAM['strategist']}: запит `{search_params.get('searchQuery')}`{extra_titles_note} | локації: {search_params.get('locations')}"
    )

    # --- PM-цикл: Claude сам діагностує і лагодить запит, якщо перша спроба
    # дала помилку або підозріло 0 профілів, замість того щоб сліпо чекати
    # наступної повної ітерації циклу. ---
    previous_attempts = []
    profiles, apify_error = [], None
    for attempt in range(1, MAX_APIFY_ATTEMPTS_PER_ITERATION + 1):
        sig = search_signature(search_params)
        start_page = st.session_state.page_cursor.get(sig, 1)
        profiles, apify_error = run_apify_search(apify_token, search_params, st.session_state.max_profiles, start_page=start_page)
        # Наступного разу для цього ж самого запиту тягнемо НАСТУПНУ сторінку
        # видачі, а не ту саму - інакше повторно повертається той самий верх.
        pages_consumed = max(1, math.ceil(st.session_state.max_profiles / 25))
        st.session_state.page_cursor[sig] = start_page + pages_consumed

        previous_attempts.append({
            "attempt": attempt, "search_params": search_params,
            "error": apify_error, "profiles_found": len(profiles),
        })

        if apify_error:
            log.append(f"❌ Спроба {attempt}/{MAX_APIFY_ATTEMPTS_PER_ITERATION}: помилка Apify - {apify_error}")
        else:
            log.append(f"📥 Спроба {attempt}/{MAX_APIFY_ATTEMPTS_PER_ITERATION} (сторінка {start_page}): знайдено {len(profiles)} профілів")

        if not apify_error and len(profiles) > 0:
            break  # Все ок - працюємо з цим результатом далі

        if attempt == MAX_APIFY_ATTEMPTS_PER_ITERATION:
            log.append(f"{TEAM['pm']} вичерпав спроби полагодити запит у цій ітерації, працюю з тим, що є")
            break

        log.append(f"{TEAM['pm']} аналізує, чому запит не дав результату, і лагодить його...")
        repaired = agent_diagnose_and_repair(
            claude_client, st.session_state.job_description, search_params, apify_error, len(profiles),
            previous_attempts, known_issues=st.session_state.known_issues,
        )
        if not repaired or repaired.get("error"):
            error_detail = repaired.get("error") if repaired else "порожня відповідь"
            log.append(f"⚠️ {TEAM['pm']} не зміг запропонувати виправлення ({error_detail}), зупиняюсь на поточному запиті")
            break
        log.append(f"{TEAM['pm']}, діагноз: {repaired.get('diagnosis')}")
        log.append(f"{TEAM['pm']}, новий запит: `{repaired.get('searchQuery')}` | локації: {repaired.get('locations')}")

        # Якщо PM-агент виявив НОВУ проблему, якої ще нема в пам'яті - одразу
        # зберігаємо її в персональний Apify Key-Value Store (write-through,
        # не чекаючи кінця сесії), щоб Strategist і PM більше НІКОЛИ не
        # наступали на ці самі граблі з нуля - ні пізніше в цій сесії, ні в
        # будь-якій майбутній, без жодної ручної дії рекрутерки.
        new_lesson = (repaired.get("new_lesson") or "").strip()
        if new_lesson and new_lesson not in st.session_state.known_issues:
            merged_issues, save_error = merge_and_save_known_issues(apify_token, st.session_state.known_issues, new_lesson)
            if save_error:
                log.append(f"⚠️ {TEAM['pm']} вивчив урок, але не вдалось зберегти в постійну пам'ять: {save_error}")
            else:
                st.session_state.known_issues = merged_issues
                log.append(f"🧠 {TEAM['pm']} запам'ятав назавжди: _{new_lesson}_")

        # ЗЛИВАЄМО, а не повністю заміняємо search_params: PM_TOOL_SCHEMA
        # містить лише поля, що стосуються технічної репарації запиту
        # (searchQuery/currentJobTitles/locations/autoQuerySegmentation).
        # Повна заміна (`search_params = repaired`) губила б будь-яке поле,
        # якого нема в PM-схемі - напр. yearsOfExperienceIds, який поставив
        # Strategist ще на початку ітерації, зник би після першого ж репару.
        search_params = {**search_params, **repaired}

    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: якщо get_profile_url не знайшла жодного з полів
    # (linkedinUrl/profileUrl/url/publicIdentifier), вона повертає "". Раніше
    # ми фільтрували і ДОДАВАЛИ в seen_urls за цим самим виразом - перший же
    # такий "безURL" профіль "отруював" пам'ять порожнім рядком, і УСІ
    # наступні профілі без розпізнаного URL (у всіх майбутніх сесіях, бо це
    # персистентна пам'ять) автоматично вважались "вже перевіреними" і
    # губились без жодної оцінки. Тепер порожній URL НІКОЛИ не потрапляє в
    # seen_urls (тому не може нічого "заблокувати" наперед), а фільтрація по
    # seen_urls працює тільки для профілів з реальним, непорожнім URL.
    new_profiles = []
    for p in profiles:
        url = get_profile_url(p)
        if url and url in st.session_state.seen_urls:
            continue
        new_profiles.append(p)
    for p in new_profiles:
        url = get_profile_url(p)
        if url:
            st.session_state.seen_urls.add(url)
    log.append(f"📥 LinkedIn-пошук (Apify): підсумково {len(profiles)} профілів, з них {len(new_profiles)} нових")

    # БАГ, ЯКИЙ ЦЕ ВИПРАВЛЯЄ: дешевий keyword pre-filter (без виклику Claude)
    # раніше застосовувався ТІЛЬКИ до Google X-ray каналу нижче. Основний
    # канал (harvestapi/linkedin-profile-search), який реально дає по 20-25
    # кандидатів щоразу і з'їдає весь бюджет Claude, такого фільтру не мав -
    # явно нерелевантні профілі (напр. рекрутери/маркетологи на пошук "Legal
    # Counsel") усе одно йшли на повний платний виклик Судді й Скептика,
    # перш ніж отримати hard-stop. Тепер перевіряємо headline/поточну посаду
    # на згадку хоч одного шуканого тайтлу ще ДО виклику Evaluator.
    relevant_profiles, skipped_irrelevant = filter_relevant_profiles(new_profiles, search_params)
    if skipped_irrelevant:
        log.append(
            f"🧹 Відсіяно {skipped_irrelevant} профілів за заголовком/поточною посадою як явно нерелевантні "
            "- не витрачаємо на них виклик Судді"
        )

    # Джерело 1: прямий LinkedIn-пошук
    candidate_pool = [(p, "LinkedIn пошук") for p in relevant_profiles]

    # Джерело 2 (паралельний канал): Google X-ray - той самий Apify-токен,
    # шукає LinkedIn-профілі, яких могла не показати пряма видача LinkedIn.
    if st.session_state.get("enable_google_xray"):
        xray_query = build_xray_query(search_params)
        log.append(f"🌐 Google X-ray запит: `{xray_query}`")
        xray_results, xray_error = run_google_xray_search(apify_token, xray_query)
        if xray_error:
            log.append(f"❌ ПОМИЛКА Google X-ray пошуку: {xray_error}")
        already_found_urls = st.session_state.seen_urls
        new_xray_results = [r for r in xray_results if r["url"] not in already_found_urls]
        for r in new_xray_results:
            st.session_state.seen_urls.add(r["url"])
        log.append(f"🌐 Google X-ray: знайдено {len(xray_results)} посилань, з них {len(new_xray_results)} нових")

        # Безкоштовний фільтр за заголовком/сніпетом ПЕРЕД платним скрапінгом -
        # не витрачаємо гроші на профілі, які Google повернув, але вони явно
        # не про шукану роль.
        relevant_xray_results = filter_relevant_xray_results(new_xray_results, search_params)
        skipped = len(new_xray_results) - len(relevant_xray_results)
        if skipped:
            log.append(f"🧹 Відсіяно {skipped} посилань за заголовком/сніпетом як нерелевантні - не скрапимо їх повний профіль")
        relevant_urls = [r["url"] for r in relevant_xray_results]

        xray_profiles, fetch_error = fetch_linkedin_profiles_by_url(apify_token, relevant_urls)
        if fetch_error:
            log.append(f"❌ ПОМИЛКА дотягування профілів через Google X-ray: {fetch_error}")
        log.append(f"🌐 Google X-ray: дотягнув повні дані для {len(xray_profiles)} профілів")
        candidate_pool += [(p, "LinkedIn через Google X-ray") for p in xray_profiles]

    # Зберігаємо оновлений список перевірених URL для ЦІЄЇ вакансії одразу
    # (write-through), щоб навіть дострокове закриття сесії не загубило цю
    # інформацію - наступний пошук з тим самим описом вакансії підхопить її.
    seen_save_error = save_seen_urls(apify_token, st.session_state.job_description, st.session_state.seen_urls)
    if seen_save_error:
        log.append(f"⚠️ Не вдалось зберегти пам'ять про перевірених кандидатів: {seen_save_error}")

    # Перевіряємо кандидатів ПАЧКАМИ по MAX_CANDIDATE_WORKERS одночасно
    # (ThreadPoolExecutor) замість строго по одному - швидше в кілька разів
    # на пул із 20+ кандидатів. Свідомо невелике число потоків (3, не 5+),
    # бо це напряму множить кількість одночасних запитів до Claude API.
    # st.session_state НЕ чіпаємо всередині потоків - тільки читаємо
    # результати в головному потоці після future.result() і дописуємо серійно.
    approved_this_iteration = 0
    pool_iter = iter(candidate_pool)
    # Якщо калібрування ще не підтверджене - зупиняємось на calibration_size,
    # а НЕ на повній цілі, навіть якщо ця ітерація сама по собі достатньо
    # продуктивна, щоб набрати весь target_count одразу. Інакше продуктивна
    # ітерація могла б проскочити повз калібрувальний чекпоінт за один раз, і
    # рекрутерка побачила б калібрування вже ПІСЛЯ того, як увесь пошук
    # фактично завершився - тобто пізно щось коригувати.
    target_for_this_pass = st.session_state.target_count
    if st.session_state.calibration_enabled and not st.session_state.calibration_approved:
        target_for_this_pass = min(target_for_this_pass, st.session_state.calibration_size)
    while len(st.session_state.approved_candidates) < target_for_this_pass:
        batch = list(itertools.islice(pool_iter, MAX_CANDIDATE_WORKERS))
        if not batch:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CANDIDATE_WORKERS) as executor:
            futures = [
                executor.submit(
                    process_single_candidate, claude_client, profile, source,
                    st.session_state.ashby_api_key, st.session_state.job_description,
                    st.session_state.stop_list, st.session_state.allow_belarusians,
                    st.session_state.recruiter_feedback, st.session_state.donor_list,
                )
                for profile, source in batch
            ]
            for future in futures:
                candidate_row, log_lines = future.result()
                log.extend(log_lines)
                if candidate_row:
                    st.session_state.approved_candidates.append(candidate_row)
                    approved_this_iteration += 1
                    log.append(f"   → {len(st.session_state.approved_candidates)}/{st.session_state.target_count}")

    st.session_state.search_history.append({
        "iteration": iteration,
        "search_params": search_params,
        "recruiter_note": recruiter_note,
        "candidates_checked": len(candidate_pool),
        "approved_this_iteration": approved_this_iteration,
        "approved_total": len(st.session_state.approved_candidates),
    })

    if len(st.session_state.approved_candidates) >= st.session_state.target_count:
        st.session_state.stage = "done"
        log.append(f"✅ Ціль досягнута: {len(st.session_state.approved_candidates)}/{st.session_state.target_count}")
    elif iteration >= st.session_state.max_iterations:
        st.session_state.stage = "done"
        log.append(
            f"⏹️ Ліміт ітерацій вичерпано ({st.session_state.max_iterations}), "
            f"знайдено {len(st.session_state.approved_candidates)}/{st.session_state.target_count}"
        )


# --- ЕКРАН 1: СТАРТОВА ФОРМА (тільки поки цикл ще не запущений) ---
if st.session_state.stage == "idle":
    with st.expander("👥 Наша команда"):
        st.markdown(
            f"- {TEAM['strategist']} - будує й перебудовує пошуковий запит, враховує твої корективи\n"
            f"- {TEAM['pm']} - лагодить технічні збої Apify-запиту і запам'ятовує рішення назавжди\n"
            f"- {TEAM['ats']} - звірка з Ashby (поки заглушка, без ключа не перевіряє)\n"
            f"- {TEAM['evaluator']} - жорстка оцінка кандидата: локація, стоп-лист, грейд, тір\n"
            f"- {TEAM['critic']} - перевіряє {TEAM['evaluator']} на помилки, пропускає далі тільки чистих кандидатів"
        )
    job_description_input = st.text_area(
        "Опис вакансії / вимоги (Job Description)",
        height=250,
        placeholder="Встав сюди повний опис вакансії: тайтл, обов'язки, must-have навички, локація тощо.",
        key="jd_input",
    )
    target_count_input = st.number_input(
        "Скільки ідеальних (A-tier) кандидатів потрібно?", min_value=1, max_value=50, value=10, key="target_input"
    )
    start_button = st.button("🚀 Почати пошук", type="primary")

    if start_button:
        if not anthropic_api_key or not apify_token:
            st.error("Потрібні щонайменше Anthropic API key і Apify API token в лівій панелі.")
            st.stop()
        if not job_description_input.strip():
            st.error("Встав опис вакансії, інакше система не знатиме, кого шукати.")
            st.stop()

        # Перш ніж запускати сам пошук, Стратег дивиться на опис вакансії і
        # вирішує, чи достатньо йому інформації, чи є сенс уточнити 1-2 речі
        # (як реальний рекрутер-стратег зробив би перед тим, як діяти наосліп).
        with st.spinner("🧭 Стратег дивиться опис вакансії..."):
            brief_client = anthropic.Anthropic(api_key=anthropic_api_key)
            brief_result = agent_strategist_brief(brief_client, job_description_input)

        if brief_result.get("ready", True) or not brief_result.get("questions"):
            lock_in_and_start(job_description_input, target_count_input)
            st.rerun()
        else:
            st.session_state.brief_job_description = job_description_input
            st.session_state.brief_target_count = target_count_input
            st.session_state.brief_questions = brief_result["questions"]
            st.session_state.stage = "brief"
            st.rerun()

# --- ЕКРАН 1b: БРИФІНГ - СТРАТЕГ СТАВИТЬ 1-2 УТОЧНЮЮЧІ ПИТАННЯ ---
if st.session_state.stage == "brief":
    st.subheader("🧭 Стратег хоче уточнити пару деталей перед пошуком")
    st.caption("Коротко відповідай своїми словами - це піде прямо в опис вакансії для решти команди.")
    answers = []
    for i, q in enumerate(st.session_state.brief_questions):
        a = st.text_input(q, key=f"brief_answer_{i}")
        answers.append(a)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶️ Продовжити з відповідями", type="primary", use_container_width=True):
            extra_lines = [
                f"Уточнення рекрутера на питання «{q}»: {a.strip()}"
                for q, a in zip(st.session_state.brief_questions, answers) if a.strip()
            ]
            full_jd = st.session_state.brief_job_description
            if extra_lines:
                full_jd += "\n\n" + "\n".join(extra_lines)
            lock_in_and_start(full_jd, st.session_state.brief_target_count)
            st.rerun()
    with col2:
        if st.button("⏭️ Пропустити брифінг, почати як є", use_container_width=True):
            lock_in_and_start(st.session_state.brief_job_description, st.session_state.brief_target_count)
            st.rerun()

# --- ЕКРАН 2: АКТИВНИЙ ЦИКЛ - РІВНО ОДНА ІТЕРАЦІЯ ЗА РАЗ + ЧЕКПОІНТ ---
if st.session_state.stage == "active":
    # Використовуємо ЗАФІКСОВАНІ на старті ключі (st.session_state), а не
    # live-значення з полів сайдбару - інакше зміна поля в сайдбарі під час
    # активного циклу непомітно підмінила б ключ на наступній ітерації.
    claude_client = anthropic.Anthropic(api_key=st.session_state.anthropic_api_key)

    if st.session_state.trigger_run:
        with st.spinner(f"Виконую ітерацію {st.session_state.iteration + 1}/{st.session_state.max_iterations}..."):
            run_one_iteration(claude_client, st.session_state.apify_token, st.session_state.pending_note)
        st.session_state.trigger_run = False
        st.session_state.pending_note = ""
        st.rerun()
    elif (
        st.session_state.calibration_enabled
        and not st.session_state.calibration_approved
        and len(st.session_state.approved_candidates) >= st.session_state.calibration_size
    ):
        # --- КАЛІБРУВАЛЬНИЙ ЧЕКПОІНТ: перш ніж шукати повну кількість,
        # показуємо перших N схвалених кандидатів і чекаємо ЯВНОГО
        # підтвердження напрямку пошуку. Якщо рекрутерка каже "не те" -
        # ця (неправильна) вибірка скидається повністю, і після наступної
        # ітерації з її корективою gate показує НОВУ вибірку знову - і так,
        # поки не буде явного "так". search_history/seen_urls НЕ скидаємо
        # (Стратег далі бачить, що вже не спрацювало), скидаємо тільки
        # approved_candidates, бо ці конкретні кандидати - не те, що треба.
        st.subheader("🔍 Калібрування - це той напрямок пошуку?")
        st.caption(
            f"Перш ніж шукати решту до цілі {st.session_state.target_count}, підтвердь: ось перші "
            f"{len(st.session_state.approved_candidates)} схвалених кандидатів. Якщо профіль підходить - "
            "продовжуємо тим самим курсом; якщо ні - скажи що не так, і спробуємо інший напрямок."
        )
        preview_columns = ["Ім'я", "LinkedIn", "Поточна роль", "Грейд", "Джерело", "AI summary"]
        preview_rows = [
            {col: row.get(col, "") for col in preview_columns}
            for row in st.session_state.approved_candidates
        ]
        st.dataframe(
            preview_rows,
            column_config={"LinkedIn": st.column_config.LinkColumn("LinkedIn")},
            use_container_width=True,
            hide_index=True,
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                f"✅ Так, це те - шукати решту до {st.session_state.target_count}",
                type="primary", use_container_width=True,
            ):
                st.session_state.calibration_approved = True
                st.session_state.trigger_run = True
                st.rerun()
        with col2:
            calibration_note = st.text_area(
                "✏️ Ні, ось що не так / що виправити",
                placeholder="напр. 'це не той рівень', 'потрібно ближче до продажів, не до маркетингу'",
                key="calibration_note_input",
            )
            if st.button("🔁 Скасувати цю вибірку і спробувати інший напрямок", use_container_width=True):
                st.session_state.log.append(
                    f"🔁 Рекрутерка відхилила калібрувальну вибірку "
                    f"({len(st.session_state.approved_candidates)} кандидатів): "
                    f"_{calibration_note.strip() or '(без коментаря)'}_ - скидаю цю вибірку, пробую інший напрямок."
                )
                st.session_state.approved_candidates = []
                st.session_state.pending_note = calibration_note
                st.session_state.trigger_run = True
                st.rerun()
    else:
        st.info(
            f"Ітерація {st.session_state.iteration}/{st.session_state.max_iterations} завершена. "
            f"Знайдено {len(st.session_state.approved_candidates)}/{st.session_state.target_count} A-tier кандидатів."
        )
        recruiter_note = st.text_area(
            "💬 Корективи для наступної ітерації (необов'язково)",
            placeholder="напр. 'шукай ще в Львові', 'тайтл X не підходить, забудь про нього', 'спробуй компанію Y як донора'",
            key="recruiter_note_input",
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("▶️ Продовжити з наступною ітерацією", type="primary", use_container_width=True):
                st.session_state.pending_note = recruiter_note
                st.session_state.trigger_run = True
                st.rerun()
        with col2:
            if st.button("⏹️ Завершити зараз і показати фінальний список", use_container_width=True):
                st.session_state.stage = "done"
                st.rerun()

# --- ЛОГ ПОШУКУ (видно і під час циклу, і після завершення) ---
if st.session_state.log:
    with st.expander("📜 Лог пошуку по ітераціях", expanded=(st.session_state.stage != "done")):
        for line in st.session_state.log:
            st.markdown(line)

# --- ЕКРАН 3: ФІНАЛЬНІ РЕЗУЛЬТАТИ ---
if st.session_state.stage == "done":
    if 0 < len(st.session_state.approved_candidates) < st.session_state.get("target_count", 0):
        st.warning(
            f"Зупинено на {len(st.session_state.approved_candidates)}/{st.session_state.target_count} кандидатів. "
            "Якщо цього не досить - онови критерії пошуку (стоп-лист, локації, тайтли в JD) і запусти новий пошук."
        )

    if st.session_state.approved_candidates:
        st.divider()
        st.subheader(f"Результати: {len(st.session_state.approved_candidates)} схвалених кандидатів")

        edited = st.data_editor(
            st.session_state.approved_candidates,
            column_config={
                "Обрати": st.column_config.CheckboxColumn("Обрати"),
                "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
                "Причина відхилення": st.column_config.SelectboxColumn(
                    "Причина відхилення", options=REJECTION_REASON_OPTIONS,
                    help="Заповнюй для кандидатів, яких НЕ будеш просувати - це фідбек для навчання AI.",
                ),
                "Своя причина": st.column_config.TextColumn(
                    "Своя причина (якщо обрано 'Інше')",
                ),
            },
            disabled=["Ім'я", "LinkedIn", "Поточна роль", "Грейд", "Джерело", "Ashby статус", "AI summary"],
            use_container_width=True,
            hide_index=True,
        )

        selected = [row for row in edited if row["Обрати"]]

        col_export, col_feedback = st.columns(2)
        with col_export:
            if st.button(f"📤 Експортувати обраних ({len(selected)}) у CSV для Ashby"):
                if not selected:
                    st.warning("Спочатку познач хоча б одного кандидата чекбоксом.")
                else:
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    # УВАГА: це орієнтовний формат CSV. Точні назви колонок для імпорту
                    # треба звірити з реальним Ashby-акаунтом, коли буде доступ до нього.
                    writer.writerow(["Name", "LinkedIn URL", "Current Role", "Grade", "Source", "Notes"])
                    for row in selected:
                        writer.writerow([row["Ім'я"], row["LinkedIn"], row["Поточна роль"], row["Грейд"], row["Джерело"], row["AI summary"]])
                    st.download_button(
                        "⬇️ Завантажити CSV",
                        data=buf.getvalue(),
                        file_name="skelar_sourcing_export.csv",
                        mime="text/csv",
                    )

        with col_feedback:
            if st.button("💾 Зберегти фідбек для навчання AI"):
                if not apify_token:
                    st.warning("Потрібен Apify API token в сайдбарі, щоб зберегти фідбек.")
                else:
                    role_label = extract_role_label(st.session_state.job_description)
                    new_entries = []
                    for row in edited:
                        reason_choice = row.get("Причина відхилення", "")
                        if not reason_choice:
                            continue
                        reason_text = row.get("Своя причина", "").strip() if reason_choice == "Інше" else reason_choice
                        if not reason_text:
                            continue
                        new_entries.append({
                            "candidate_name": row["Ім'я"],
                            "reason": reason_text,
                            "role": role_label,
                        })
                    if not new_entries:
                        st.warning("Немає жодної позначеної причини відхилення - нічого зберігати.")
                    else:
                        # merge_and_save_recruiter_feedback перечитує store перед записом,
                        # щоб не затерти фідбек, збережений паралельно іншим рекрутером
                        # з тим самим спільним Apify-токеном (див. коментар у функції).
                        combined, save_error = merge_and_save_recruiter_feedback(apify_token, st.session_state.recruiter_feedback, new_entries)
                        if save_error:
                            st.warning(f"Не вдалось зберегти фідбек: {save_error}")
                        else:
                            st.session_state.recruiter_feedback = combined
                            st.success(f"Збережено {len(new_entries)} нових прикладів фідбеку - AI побачить їх у наступних пошуках.")

    if st.session_state.known_issues:
        st.divider()
        st.subheader("🧠 Уроки в постійній пам'яті")
        st.caption(
            f"Зараз система пам'ятає {len(st.session_state.known_issues)} уроків - вони вже "
            "збережені в персональному Apify Key-Value Store автоматично, нічого копіювати "
            "не треба. Наступний пошук (навіть через тиждень, навіть в іншому браузері) сам "
            "підвантажить їх і врахує заздалегідь."
        )
        with st.expander("Показати всі уроки"):
            for issue in st.session_state.known_issues:
                st.markdown(f"- {issue}")

    st.divider()
    if st.button("🔄 Почати новий пошук"):
        # "known_issues" і "known_issues_synced_token" свідомо НЕ скидаємо - це
        # постійна пам'ять, вона має пережити перехід до нового пошуку.
        for key in ["stage", "trigger_run", "pending_note", "approved_candidates", "log", "search_history", "seen_urls",
                    "iteration", "page_cursor", "brief_job_description", "brief_target_count", "brief_questions"]:
            st.session_state.pop(key, None)
        st.rerun()
